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
from runner.services import fitness, matching, metrics

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


def exchange_code_raw(code):
    """Troca o `code` do callback por tokens e devolve o JSON cru (sem salvar)."""
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
    return resp.json()


def save_token(user, data):
    """Persiste (ou atualiza) o token da Strava para um usuario."""
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
    _capture_sex_hint(user, athlete.get("sex"))
    return token


def _capture_sex_hint(user, sex):
    """
    Guarda o sexo da Strava como DICA pré-marcada na confirmação do login.
    Nunca sobrescreve o que a pessoa já confirmou (a confirmação manda).
    """
    if sex not in ("M", "F"):
        return
    from runner.models import Profile

    prof, _ = Profile.objects.get_or_create(user=user)
    if not prof.sex_confirmed:
        prof.sex = sex
        prof.save(update_fields=["sex"])


def exchange_code_for_token(user, code):
    """Troca o `code` por tokens e salva para o usuario (fluxo de conexao)."""
    return save_token(user, exchange_code_raw(code))


def get_or_create_user_from_athlete(data):
    """
    Login com Strava: encontra ou cria o usuario do ZayaRun a partir do atleta
    autenticado. Reusa o usuario se ja houver um token desse athlete_id.
    Usuarios criados aqui logam SO via Strava (senha inutilizavel).
    """
    from django.contrib.auth import get_user_model

    from runner.models import Profile

    athlete = data.get("athlete") or {}
    athlete_id = athlete.get("id")
    if not athlete_id:
        raise StravaError("A Strava nao retornou o atleta; tente novamente.")

    existing = (
        StravaToken.objects.filter(athlete_id=athlete_id)
        .select_related("user")
        .first()
    )
    if existing:
        return existing.user

    User = get_user_model()
    user, created = User.objects.get_or_create(username=f"strava_{athlete_id}")
    if created:
        user.first_name = (athlete.get("firstname") or "")[:150]
        user.last_name = (athlete.get("lastname") or "")[:150]
        user.set_unusable_password()
        user.save()
        name = " ".join(
            p for p in [athlete.get("firstname"), athlete.get("lastname")] if p
        ).strip()
        Profile.objects.get_or_create(user=user, defaults={"display_name": name})
    return user


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


def sync_activities(user, deep=False):
    """
    Baixa atividades e salva/atualiza no banco. Retorna (criadas, atualizadas).

    `deep=True` faz um backfill profundo (1a conexao) — ate ~12 paginas de 100,
    respeitando o rate limit da Strava — para termos historico suficiente para
    montar o plano. `deep=False` (padrao) sincroniza so as recentes, rapido.
    """
    token = StravaToken.objects.get(user=user)
    per_page, max_pages = (100, 12) if deep else (50, 2)
    created = updated = 0
    for page in range(1, max_pages + 1):
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
    # Casa o realizado com os treinos prescritos (planejado x realizado).
    matching.reconcile(user)
    # Recalcula o perfil de aptidao (base do gerador de plano).
    try:
        fitness.update_profile(user)
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao atualizar o perfil de aptidao")
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
        "average_cadence": item.get("average_cadence"),
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
