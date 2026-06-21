"""
Perfil de aptidão — lê TODO o histórico de corridas do atleta e o resume nos
sinais que um treinador realmente usa para montar um plano:

  volume típico (robusto), tendência, frequência, maior longão, pico histórico,
  zonas de pace de dados REAIS (fácil / limiar / esforço), ACWR, consistência,
  nível e nível de confiança (quanto histórico temos).

compute_profile() é puro e testável; update_profile() persiste o snapshot
(FitnessProfile) consultado pelo gerador de plano e pela UI.
"""
import statistics
from datetime import timedelta

from django.utils import timezone

from runner.services import metrics


def _iso_key(d):
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _weekly_km(runs):
    """{(ano, semana ISO): km} a partir das corridas."""
    buckets = {}
    for a in runs:
        d = timezone.localtime(a.start_date).date()
        buckets[_iso_key(d)] = buckets.get(_iso_key(d), 0.0) + a.distance_m / 1000.0
    return buckets


def compute_profile(activities):
    """Deriva o perfil de aptidão. Retorna dict (campos do FitnessProfile) ou None."""
    runs = sorted(
        [a for a in metrics.only_runs(activities) if a.distance_m > 0],
        key=lambda a: a.start_date,
    )
    if not runs:
        return None

    now = timezone.now()
    today = timezone.localdate()
    this_key = _iso_key(today)
    earliest = timezone.localtime(runs[0].start_date).date()
    weeks_of_data = max(1, (today - earliest).days // 7 + 1)

    weekly = _weekly_km(runs)
    completed_keys = sorted(k for k in weekly if k != this_key) or sorted(weekly)

    # --- Volume típico: mediana das últimas 8 semanas completas (robusta) ---
    last8 = [weekly[k] for k in completed_keys[-8:]]
    weekly_volume_km = round(statistics.median(last8), 1) if last8 else 0.0

    # --- Tendência: média das 4 recentes vs 4 anteriores ---
    volume_trend = "stable"
    if len(completed_keys) >= 8:
        recent4 = sum(weekly[k] for k in completed_keys[-4:]) / 4
        prev4 = sum(weekly[k] for k in completed_keys[-8:-4]) / 4
        if prev4 > 0:
            change = (recent4 - prev4) / prev4
            volume_trend = (
                "building" if change > 0.10 else "detraining" if change < -0.10 else "stable"
            )

    # --- Frequência: mediana de corridas/semana (8 semanas completas) ---
    count_by_week = {}
    for a in runs:
        key = _iso_key(timezone.localtime(a.start_date).date())
        if key != this_key:
            count_by_week[key] = count_by_week.get(key, 0) + 1
    recent_counts = [count_by_week[k] for k in sorted(count_by_week)[-8:]]
    runs_per_week = round(statistics.median(recent_counts)) if recent_counts else len(runs)

    # --- Longão recente (12 sem) e pico histórico ---
    recent_runs = [a for a in runs if a.start_date >= now - timedelta(weeks=12)]
    longest_run_km = round(
        max((a.distance_km for a in (recent_runs or runs))), 1
    )
    peak_weekly_km = round(max(weekly.values()), 1) if weekly else 0.0

    # --- Zonas de pace a partir de dados reais ---
    paces = [a.pace_seconds_per_km for a in runs if a.pace_seconds_per_km > 0]
    easy_pace_s = None
    if paces:
        median_pace = statistics.median(paces)
        easy_runs = [p for p in paces if p >= median_pace]  # metade mais lenta = aeróbico
        easy_pace_s = round(statistics.median(easy_runs or paces))

    # Melhor esforço recente (>=3km) como âncora; limiar estimado via Riegel p/ 10k.
    effort_pool = [
        a for a in runs
        if a.distance_m >= 3000 and a.pace_seconds_per_km > 0
        and a.start_date >= now - timedelta(weeks=10)
    ] or [a for a in runs if a.distance_m >= 3000 and a.pace_seconds_per_km > 0]
    recent_effort = min(effort_pool, key=lambda a: a.pace_seconds_per_km) if effort_pool else None
    recent_effort_pace_s = round(recent_effort.pace_seconds_per_km) if recent_effort else None
    recent_effort_distance_m = recent_effort.distance_m if recent_effort else None
    threshold_pace_s = None
    if recent_effort:
        t2 = recent_effort.moving_time_s * (10000 / recent_effort.distance_m) ** 1.06
        threshold_pace_s = round(t2 / 10.0)

    acwr = metrics.acwr(runs)

    # --- Consistência: % das últimas 8 semanas completas com pelo menos 1 corrida ---
    window = completed_keys[-8:]
    consistency_pct = (
        round(sum(1 for k in window if weekly.get(k, 0) > 0) / len(window) * 100)
        if window else 100
    )

    # --- Nível e confiança ---
    if weekly_volume_km >= 60 or longest_run_km >= 25:
        level = "advanced"
    elif weekly_volume_km >= 30 or longest_run_km >= 15:
        level = "intermediate"
    else:
        level = "beginner"

    n = len(runs)
    if weeks_of_data >= 8 and n >= 15:
        confidence = "high"
    elif weeks_of_data >= 4 and n >= 6:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "weeks_of_data": weeks_of_data,
        "activities_count": n,
        "weekly_volume_km": weekly_volume_km,
        "volume_trend": volume_trend,
        "runs_per_week": runs_per_week,
        "longest_run_km": longest_run_km,
        "peak_weekly_km": peak_weekly_km,
        "easy_pace_s": easy_pace_s,
        "threshold_pace_s": threshold_pace_s,
        "recent_effort_pace_s": recent_effort_pace_s,
        "recent_effort_distance_m": recent_effort_distance_m,
        "acwr": acwr["ratio"] if acwr else None,
        "acwr_zone": acwr["zone"] if acwr else "",
        "consistency_pct": consistency_pct,
        "experience_level": level,
        "confidence": confidence,
    }


def infer_training_days(activities):
    """
    Dias da semana (0=Seg..6=Dom) em que o atleta costuma treinar, lidos do
    histórico. Usado para pré-preencher a anamnese. Considera dias com pelo
    menos ~12% das corridas (filtra eventuais treinos avulsos).
    """
    from collections import Counter

    runs = metrics.only_runs(activities)
    if not runs:
        return []
    counter = Counter(timezone.localtime(a.start_date).date().weekday() for a in runs)
    total = sum(counter.values())
    threshold = max(1, total * 0.12)
    return sorted(d for d, n in counter.items() if n >= threshold)


def infer_long_day(activities):
    """Dia em que o atleta faz as corridas mais longas (default domingo)."""
    runs = metrics.only_runs(activities)
    if not runs:
        return 6
    longest = max(runs, key=lambda a: a.distance_m)
    return timezone.localtime(longest.start_date).date().weekday()


def update_profile(athlete):
    """Computa e persiste o FitnessProfile do atleta. Retorna a instância ou None."""
    data = compute_profile(list(athlete.activities.all()))
    if not data:
        return None
    from runner.models import FitnessProfile

    profile, _ = FitnessProfile.objects.update_or_create(athlete=athlete, defaults=data)
    return profile
