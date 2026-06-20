"""
Insight do dia — motor de narrativa de treinador.

Gera um texto curto (PT-BR) que LE os dados do atleta e RECOMENDA o que fazer
— o diferencial que a Strava nao entrega. Dois geradores intercambiaveis:

  - "regras"      : sempre disponivel, sem dependencia externa nem custo.
  - "treinador IA": usa a API da Claude (SDK anthropic) para uma narrativa
                    natural. Cai para regras se a chave nao existir ou a
                    chamada falhar.

Provider escolhido por settings.INSIGHT_PROVIDER ("auto" | "rules" | "claude"):
  - auto   : usa a Claude se ANTHROPIC_API_KEY existir, senao regras (padrao)
  - rules  : sempre regras
  - claude : sempre tenta a Claude (fallback para regras em erro)

A chamada da Claude e cacheada por atleta para nao pesar no carregamento.
"""
import hashlib
import json
import logging
import os
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from runner.services import metrics

logger = logging.getLogger(__name__)

CACHE_TTL = 60 * 60  # 1h


# --------------------------------------------------------------------------
# Sinais (contexto) extraidos dos treinos — base para os dois geradores
# --------------------------------------------------------------------------
def build_context(activities, athlete=None):
    """
    Resume os dados do atleta em sinais simples e serializaveis.

    Se `athlete` for informado, agrega tambem sinais subjetivos (wellness:
    PSE recente, sono, dor) — sempre apenas os que existem de fato.
    """
    runs = metrics.only_runs(activities)
    if not runs:
        return None

    now = timezone.now()
    last = max(runs, key=lambda a: a.start_date)
    days_since = (now - last.start_date).days

    last7 = sum(a.distance_m for a in runs if a.start_date >= now - timedelta(days=7)) / 1000.0
    prev7 = sum(
        a.distance_m
        for a in runs
        if now - timedelta(days=14) <= a.start_date < now - timedelta(days=7)
    ) / 1000.0

    ctx = {
        "summary": metrics.summary(runs),
        "trend": metrics.evolution_trend(runs),
        "acwr": metrics.acwr(runs),
        "days_since_last_run": days_since,
        "week_km": round(last7, 1),
        "prev_week_km": round(prev7, 1),
        "last_run": {
            "name": last.name or last.sport_type,
            "distance_km": round(last.distance_km, 1),
            "pace_str": last.pace_str,
            "cadence_spm": last.cadence_spm,
            "fade_pct": _split_fade(last),
        },
    }
    if athlete is not None:
        wellness = _wellness_signals(athlete)
        if wellness:
            ctx["wellness"] = wellness
    return ctx


def _wellness_signals(athlete):
    """Sinais subjetivos recentes (apenas os preenchidos; nunca inventados)."""
    from runner.models import DailyCheckin, WorkoutFeedback

    out = {}
    checkin = DailyCheckin.objects.filter(athlete=athlete).first()
    if checkin:
        for field in ("sleep_hours", "sleep_quality", "soreness", "stress", "resting_hr", "hrv_ms"):
            value = getattr(checkin, field)
            if value is not None:
                out[field] = value
    since = timezone.localdate() - timedelta(days=14)
    rpes = [
        f.rpe
        for f in WorkoutFeedback.objects.filter(athlete=athlete, date__gte=since)
        if f.rpe is not None
    ]
    if rpes:
        out["avg_rpe_14d"] = round(sum(rpes) / len(rpes), 1)
    return out or None


def _split_fade(activity):
    """% de queda de pace entre 1a e 2a metade. Centralizado em metrics."""
    return metrics.split_fade(activity)


# --------------------------------------------------------------------------
# Orquestrador
# --------------------------------------------------------------------------
def daily_insight(activities, user=None):
    """Retorna o insight do dia, ou None se nao houver corridas."""
    ctx = build_context(activities, athlete=user)
    if not ctx:
        return None

    provider = (settings.INSIGHT_PROVIDER or "auto").lower()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    use_claude = provider == "claude" or (provider == "auto" and has_key)

    if use_claude:
        insight = _claude_insight(ctx, user)
        if insight:
            return insight
    return _rules_insight(ctx)


def _tone_from_ctx(ctx):
    acwr = ctx.get("acwr")
    if acwr and acwr["zone"] in ("risco", "atencao"):
        return "warn"
    if ctx.get("trend") and ctx["trend"]["improved"]:
        return "good"
    return "neutral"


# --------------------------------------------------------------------------
# Gerador por regras
# --------------------------------------------------------------------------
def _rules_insight(ctx):
    body = []
    trend, acwr, last = ctx["trend"], ctx["acwr"], ctx["last_run"]

    if trend:
        if trend["improved"]:
            body.append(
                f"Sua evolução está positiva: as últimas corridas saíram a "
                f"{trend['recent_pace_str']}/km, {trend['delta_str']}/km mais rápido "
                f"que antes. Bom sinal de ganho aeróbico."
            )
        else:
            body.append(
                f"O pace recente ({trend['recent_pace_str']}/km) está "
                f"{trend['delta_str']}/km mais lento que antes — pode ser fadiga "
                f"acumulada ou treinos mais puxados."
            )

    if last.get("fade_pct") is not None and last["fade_pct"] >= 4:
        body.append(
            f"No último treino você perdeu ~{last['fade_pct']:.0f}% de ritmo na "
            f"2ª metade. Vale treinar 'finish-fast' (terminar forte) nesta semana."
        )

    if acwr:
        if acwr["zone"] == "risco":
            body.append(
                f"Atenção à carga: seu ACWR está em {acwr['ratio']} (alto). "
                f"Segure o volume nos próximos dias para evitar lesão."
            )
        elif acwr["zone"] == "atencao":
            body.append(
                f"Sua carga subiu rápido (ACWR {acwr['ratio']}). Progrida com "
                f"cautela — no máximo ~10% de volume a mais por semana."
            )
        elif acwr["zone"] == "baixa":
            body.append(
                f"Carga baixa (ACWR {acwr['ratio']}): há espaço para progredir o "
                f"volume com segurança."
            )
        else:
            body.append(
                f"Carga na zona ideal (ACWR {acwr['ratio']}): pode manter o ritmo "
                f"de progressão."
            )

    if ctx["days_since_last_run"] >= 4:
        body.append(
            f"Você não corre há {ctx['days_since_last_run']} dias — que tal um "
            f"treino leve para retomar a regularidade?"
        )

    if not body:
        body.append(
            f"Você já acumula {ctx['summary']['total_distance_km']} km. "
            f"Mantenha a constância — ela é o que mais constrói evolução."
        )

    return {
        "source": "análise",
        "tone": _tone_from_ctx(ctx),
        "headline": "Insight do dia",
        "body": body,
    }


# --------------------------------------------------------------------------
# Gerador com a API da Claude (anthropic)
# --------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Você é um treinador de corrida de rua experiente, motivador e direto. "
    "Fale em português do Brasil. Analise APENAS os dados fornecidos (não "
    "invente números). Escreva no máximo 4 frases curtas, sem markdown, sem "
    "listas. Termine com UMA recomendação prática para a próxima semana. "
    "Tom encorajador, mas honesto sobre risco de lesão quando a carga estiver alta."
)


def _claude_insight(ctx, user):
    cache_key = _cache_key(ctx, user)
    if cache_key:
        cached = cache.get(cache_key)
        if cached:
            return cached

    try:
        import anthropic
    except ImportError:
        logger.warning("Pacote 'anthropic' não instalado; usando regras.")
        return None

    try:
        client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente
        resp = client.messages.create(
            model=getattr(settings, "INSIGHT_MODEL", "claude-opus-4-8"),
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Dados do atleta (JSON):\n"
                        + json.dumps(ctx, ensure_ascii=False, indent=2)
                    ),
                }
            ],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao gerar insight com a Claude; usando regras.")
        return None

    if not text:
        return None

    insight = {
        "source": "treinador IA",
        "tone": _tone_from_ctx(ctx),
        "headline": "Insight do dia",
        "body": [text],
    }
    if cache_key:
        cache.set(cache_key, insight, CACHE_TTL)
    return insight


def _cache_key(ctx, user):
    if not user or not getattr(user, "pk", None):
        return None
    raw = json.dumps(ctx, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"zayarun:insight:{user.pk}:{digest}"
