"""
Calculos de treinador: agregados, volume semanal, evolucao de pace e
projecao de provas. Tudo centralizado aqui para ficar testavel e
reutilizavel pelas views.
"""
from collections import OrderedDict
from datetime import timedelta

from django.utils import timezone


# Distancias-alvo de prova (em metros) para projecao.
RACE_DISTANCES = OrderedDict(
    [
        ("5 km", 5_000),
        ("10 km", 10_000),
        ("21 km (meia)", 21_097),
        ("42 km (maratona)", 42_195),
    ]
)


# Periodos de filtro (em dias). None = histórico completo.
PERIODS = OrderedDict(
    [
        ("week", ("Semana", 7)),
        ("month", ("Mês", 30)),
        ("year", ("Ano", 365)),
        ("all", ("Tudo", None)),
    ]
)


def period_choices():
    """Lista de (chave, rotulo) para montar o seletor na UI."""
    return [(key, label) for key, (label, _days) in PERIODS.items()]


def normalize_period(period):
    return period if period in PERIODS else "month"


def filter_by_period(activities, period):
    """Filtra atividades pelo periodo escolhido (janela movel em dias)."""
    period = normalize_period(period)
    _label, days = PERIODS[period]
    if days is None:
        return list(activities)
    cutoff = timezone.now() - timedelta(days=days)
    return [a for a in activities if a.start_date >= cutoff]


def only_runs(activities):
    return [a for a in activities if a.is_run]


def summary(activities):
    """Totais gerais de um conjunto de atividades (corridas)."""
    runs = only_runs(activities)
    total_distance_m = sum(a.distance_m for a in runs)
    total_time_s = sum(a.moving_time_s for a in runs)
    total_elevation = sum(a.total_elevation_gain_m for a in runs)
    avg_pace = (total_time_s / (total_distance_m / 1000.0)) if total_distance_m else 0

    cadences = [a.cadence_spm for a in runs if a.cadence_spm]
    avg_cadence = round(sum(cadences) / len(cadences)) if cadences else None
    avg_distance = (total_distance_m / 1000.0 / len(runs)) if runs else 0

    return {
        "count": len(runs),
        "total_distance_km": round(total_distance_m / 1000.0, 1),
        "total_time_s": total_time_s,
        "total_elevation_m": round(total_elevation),
        "avg_pace_seconds": avg_pace,
        "avg_pace_str": format_pace(avg_pace),
        "avg_distance_km": round(avg_distance, 1),
        "avg_cadence_spm": avg_cadence,
    }


def volume_series(activities, period):
    """
    Volume (km) agrupado de forma adequada ao periodo:
      - Semana  -> por dia (7 dias)
      - Mes     -> por semana (~6 semanas)
      - Ano/Tudo-> por mes (12 meses)
    Retorna lista de {label, km}, do mais antigo ao mais recente.
    """
    period = normalize_period(period)
    runs = only_runs(activities)
    if period == "week":
        return _bucket_by_day(runs, days=7)
    if period == "month":
        return _bucket_by_week(runs, weeks=6)
    return _bucket_by_month(runs, months=12)


def _bucket_by_day(runs, days):
    today = timezone.localdate()
    buckets = OrderedDict(
        (today - timedelta(days=i), 0.0) for i in range(days - 1, -1, -1)
    )
    for a in runs:
        d = timezone.localtime(a.start_date).date()
        if d in buckets:
            buckets[d] += a.distance_m / 1000.0
    return [{"label": d.strftime("%d/%m"), "km": round(km, 1)} for d, km in buckets.items()]


def _bucket_by_week(runs, weeks):
    today = timezone.localdate()
    start_of_week = today - timedelta(days=today.weekday())
    buckets = OrderedDict(
        (start_of_week - timedelta(weeks=i), 0.0) for i in range(weeks - 1, -1, -1)
    )
    for a in runs:
        d = timezone.localtime(a.start_date).date()
        wk = d - timedelta(days=d.weekday())
        if wk in buckets:
            buckets[wk] += a.distance_m / 1000.0
    return [{"label": wk.strftime("%d/%m"), "km": round(km, 1)} for wk, km in buckets.items()]


def _bucket_by_month(runs, months):
    today = timezone.localdate()
    keys = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    buckets = OrderedDict((k, 0.0) for k in keys)
    for a in runs:
        d = timezone.localtime(a.start_date).date()
        if (d.year, d.month) in buckets:
            buckets[(d.year, d.month)] += a.distance_m / 1000.0
    meses = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    return [
        {"label": f"{meses[mm - 1]}/{str(yy)[2:]}", "km": round(km, 1)}
        for (yy, mm), km in buckets.items()
    ]


def evolution_series(activities, limit=30):
    """
    Series por corrida (cronologicas) para os graficos de evolucao:
    pace, cadencia e distancia. Cadencia ausente vira None (gap no grafico).
    """
    runs = sorted(
        [a for a in only_runs(activities) if a.pace_seconds_per_km > 0],
        key=lambda a: a.start_date,
    )[-limit:]
    return {
        "labels": [timezone.localtime(a.start_date).strftime("%d/%m") for a in runs],
        "paces_seconds": [round(a.pace_seconds_per_km, 1) for a in runs],
        "paces_str": [a.pace_str for a in runs],
        "distances_km": [round(a.distance_km, 1) for a in runs],
        "cadences_spm": [a.cadence_spm for a in runs],
        "has_cadence": any(a.cadence_spm for a in runs),
    }


def evolution_trend(activities, recent=5, previous=5):
    """
    Compara o pace medio das ultimas `recent` corridas com as `previous`
    anteriores. Retorna diferenca em segundos/km (negativo = melhorou).
    """
    runs = sorted(
        [a for a in only_runs(activities) if a.pace_seconds_per_km > 0],
        key=lambda a: a.start_date,
    )
    if len(runs) < recent + 1:
        return None

    recent_runs = runs[-recent:]
    previous_runs = runs[-(recent + previous):-recent] or runs[:-recent]
    if not previous_runs:
        return None

    recent_avg = sum(a.pace_seconds_per_km for a in recent_runs) / len(recent_runs)
    prev_avg = sum(a.pace_seconds_per_km for a in previous_runs) / len(previous_runs)
    delta = recent_avg - prev_avg  # negativo = ficou mais rapido
    return {
        "delta_seconds": round(delta, 1),
        "improved": delta < 0,
        "recent_pace_str": format_pace(recent_avg),
        "previous_pace_str": format_pace(prev_avg),
        "delta_str": format_pace(abs(delta)),
    }


def race_projections(activities, min_distance_m=3000):
    """
    Projeta tempos de prova usando a formula de Riegel:
        T2 = T1 * (D2 / D1) ^ 1.06

    Usa como base a melhor corrida recente (maior distancia com bom pace)
    com distancia >= min_distance_m. Retorna lista por distancia-alvo.
    """
    runs = [
        a
        for a in only_runs(activities)
        if a.distance_m >= min_distance_m and a.moving_time_s > 0
    ]
    if not runs:
        return []

    # Base: corrida com melhor pace entre as mais longas (mais representativa).
    base = min(runs, key=lambda a: a.pace_seconds_per_km)
    d1 = base.distance_m
    t1 = base.moving_time_s

    projections = []
    for label, d2 in RACE_DISTANCES.items():
        t2 = t1 * (d2 / d1) ** 1.06
        projections.append(
            {
                "label": label,
                "distance_km": d2 / 1000.0,
                "time_str": format_duration(t2),
                "pace_str": format_pace(t2 / (d2 / 1000.0)),
            }
        )
    return {
        "base_distance_km": round(d1 / 1000.0, 1),
        "base_pace_str": base.pace_str,
        "projections": projections,
    }


def acwr(activities):
    """
    Acute:Chronic Workload Ratio simplificado por distancia.
    Carga aguda = km dos ultimos 7 dias.
    Carga cronica = media semanal dos ultimos 28 dias.
    Faixa "segura" classica: 0.8 a 1.3 (acima de 1.5 = risco de lesao).
    """
    runs = only_runs(activities)
    now = timezone.now()
    last7 = sum(
        a.distance_m for a in runs if a.start_date >= now - timedelta(days=7)
    ) / 1000.0
    last28 = sum(
        a.distance_m for a in runs if a.start_date >= now - timedelta(days=28)
    ) / 1000.0
    chronic_week = last28 / 4.0
    if chronic_week <= 0:
        return None
    ratio = last7 / chronic_week
    if ratio < 0.8:
        zone, status, label = "baixa", "Baixa", "Carga baixa — dá pra progredir"
    elif ratio <= 1.3:
        zone, status, label = "ideal", "Ideal", "Zona ideal de progressão"
    elif ratio <= 1.5:
        zone, status, label = "atencao", "Atenção", "Subindo rápido — cuidado"
    else:
        zone, status, label = "risco", "Risco", "Carga alta demais — risco de lesão"
    return {
        "ratio": round(ratio, 2),
        "acute_km": round(last7, 1),
        "chronic_week_km": round(chronic_week, 1),
        "zone": zone,
        "status": status,
        "label": label,
        "bar_pct": min(round(ratio / 1.5 * 100), 100),
    }


# --- Formatadores ---
def format_pace(seconds_per_km):
    if not seconds_per_km or seconds_per_km <= 0:
        return "--"
    minutes = int(seconds_per_km // 60)
    seconds = int(round(seconds_per_km % 60))
    if seconds == 60:
        minutes, seconds = minutes + 1, 0
    return f"{minutes}:{seconds:02d}"


def format_duration(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}min"
    return f"{m}min{s:02d}s"
