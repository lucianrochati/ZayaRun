"""
Gerador de plano de prova (macrociclo) — o coração do H3.

A partir do estado atual do atleta (volume e ritmo recentes) e de uma META
(distância-alvo + data da prova), monta um plano periodizado, semana a semana:
progressão de volume (~8%/sem) com semanas de corte a cada 4, polimento
(taper) no fim, e sessões-chave (longão, ritmo, intervalado) com pace-alvo.

É determinístico e testável. A IA (services.coaching) pode reescrever a
narrativa por cima, mas a estrutura nasce destas regras de treinador.
"""
from collections import OrderedDict
from datetime import timedelta

from django.utils import timezone

from runner.models import PlannedWorkout, TrainingPlan
from runner.services import metrics

MAX_WEEKS = 20  # planos mais longos começam no meio do caminho

# Presets por distância-alvo. offsets = segundos somados ao pace de prova
# (negativo = mais rápido). long_cap = teto do longão.
RACE_PRESETS = OrderedDict(
    [
        (5_000, {
            "label": "5 km", "taper_weeks": 1, "long_cap_km": 14,
            "peak_cap_km": 55, "runs": 4, "tempo_km": 4,
            "interval": {"reps": 6, "dist_m": 800, "rec": "200m de trote"},
            "offsets": {"recovery": (70, 95), "easy": (45, 65), "long": (40, 60),
                         "tempo": (15, 30), "interval": (-20, -5)},
        }),
        (10_000, {
            "label": "10 km", "taper_weeks": 1, "long_cap_km": 18,
            "peak_cap_km": 65, "runs": 4, "tempo_km": 6,
            "interval": {"reps": 5, "dist_m": 1000, "rec": "200m de trote"},
            "offsets": {"recovery": (70, 95), "easy": (45, 65), "long": (35, 55),
                         "tempo": (5, 20), "interval": (-25, -10)},
        }),
        (21_097, {
            "label": "21 km (meia)", "taper_weeks": 2, "long_cap_km": 24,
            "peak_cap_km": 80, "runs": 5, "tempo_km": 8,
            "interval": {"reps": 5, "dist_m": 1000, "rec": "200m de trote"},
            "offsets": {"recovery": (75, 100), "easy": (50, 70), "long": (30, 50),
                         "tempo": (-10, 5), "interval": (-30, -15)},
        }),
        (42_195, {
            "label": "42 km (maratona)", "taper_weeks": 3, "long_cap_km": 34,
            "peak_cap_km": 110, "runs": 5, "tempo_km": 12,
            "interval": {"reps": 4, "dist_m": 1600, "rec": "400m de trote"},
            "offsets": {"recovery": (80, 105), "easy": (55, 75), "long": (25, 45),
                         "tempo": (-20, -5), "interval": (-35, -20)},
        }),
    ]
)

TAPER_FRACTIONS = {0: 0.35, 1: 0.55, 2: 0.70}  # fração do pico por semana até a prova


# --------------------------------------------------------------------------
# Estimativas a partir dos dados do atleta
# --------------------------------------------------------------------------
def closest_preset(goal_distance_m):
    return min(RACE_PRESETS.items(), key=lambda kv: abs(kv[0] - goal_distance_m))[1]


def current_base_km(activities):
    """Volume semanal médio das últimas 4 semanas (km)."""
    now = timezone.now()
    last28 = sum(
        a.distance_m for a in metrics.only_runs(activities)
        if a.start_date >= now - timedelta(days=28)
    ) / 1000.0
    return round(last28 / 4.0, 1)


def estimate_goal_pace(activities, goal_distance_m, goal_time_s=None):
    """
    Pace-alvo de prova (s/km). Usa a meta de tempo se houver; senão projeta
    pela melhor corrida recente (Riegel). Cai num default conservador se não
    houver dados — e sinaliza baixa confiança.
    """
    if goal_time_s:
        return goal_time_s / (goal_distance_m / 1000.0), True
    runs = [
        a for a in metrics.only_runs(activities)
        if a.distance_m >= 3000 and a.moving_time_s > 0
    ]
    if not runs:
        return 360.0, False  # 6:00/km como ponto de partida (baixa confiança)
    base = min(runs, key=lambda a: a.pace_seconds_per_km)
    t2 = base.moving_time_s * (goal_distance_m / base.distance_m) ** 1.06
    return t2 / (goal_distance_m / 1000.0), True


def training_paces(goal_pace_s, preset):
    """Faixas de pace por tipo de treino (segundos/km), a partir do pace-alvo."""
    paces = {}
    for kind, (lo_off, hi_off) in preset["offsets"].items():
        paces[kind] = (round(goal_pace_s + lo_off), round(goal_pace_s + hi_off))
    paces["race"] = (round(goal_pace_s), round(goal_pace_s))
    return paces


# --------------------------------------------------------------------------
# Montagem do macrociclo
# --------------------------------------------------------------------------
def _phase_label(week_no, n, taper_weeks):
    if (n - week_no) < taper_weeks:
        return "Polimento"
    f = week_no / n
    if f <= 0.45:
        return "Base"
    if f <= 0.8:
        return "Construção"
    return "Pico"


def _weekly_volumes(base_km, peak_km, n, taper_weeks):
    """Volume (km) de cada semana, com corte a cada 4 e taper no fim."""
    weeks = []
    running = base_km
    for i in range(n):
        week_no = i + 1
        wfe = n - week_no  # semanas até a prova
        is_race_week = i == n - 1
        cutback = False
        if wfe < taper_weeks or is_race_week:
            vol = peak_km * TAPER_FRACTIONS.get(wfe, 0.35)
            phase = "Polimento"
        elif week_no % 4 == 0:
            vol = running * 0.7
            cutback = True
            phase = _phase_label(week_no, n, taper_weeks)
        else:
            running = min(running * 1.08, peak_km)
            vol = running
            phase = _phase_label(week_no, n, taper_weeks)
        weeks.append({
            "week_no": week_no, "phase": phase, "volume_km": round(vol, 1),
            "cutback": cutback, "is_race_week": is_race_week,
        })
    return weeks


def _pace_kw(paces, kind):
    lo, hi = paces[kind]
    return {"target_pace_low_s": lo, "target_pace_high_s": hi}


def _easy_days(runs_per_week):
    # (dias de easy, dia do quali=qua=2, dia do longão=dom=6)
    if runs_per_week >= 5:
        return [1, 3, 4]  # ter, qui, sex
    return [1, 4]  # ter, sex


def build_week(monday, week, paces, preset, goal_distance_m, race_date=None):
    """Distribui o volume da semana em treinos diários (lista de dicts)."""
    vol = week["volume_km"]
    out = []

    # Semana da prova: rodagens leves + a prova no dia certo.
    if week["is_race_week"] and race_date is not None:
        for wd in range(7):
            day = monday + timedelta(days=wd)
            if day == race_date:
                out.append({
                    "date": day, "weekday": wd, "workout_type": "race",
                    "title": f"PROVA — {preset['label']}",
                    "description": "Dia da prova. Confie no plano e segure o ritmo no início.",
                    "target_distance_m": goal_distance_m,
                    **_pace_kw(paces, "race"), "structure": [],
                })
            elif day < race_date and wd in (1, 3):  # 2 rodagens leves pré-prova
                out.append({
                    "date": day, "weekday": wd, "workout_type": "easy",
                    "title": "Rodagem leve", "description": "Solto, só para ativar.",
                    "target_distance_m": 5000, **_pace_kw(paces, "easy"), "structure": [],
                })
        return out

    runs = preset["runs"]
    long_km = round(min(vol * 0.35, preset["long_cap_km"]), 1)
    quali_km = round(vol * 0.22, 1)
    easy_days = _easy_days(runs)
    easy_total = max(vol - long_km - quali_km, 0)
    easy_each = round(easy_total / len(easy_days), 1) if easy_days else 0

    # Longão (domingo)
    out.append({
        "date": monday + timedelta(days=6), "weekday": 6, "workout_type": "long",
        "title": f"Longão {long_km:.0f} km",
        "description": "Ritmo confortável e constante; construa resistência aeróbica.",
        "target_distance_m": long_km * 1000, **_pace_kw(paces, "long"), "structure": [],
    })

    # Sessão-chave (quarta)
    out.append(_quality_session(monday + timedelta(days=2), week["phase"], paces, preset, quali_km))

    # Rodagens fáceis
    for wd in easy_days:
        out.append({
            "date": monday + timedelta(days=wd), "weekday": wd, "workout_type": "easy",
            "title": f"Rodagem {easy_each:.0f} km",
            "description": "Conversável; base aeróbica e recuperação ativa.",
            "target_distance_m": easy_each * 1000, **_pace_kw(paces, "easy"), "structure": [],
        })
    return out


def _quality_session(date, phase, paces, preset, quali_km):
    wd = (date.weekday())
    if phase == "Base":
        return {
            "date": date, "weekday": wd, "workout_type": "strides",
            "title": "Rodagem + educativos",
            "description": f"{quali_km:.0f} km soltos + 6×100m progressivos no fim.",
            "target_distance_m": quali_km * 1000, **_pace_kw(paces, "easy"),
            "structure": [{"reps": 6, "distance_m": 100, "note": "progressivo", "recovery": "caminhada"}],
        }
    if phase == "Construção":
        iv = preset["interval"]
        total = iv["reps"] * iv["dist_m"] / 1000.0
        return {
            "date": date, "weekday": wd, "workout_type": "interval",
            "title": f"Intervalado {iv['reps']}×{iv['dist_m']}m",
            "description": (
                f"Aquecimento 2 km + {iv['reps']}×{iv['dist_m']}m forte "
                f"({iv['rec']}) + 1 km solto. Total útil ~{total:.0f} km."
            ),
            "target_distance_m": (total + 3) * 1000, **_pace_kw(paces, "interval"),
            "structure": [{"reps": iv["reps"], "distance_m": iv["dist_m"], "recovery": iv["rec"]}],
        }
    # Pico (e demais): ritmo/limiar
    return {
        "date": date, "weekday": wd, "workout_type": "tempo",
        "title": f"Ritmo {preset['tempo_km']} km",
        "description": (
            f"Aquecimento 2 km + {preset['tempo_km']} km contínuos no pace de "
            f"limiar + 1 km solto."
        ),
        "target_distance_m": (preset["tempo_km"] + 3) * 1000, **_pace_kw(paces, "tempo"),
        "structure": [],
    }


def generate_plan(activities, goal_distance_m, goal_race_date, start_date=None, goal_time_s=None):
    """
    Monta a especificação completa de um plano (sem gravar no banco).

    Retorna dict: meta (base/pico/pace/confiança) + weeks (resumo) +
    workouts (lista achatada pronta para materializar).
    """
    today = start_date or timezone.localdate()
    preset = closest_preset(goal_distance_m)

    race_monday = goal_race_date - timedelta(days=goal_race_date.weekday())
    this_monday = today - timedelta(days=today.weekday())
    n = ((race_monday - this_monday).days // 7) + 1
    n = max(1, min(n, MAX_WEEKS))
    first_monday = race_monday - timedelta(weeks=n - 1)

    taper_weeks = min(preset["taper_weeks"], max(1, n - 1))
    base = max(current_base_km(activities), 12.0)  # piso só para não degenerar
    peak = max(min(preset["peak_cap_km"], base * 1.5), base * 1.1)
    goal_pace, confident = estimate_goal_pace(activities, goal_distance_m, goal_time_s)
    paces = training_paces(goal_pace, preset)

    weeks = _weekly_volumes(base, peak, n, taper_weeks)
    workouts = []
    for i, week in enumerate(weeks):
        monday = first_monday + timedelta(weeks=i)
        day_workouts = build_week(
            monday, week, paces, preset, goal_distance_m, race_date=goal_race_date
        )
        for w in day_workouts:
            w["week_no"] = week["week_no"]
            w["phase"] = week["phase"]
        workouts.extend(day_workouts)

    return {
        "meta": {
            "label": preset["label"],
            "goal_distance_m": goal_distance_m,
            "goal_race_date": goal_race_date,
            "weeks": n,
            "base_km": round(base, 1),
            "peak_km": round(peak, 1),
            "goal_pace_s": round(goal_pace),
            "goal_pace_str": metrics.format_pace(goal_pace),
            "goal_time_s": goal_time_s,
            "confident": confident,
            "start_date": first_monday,
        },
        "weeks": weeks,
        "workouts": workouts,
    }


def materialize_plan(athlete, spec, created_by=None, source=PlannedWorkout.SOURCE_AI,
                     generated_by=TrainingPlan.GEN_RULES, replace_future=True):
    """
    Persiste a especificação: cria o TrainingPlan e os PlannedWorkout.
    Por padrão remove treinos futuros ainda não realizados (re-planejamento).
    """
    meta = spec["meta"]
    if replace_future:
        PlannedWorkout.objects.filter(
            athlete=athlete, date__gte=timezone.localdate(),
            status=PlannedWorkout.STATUS_PLANNED,
        ).delete()

    plan = TrainingPlan.objects.create(
        athlete=athlete, created_by=created_by,
        goal_distance_m=meta["goal_distance_m"], goal_label=meta["label"],
        goal_race_date=meta["goal_race_date"], goal_time_s=meta.get("goal_time_s"),
        start_date=meta["start_date"], weekly_base_km=meta["base_km"],
        peak_weekly_km=meta["peak_km"], generated_by=generated_by,
        status=TrainingPlan.STATUS_ACTIVE,
    )
    objs = []
    for w in spec["workouts"]:
        objs.append(PlannedWorkout(
            athlete=athlete, created_by=created_by, plan=plan, source=source,
            date=w["date"], workout_type=w["workout_type"], title=w["title"],
            description=w["description"], target_distance_m=w.get("target_distance_m"),
            target_pace_low_s=w.get("target_pace_low_s"),
            target_pace_high_s=w.get("target_pace_high_s"),
            structure=w.get("structure", []),
        ))
    PlannedWorkout.objects.bulk_create(objs)
    return plan
