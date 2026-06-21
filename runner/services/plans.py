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

from runner.models import PlannedWorkout, TrainingPlan, weekday_labels
from runner.services import fitness, metrics

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
    """Faixas de pace por tipo (s/km) a partir do pace-alvo (fallback sem histórico)."""
    paces = {}
    for kind, (lo_off, hi_off) in preset["offsets"].items():
        paces[kind] = (round(goal_pace_s + lo_off), round(goal_pace_s + hi_off))
    paces["race"] = (round(goal_pace_s), round(goal_pace_s))
    return paces


def training_paces_from_profile(profile, goal_pace_s):
    """
    Faixas de pace ancoradas nos dados REAIS do atleta (zona fácil e limiar),
    não em offsets genéricos. O ritmo de prova é a meta; o resto vem do que ele
    realmente corre. Retorna None se não houver zona fácil estimada.
    """
    easy = profile.get("easy_pace_s")
    if not easy:
        return None
    thr = profile.get("threshold_pace_s") or (easy - 25)
    interval = thr - 15
    raw = {
        "recovery": (easy + 10, easy + 30),
        "easy": (easy, easy + 15),
        "long": (max(easy - 10, goal_pace_s), easy + 5),
        "tempo": (thr - 4, thr + 10),
        "interval": (interval - 12, interval),
        "race": (goal_pace_s, goal_pace_s),
    }
    # Garante lo<=hi e valores plausíveis.
    return {k: (min(a, b), max(a, b)) for k, (a, b) in raw.items()}


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


def _spread_pick(days, k):
    """Escolhe k dias bem espaçados de uma lista ordenada."""
    if k >= len(days):
        return list(days)
    if k <= 0:
        return []
    step = len(days) / k
    return [days[int(i * step)] for i in range(k)]


def _circ_gap(a, b):
    """Distância circular (na semana) entre dois dias."""
    diff = abs(a - b)
    return min(diff, 7 - diff)


def weekly_schedule(available_days, sessions, long_day=6):
    """
    Monta a agenda da semana a partir dos DIAS DISPONÍVEIS do atleta, garantindo
    o dia do longão e ESPAÇANDO os treinos-chave (sem dois pesados colados quando
    possível). Retorna lista ordenada de (weekday, role) — role ∈ {long, quality, easy}.
    """
    days = sorted({d for d in (available_days or []) if 0 <= d <= 6})
    if not days:
        days = [1, 2, 3, 4, 5, 6]  # padrão: ter→dom
    sessions = sessions or len(days)
    floor = 3 if len(days) >= 3 else len(days)
    sessions = min(max(sessions, floor), len(days))

    long_d = long_day if long_day in days else max(days)
    others = [d for d in days if d != long_d]
    chosen_others = _spread_pick(others, sessions - 1)
    quality_d = (
        max(chosen_others, key=lambda d: _circ_gap(d, long_d)) if chosen_others else None
    )

    roles = [(long_d, "long")]
    for d in chosen_others:
        roles.append((d, "quality" if d == quality_d else "easy"))
    return sorted(roles)


def hard_days_adjacent(schedule):
    """True se dois treinos-chave (long/quality) caem em dias consecutivos."""
    hard = sorted(wd for wd, role in schedule if role in ("long", "quality"))
    if any(b - a == 1 for a, b in zip(hard, hard[1:])):
        return True
    return len(hard) >= 2 and (hard[0] + 7 - hard[-1]) == 1  # domingo→segunda


def build_week(monday, week, paces, preset, goal_distance_m, race_date=None,
               schedule=None, long_cap_km=None):
    """Distribui o volume da semana nos dias da agenda (schedule)."""
    vol = week["volume_km"]
    out = []
    sched = schedule or weekly_schedule([1, 2, 3, 4, 5, 6], preset["runs"], 6)

    # Semana da prova: rodagens leves nos dias disponíveis + a prova no dia certo.
    if week["is_race_week"] and race_date is not None:
        pre_easy = [wd for wd, _ in sched][:2]
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
            elif day < race_date and wd in pre_easy:
                out.append({
                    "date": day, "weekday": wd, "workout_type": "easy",
                    "title": "Rodagem leve", "description": "Solto, só para ativar.",
                    "target_distance_m": 5000, **_pace_kw(paces, "easy"), "structure": [],
                })
        return out

    long_days = [wd for wd, role in sched if role == "long"]
    quality_days = [wd for wd, role in sched if role == "quality"]
    easy_days = [wd for wd, role in sched if role == "easy"]
    long_km = round(min(vol * 0.35, long_cap_km or preset["long_cap_km"]), 1)
    quali_km = round(vol * 0.22, 1)
    easy_total = max(vol - long_km - quali_km, 0)
    easy_each = round(easy_total / len(easy_days), 1) if easy_days else 0

    for wd in long_days:
        out.append({
            "date": monday + timedelta(days=wd), "weekday": wd, "workout_type": "long",
            "title": f"Longão {long_km:.0f} km",
            "description": "Ritmo confortável e constante; construa resistência aeróbica.",
            "target_distance_m": long_km * 1000, **_pace_kw(paces, "long"), "structure": [],
        })
    for wd in quality_days:
        out.append(_quality_session(monday + timedelta(days=wd), week["phase"], paces, preset, quali_km))
    for wd in easy_days:
        out.append({
            "date": monday + timedelta(days=wd), "weekday": wd, "workout_type": "easy",
            "title": f"Rodagem {easy_each:.0f} km",
            "description": "Conversável; base aeróbica e recuperação ativa.",
            "target_distance_m": easy_each * 1000, **_pace_kw(paces, "easy"), "structure": [],
        })
    out.sort(key=lambda w: w["weekday"])
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


def _safety_review(anamnese, profile, schedule, n, preset, goal_distance_m):
    """Avisos clínicos (blindagem) + flag conservadora antes de gerar o plano."""
    warnings = []
    conservative = False
    if anamnese:
        if anamnese.has_pain_now:
            conservative = True
            onde = f" ({anamnese.pain_where})" if anamnese.pain_where else ""
            warnings.append(
                f"Você relatou DOR no momento{onde}. Procure avaliação de um profissional "
                "antes de cargas altas — o plano foi gerado em versão conservadora."
            )
        if (anamnese.injury_history or "").strip():
            warnings.append(
                "Histórico de lesão informado: progressão mais cautelosa; pare ao "
                "primeiro sinal de dor e priorize fortalecimento."
            )
    if profile:
        if profile.get("acwr_zone") in ("risco", "atencao"):
            warnings.append(
                "Sua carga recente já está elevada (ACWR) — as primeiras semanas "
                "seguram o volume de propósito."
            )
        if profile.get("confidence") == "low":
            warnings.append(
                "Pouco histórico disponível: plano conservador, que se ajusta conforme "
                "você registra treinos."
            )
    if hard_days_adjacent(schedule):
        warnings.append(
            "Seus dias disponíveis deixam treinos fortes em sequência — evite intensidade "
            "em dois dias seguidos; deixe um deles bem leve."
        )
    min_weeks = {5000: 4, 10000: 6, 21097: 8, 42195: 12}.get(goal_distance_m, 6)
    if n < min_weeks:
        warnings.append(
            f"Prazo curto para {preset['label']} ({n} sem; o ideal seria ≥ {min_weeks}). "
            "Priorize chegar inteiro à prova, não bater tempo."
        )
    return warnings, conservative


def generate_plan(activities, goal_distance_m, goal_race_date, start_date=None,
                  goal_time_s=None, anamnese=None):
    """
    Monta a especificação completa de um plano (sem gravar no banco).

    Usa o HISTÓRICO (FitnessProfile) e a ANAMNESE (dias disponíveis, lesões) para
    montar um plano realista e seguro, com avisos clínicos (blindagem).
    Retorna dict: meta + weeks (resumo) + workouts (lista achatada).
    """
    today = start_date or timezone.localdate()
    preset = closest_preset(goal_distance_m)

    race_monday = goal_race_date - timedelta(days=goal_race_date.weekday())
    this_monday = today - timedelta(days=today.weekday())
    n = ((race_monday - this_monday).days // 7) + 1
    n = max(1, min(n, MAX_WEEKS))
    first_monday = race_monday - timedelta(weeks=n - 1)

    taper_weeks = min(preset["taper_weeks"], max(1, n - 1))

    # --- Parâmetros vindos do HISTÓRICO real do atleta (perfil de aptidão) ---
    profile = fitness.compute_profile(activities)
    if profile:
        base = max(profile["weekly_volume_km"], 6.0)
        peak = max(
            min(preset["peak_cap_km"], max(profile["peak_weekly_km"], base * 1.4)),
            base * 1.1,
        )
        runs_per_week = min(max(profile["runs_per_week"], 3), preset["runs"])
        # O longão não salta além do que ele já fez: até ~+6 km ou o teto da prova.
        long_cap_km = min(preset["long_cap_km"], max(profile["longest_run_km"] + 6, base * 0.4))
        confidence = profile["confidence"]
    else:
        base = 12.0
        peak = max(min(preset["peak_cap_km"], base * 1.5), base * 1.1)
        runs_per_week = preset["runs"]
        long_cap_km = preset["long_cap_km"]
        confidence = "low"

    goal_pace, confident = estimate_goal_pace(activities, goal_distance_m, goal_time_s)
    # Paces ancorados nas zonas REAIS do atleta; cai p/ offsets da meta sem histórico.
    paces = (
        (training_paces_from_profile(profile, round(goal_pace)) if profile else None)
        or training_paces(goal_pace, preset)
    )

    # --- Agenda: dias disponíveis (anamnese > histórico > padrão) + espaçamento ---
    if anamnese and anamnese.available_days:
        avail = anamnese.available_days
        sessions = anamnese.sessions_per_week or runs_per_week
        long_day = anamnese.preferred_long_day
    else:
        avail = fitness.infer_training_days(activities)
        sessions = runs_per_week
        long_day = fitness.infer_long_day(activities)
    sessions = min(sessions or runs_per_week, preset["runs"])
    schedule = weekly_schedule(avail, sessions, long_day)
    runs_per_week = len(schedule)

    # --- Blindagem clínica: avisos + versão conservadora quando há risco ---
    warnings, conservative = _safety_review(
        anamnese, profile, schedule, n, preset, goal_distance_m
    )
    if conservative:
        peak = round(max(peak * 0.85, base * 1.05), 1)

    weeks = _weekly_volumes(base, peak, n, taper_weeks)
    workouts = []
    for i, week in enumerate(weeks):
        monday = first_monday + timedelta(weeks=i)
        day_workouts = build_week(
            monday, week, paces, preset, goal_distance_m, race_date=goal_race_date,
            schedule=schedule, long_cap_km=long_cap_km,
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
            "runs_per_week": runs_per_week,
            "long_cap_km": round(long_cap_km, 1),
            "training_days": weekday_labels([wd for wd, _ in schedule]),
            "goal_pace_s": round(goal_pace),
            "goal_pace_str": metrics.format_pace(goal_pace),
            "goal_time_s": goal_time_s,
            "confident": confident,
            "confidence": confidence,
            "from_history": bool(profile),
            "from_anamnese": bool(anamnese),
            "safety_warnings": warnings,
            "conservative": conservative,
            "profile": profile,
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
        notes="\n".join(meta.get("safety_warnings", [])),
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
