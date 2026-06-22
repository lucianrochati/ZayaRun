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

MAX_WEEKS = 28  # teto de semanas estruturadas (~6,5 meses); provas mais distantes
                # ganham fase de base mais longa, mas o plano NUNCA adia o início.

# --------------------------------------------------------------------------
# Linguagem universal de intensidade (Z1–Z5) — falada em cada treino, e km
# coerente entre título, descrição e alvo (target_distance_m).
# --------------------------------------------------------------------------
Z1 = "Z1 (regenerativo)"
Z2 = "Z2 (fácil/aeróbico)"
Z3 = "Z3 (moderado)"
Z4 = "Z4 (limiar)"
Z5 = "Z5 (forte/VO₂máx)"

ZONES_LEGEND = [
    ("Z1", "Regenerativo — muito leve, recuperação ativa"),
    ("Z2", "Fácil/aeróbico — conversável, base da semana"),
    ("Z3", "Moderado — ritmo de prova longa"),
    ("Z4", "Limiar — controlado-forte, sustentável ~1h"),
    ("Z5", "Forte/VO₂máx — tiros, respiração ofegante"),
]


def _tidy_km(km):
    """Arredonda para 0,5 km: número 'limpo' e idêntico em título, descrição e alvo."""
    return round(km * 2) / 2.0


def _km_txt(km):
    """Texto do nº de km igual ao chip do alvo: inteiro quando exato, senão 1 casa."""
    return f"{km:.0f}" if abs(km - round(km)) < 0.05 else f"{km:.1f}"


def _pace_range_str(paces, kind):
    """Faixa de pace de um tipo como 'm:ss–m:ss/km' (ou 'm:ss/km' se único)."""
    lo, hi = paces.get(kind, (None, None))
    if not lo and not hi:
        return None
    lo, hi = min(lo, hi), max(lo, hi)
    if lo == hi:
        return f"{metrics.format_pace(lo)}/km"
    return f"{metrics.format_pace(lo)}–{metrics.format_pace(hi)}/km"


def _rest_txt(seconds):
    """Tempo de descanso 'limpo' (arredondado a 5 s): '80s' ou '2min30'."""
    s = int(round(seconds / 5.0) * 5)
    if s < 90:
        return f"{s}s"
    m, sec = divmod(s, 60)
    return f"{m}min" if sec == 0 else f"{m}min{sec:02d}"


def _recovery_seconds(rec_m, paces):
    """Descanso entre repetições (s) = trote de recuperação × pace de trote (Z1–Z2)."""
    rec_pace = max(paces.get("recovery") or (0, 0))  # o mais lento da faixa
    if not rec_pace:
        rec_pace = max(paces.get("easy") or (360, 360))
    return round(rec_m / 1000.0 * rec_pace)


def _intensity_policy(profile, conservative):
    """
    Calibra a INTENSIDADE-teto pelo nível REAL do atleta + blindagem clínica.

    O *pace* dos tiros já vem do histórico (limiar) — aqui decidimos a ZONA-teto e
    se o trabalho forte (Z5) é liberado. Iniciante OU versão conservadora (dor,
    lesão, ACWR alto, pouco histórico) NÃO recebe Z5: os "tiros" viram progressivos
    controlados (Z3) e o intervalado forte é trocado por ritmo controlado.
    """
    level = (profile or {}).get("experience_level", "beginner")
    if conservative or level == "beginner":
        return {
            "hard": False,
            "strides_zone": Z3,
            "pace_kind": "tempo",  # referência de ritmo controlado p/ os progressivos
            "strides_note": "acelere de forma controlada (sem sprint) e desacelere",
        }
    # intermediário e avançado: trabalho forte liberado
    return {
        "hard": True,
        "strides_zone": Z5,
        "pace_kind": "interval",
        "strides_note": "acelere forte ao longo de cada tiro; caminhe para recuperar",
    }


# Política padrão (trabalho forte) — usada quando o chamador não calibra (ex.:
# prescrição manual avulsa). generate_plan/auto_prescribe passam a calibrada.
_DEFAULT_INTENSITY = {
    "hard": True, "strides_zone": Z5, "pace_kind": "interval",
    "strides_note": "acelere forte ao longo de cada tiro; caminhe para recuperar",
}

# Presets por distância-alvo. offsets = segundos somados ao pace de prova
# (negativo = mais rápido). long_cap = teto do longão.
RACE_PRESETS = OrderedDict(
    [
        (5_000, {
            "label": "5 km", "taper_weeks": 1, "long_cap_km": 14,
            "peak_cap_km": 55, "runs": 4, "tempo_km": 4,
            "interval": {"reps": 6, "dist_m": 800, "rec": "200m de trote", "rec_m": 200},
            "offsets": {"recovery": (70, 95), "easy": (45, 65), "long": (40, 60),
                         "tempo": (15, 30), "interval": (-20, -5)},
        }),
        (10_000, {
            "label": "10 km", "taper_weeks": 1, "long_cap_km": 18,
            "peak_cap_km": 65, "runs": 4, "tempo_km": 6,
            "interval": {"reps": 5, "dist_m": 1000, "rec": "200m de trote", "rec_m": 200},
            "offsets": {"recovery": (70, 95), "easy": (45, 65), "long": (35, 55),
                         "tempo": (5, 20), "interval": (-25, -10)},
        }),
        (21_097, {
            "label": "21 km (meia)", "taper_weeks": 2, "long_cap_km": 24,
            "peak_cap_km": 80, "runs": 5, "tempo_km": 8,
            "interval": {"reps": 5, "dist_m": 1000, "rec": "200m de trote", "rec_m": 200},
            "offsets": {"recovery": (75, 100), "easy": (50, 70), "long": (30, 50),
                         "tempo": (-10, 5), "interval": (-30, -15)},
        }),
        (42_195, {
            "label": "42 km (maratona)", "taper_weeks": 3, "long_cap_km": 34,
            "peak_cap_km": 110, "runs": 5, "tempo_km": 12,
            "interval": {"reps": 4, "dist_m": 1600, "rec": "400m de trote", "rec_m": 400},
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
               schedule=None, long_cap_km=None, intensity=None):
    """Distribui o volume da semana nos dias da agenda (schedule)."""
    vol = week["volume_km"]
    out = []
    intensity = intensity or _DEFAULT_INTENSITY
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
                    "title": "Rodagem leve — 5 km",
                    "description": f"5 km bem soltos em {Z1}–{Z2}, só para ativar as pernas.",
                    "target_distance_m": 5000, **_pace_kw(paces, "easy"), "structure": [],
                })
        return out

    long_days = [wd for wd, role in sched if role == "long"]
    quality_days = [wd for wd, role in sched if role == "quality"]
    easy_days = [wd for wd, role in sched if role == "easy"]
    # Distâncias "limpas" (passo de 0,5 km) — o MESMO número aparece no título,
    # na descrição e no alvo (target_distance_m), sem arredondamentos divergentes.
    long_km = _tidy_km(min(vol * 0.35, long_cap_km or preset["long_cap_km"]))
    quali_km = _tidy_km(vol * 0.22)
    easy_total = max(vol - long_km - quali_km, 0)
    easy_each = max(_tidy_km(easy_total / len(easy_days)), 1.0) if easy_days else 0

    for wd in long_days:
        out.append({
            "date": monday + timedelta(days=wd), "weekday": wd, "workout_type": "long",
            "title": f"Longão {_km_txt(long_km)} km",
            "description": (
                f"{_km_txt(long_km)} km em ritmo confortável e constante, "
                f"{Z2} — construa resistência aeróbica."
            ),
            "target_distance_m": long_km * 1000, **_pace_kw(paces, "long"), "structure": [],
        })
    for wd in quality_days:
        out.append(_quality_session(
            monday + timedelta(days=wd), week["phase"], paces, preset, quali_km, intensity
        ))
    for wd in easy_days:
        out.append({
            "date": monday + timedelta(days=wd), "weekday": wd, "workout_type": "easy",
            "title": f"Rodagem {_km_txt(easy_each)} km",
            "description": (
                f"{_km_txt(easy_each)} km conversáveis, {Z2} — "
                "base aeróbica e recuperação ativa."
            ),
            "target_distance_m": easy_each * 1000, **_pace_kw(paces, "easy"), "structure": [],
        })
    out.sort(key=lambda w: w["weekday"])
    return out


def _quality_session(date, phase, paces, preset, quali_km, intensity=None):
    wd = (date.weekday())
    intensity = intensity or _DEFAULT_INTENSITY
    tiros_pace = _pace_range_str(paces, intensity["pace_kind"])
    pace_txt = f" a ~{tiros_pace}" if tiros_pace else ""

    if phase == "Base":
        # Rodagem fácil + 6 tiros curtos de 100 m no fim (contam no alvo: +0,6 km).
        strides_km = 0.6
        total_km = quali_km + strides_km
        zone = intensity["strides_zone"]
        rest_s = 45  # caminhada de recuperação entre os tiros curtos
        return {
            "date": date, "weekday": wd, "workout_type": "strides",
            "title": f"Rodagem + tiros — {_km_txt(total_km)} km",
            "description": (
                f"{_km_txt(quali_km)} km soltos em {Z2} + 6×100 m progressivos no fim "
                f"em {zone}{pace_txt} ({intensity['strides_note']}; "
                f"~{_rest_txt(rest_s)} de caminhada entre eles)."
            ),
            "target_distance_m": round(total_km * 1000), **_pace_kw(paces, "easy"),
            "structure": [{
                "reps": 6, "distance_m": 100, "note": "progressivo",
                "recovery": "caminhada", "recovery_s": rest_s,
                "target_pace_low_s": paces[intensity["pace_kind"]][0],
                "target_pace_high_s": paces[intensity["pace_kind"]][1],
            }],
        }

    if phase == "Construção" and not intensity["hard"]:
        # Blindagem/iniciante: troca o intervalado FORTE por ritmo controlado (sem Z5).
        work_km = max(quali_km, 3.0)
        total_km = work_km + 2  # aquecimento 1 km + controlado + 1 km solto
        return {
            "date": date, "weekday": wd, "workout_type": "tempo",
            "title": f"Ritmo controlado — {_km_txt(total_km)} km",
            "description": (
                f"Aquecimento 1 km em {Z2} + {_km_txt(work_km)} km em ritmo controlado "
                f"em {Z3}{pace_txt} (forte porém confortável; ainda fala frases curtas) "
                f"+ 1 km solto. Total {_km_txt(total_km)} km."
            ),
            "target_distance_m": round(total_km * 1000), **_pace_kw(paces, "tempo"),
            "structure": [],
        }

    if phase == "Construção":
        iv = preset["interval"]
        work_km = iv["reps"] * iv["dist_m"] / 1000.0
        total_km = work_km + 3  # aquecimento 2 km + tiros + 1 km solto
        rest_s = _recovery_seconds(iv.get("rec_m", 200), paces)  # descanso entre repetições
        return {
            "date": date, "weekday": wd, "workout_type": "interval",
            "title": f"Intervalado {iv['reps']}×{iv['dist_m']} m",
            "description": (
                f"Aquecimento 2 km em {Z2} + {iv['reps']}×{iv['dist_m']} m forte em "
                f"{intensity['strides_zone']}{pace_txt}, com ~{_rest_txt(rest_s)} de "
                f"descanso ({iv['rec']}) entre as repetições + 1 km solto. "
                f"Volume total ≈ {_km_txt(total_km)} km."
            ),
            "target_distance_m": round(total_km * 1000), **_pace_kw(paces, "interval"),
            "structure": [{
                "reps": iv["reps"], "distance_m": iv["dist_m"],
                "recovery": iv["rec"], "recovery_s": rest_s,
            }],
        }

    # Pico (e demais): ritmo/limiar — Z4 (forte) ou Z3 (conservador/iniciante).
    tempo_km = preset["tempo_km"]
    total_km = tempo_km + 3  # aquecimento 2 km + ritmo + 1 km solto
    tempo_zone = Z4 if intensity["hard"] else Z3
    tempo_pace = _pace_range_str(paces, "tempo")
    tempo_pace_txt = f" a ~{tempo_pace}" if tempo_pace else ""
    return {
        "date": date, "weekday": wd, "workout_type": "tempo",
        "title": f"Ritmo no limiar — {_km_txt(total_km)} km",
        "description": (
            f"Aquecimento 2 km em {Z2} + {_km_txt(tempo_km)} km contínuos em "
            f"{tempo_zone}{tempo_pace_txt} + 1 km solto em {Z2}. Total {_km_txt(total_km)} km."
        ),
        "target_distance_m": round(total_km * 1000), **_pace_kw(paces, "tempo"),
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
    weeks_to_race = ((race_monday - this_monday).days // 7) + 1
    n = max(1, min(weeks_to_race, MAX_WEEKS))
    # Começa SEMPRE na semana atual quando a prova cabe no horizonte do plano
    # (fase de base já começa hoje). Só para provas muito distantes (> MAX_WEEKS)
    # recuamos o início para encaixar o macrociclo inteiro até a prova.
    if weeks_to_race <= MAX_WEEKS:
        first_monday = this_monday
    else:
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

    # Intensidade calibrada pelo nível real + blindagem (iniciante/conservador ≠ Z5).
    intensity = _intensity_policy(profile, conservative)

    weeks = _weekly_volumes(base, peak, n, taper_weeks)
    workouts = []
    for i, week in enumerate(weeks):
        monday = first_monday + timedelta(weeks=i)
        day_workouts = build_week(
            monday, week, paces, preset, goal_distance_m, race_date=goal_race_date,
            schedule=schedule, long_cap_km=long_cap_km, intensity=intensity,
        )
        # Na semana atual, descarta os dias que já passaram — o plano começa no
        # PRÓXIMO dia disponível (ex.: criado no sábado, domingo já é treino).
        if i == 0:
            day_workouts = [w for w in day_workouts if w["date"] >= today]
        for w in day_workouts:
            w["week_no"] = week["week_no"]
            w["phase"] = week["phase"]
        workouts.extend(day_workouts)

    actual_start = min((w["date"] for w in workouts), default=first_monday)

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
            "start_date": actual_start,
            "zones_legend": ZONES_LEGEND,
        },
        "weeks": weeks,
        "workouts": workouts,
    }


def materialize_plan(athlete, spec, created_by=None, source=PlannedWorkout.SOURCE_AI,
                     generated_by=TrainingPlan.GEN_RULES, replace_future=True):
    """
    Persiste a especificação: cria o TrainingPlan (ATIVO) e os PlannedWorkout.

    O atleta pode ter VÁRIOS planos: o novo entra como ativo e os demais ativos
    viram inativos (ficam guardados, fora do dia a dia — seus treinos NÃO são
    apagados). `replace_future` limpa só os treinos AVULSOS futuros (sem plano).
    """
    meta = spec["meta"]
    # Mantém só um plano ativo por atleta: arquiva os outros (sem apagar treinos).
    TrainingPlan.objects.filter(
        athlete=athlete, status=TrainingPlan.STATUS_ACTIVE
    ).update(status=TrainingPlan.STATUS_INACTIVE)
    if replace_future:
        PlannedWorkout.objects.filter(
            athlete=athlete, date__gte=timezone.localdate(),
            status=PlannedWorkout.STATUS_PLANNED, plan__isnull=True,
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
