"""
Cliente da API Strava: OAuth, refresh de token e sincronizacao de atividades.

Docs: https://developers.strava.com/docs/reference/

Observacoes importantes (limites reais da Strava):
- Rate limit padrao: 100 req/15min e 1000 req/dia por app.
- O escopo necessario para ler atividades e `activity:read_all`.
- Use sempre o refresh_token para renovar o access_token expirado.
"""
import logging
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from runner.models import Activity, StravaToken
from runner.services import metrics

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"
REQUEST_TIMEOUT = 20


class StravaError(Exception):
    """Erro ao falar com a API da Strava."""


def is_configured():
    return bool(settings.STRAVA_CLIENT_ID and settings.STRAVA_CLIENT_SECRET)


def build_authorize_url(redirect_uri=None, state=""):
    """Monta a URL para o usuario autorizar o app na Strava.

    `redirect_uri` pode ser deduzido dinamicamente do host da requisicao
    (ver runner.views.strava_connect); se nao informado, usa o valor de
    settings.STRAVA_REDIRECT_URI.
    """
    params = {
        "client_id": settings.STRAVA_CLIENT_ID,
        "redirect_uri": redirect_uri or settings.STRAVA_REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_token(user, code):
    """Troca o `code` do callback por tokens e salva para o usuario."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.error("Falha na troca de token Strava: %s", resp.text)
        raise StravaError("Nao foi possivel autenticar com a Strava.")

    data = resp.json()
    athlete = data.get("athlete") or {}
    token, _ = StravaToken.objects.update_or_create(
        user=user,
        defaults={
            "athlete_id": athlete.get("id"),
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": _epoch_to_dt(data["expires_at"]),
            "scope": data.get("scope", ""),
        },
    )
    return token


def _refresh_token(token):
    """Renova o access_token usando o refresh_token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.error("Falha ao renovar token Strava: %s", resp.text)
        raise StravaError("Sessao da Strava expirou. Reconecte sua conta.")

    data = resp.json()
    token.access_token = data["access_token"]
    token.refresh_token = data["refresh_token"]
    token.expires_at = _epoch_to_dt(data["expires_at"])
    token.save(update_fields=["access_token", "refresh_token", "expires_at", "updated_at"])
    return token


def _valid_access_token(token):
    if token.is_expired:
        token = _refresh_token(token)
    return token.access_token


def _get(token, path, params=None):
    access = _valid_access_token(token)
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {access}"},
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 429:
        raise StravaError("Limite de requisicoes da Strava atingido. Tente em alguns minutos.")
    if resp.status_code != 200:
        logger.error("Erro Strava GET %s: %s", path, resp.text)
        raise StravaError("Erro ao buscar dados na Strava.")
    return resp.json()


def sync_activities(user, per_page=50, pages=2):
    """
    Baixa as atividades mais recentes e salva/atualiza no banco.

    Retorna (criadas, atualizadas).
    """
    token = StravaToken.objects.get(user=user)
    created = updated = 0
    for page in range(1, pages + 1):
        batch = _get(
            token,
            "/athlete/activities",
            params={"per_page": per_page, "page": page},
        )
        if not batch:
            break
        for item in batch:
            _, was_created = _upsert_activity(user, item)
            created += int(was_created)
            updated += int(not was_created)
    return created, updated


def fetch_activity_detail(user, activity):
    """Busca os splits por km de uma atividade e persiste."""
    token = StravaToken.objects.get(user=user)
    data = _get(token, f"/activities/{activity.external_id}")
    splits = []
    for sp in data.get("splits_metric", []) or []:
        dist = sp.get("distance", 0) or 0
        moving = sp.get("moving_time", 0) or 0
        pace = (moving / (dist / 1000.0)) if dist else 0
        splits.append(
            {
                "km": sp.get("split"),
                "distance_m": dist,
                "moving_time_s": moving,
                "pace_seconds_per_km": round(pace, 1),
                "pace_str": metrics.format_pace(pace),
                "elevation_diff": sp.get("elevation_difference"),
                "average_heartrate": sp.get("average_heartrate"),
            }
        )
    activity.splits = splits
    activity.save(update_fields=["splits", "synced_at"])
    return activity


def _upsert_activity(user, item):
    defaults = {
        "user": user,
        "source": Activity.SOURCE_STRAVA,
        "name": item.get("name", ""),
        "sport_type": item.get("sport_type") or item.get("type", ""),
        "start_date": _parse_dt(item.get("start_date")),
        "distance_m": item.get("distance", 0) or 0,
        "moving_time_s": item.get("moving_time", 0) or 0,
        "elapsed_time_s": item.get("elapsed_time", 0) or 0,
        "total_elevation_gain_m": item.get("total_elevation_gain", 0) or 0,
        "average_speed_ms": item.get("average_speed", 0) or 0,
        "max_speed_ms": item.get("max_speed", 0) or 0,
        "average_heartrate": item.get("average_heartrate"),
        "max_heartrate": item.get("max_heartrate"),
    }
    return Activity.objects.update_or_create(
        source=Activity.SOURCE_STRAVA,
        external_id=str(item["id"]),
        defaults=defaults,
    )


def _epoch_to_dt(epoch):
    return datetime.fromtimestamp(int(epoch), tz=dt_timezone.utc)


def _parse_dt(value):
    if not value:
        return timezone.now()
    # Strava retorna ISO 8601 em UTC, ex: 2024-01-31T10:00:00Z
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
