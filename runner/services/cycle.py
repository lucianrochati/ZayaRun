"""
Treino ciente do ciclo menstrual — PRIVADO da atleta.

Tudo aqui é athlete-only: nada vai pro treinador. A ideia é individual e opt-in,
e a Zaya SEMPRE pergunta (nunca decreta). A previsão de fase é só uma estimativa —
o SINTOMA do dia pesa mais. Anticoncepcional hormonal achata o ciclo, então a
previsão de fase é desligada (vai por sensação).

A Zaya educa, não diagnostica: no máximo sugere "considere um profissional"
(possível baixa disponibilidade de energia / RED-S) — sem alarme.
"""
from datetime import timedelta

from django.utils import timezone

from runner.models import CycleEvent, CycleProfile, CycleSymptom

# --- Fases do ciclo ---
MENSTRUAL = "menstrual"
FOLLICULAR = "follicular"
OVULATION = "ovulation"
LUTEAL = "luteal"
LATE_LUTEAL = "late_luteal"  # pré-menstrual / TPM

PHASE_LABEL = {
    MENSTRUAL: "Menstruação",
    FOLLICULAR: "Fase folicular",
    OVULATION: "Ovulação",
    LUTEAL: "Fase lútea",
    LATE_LUTEAL: "Pré-menstrual (TPM)",
}

# Ponto de PARTIDA: fases em que muitas (não todas) relatam mais peso. Depois
# que houver histórico, `personal_low_phases` personaliza pra ELA.
HEAVIER_PHASES = (MENSTRUAL, LATE_LUTEAL)
STRONG_PHASES = (FOLLICULAR,)

# Sintomas que a atleta pode marcar no registro diário (opcional).
SYMPTOM_OPTIONS = [
    ("colica", "Cólica"),
    ("fadiga", "Fadiga"),
    ("cabeca", "Dor de cabeça"),
    ("humor", "Oscilação de humor"),
    ("inchaco", "Inchaço"),
    ("sono", "Sono ruim"),
]

LUTEAL_LENGTH = 14       # fase lútea ~constante; a variação do ciclo está na folicular
RED_S_GAP_DAYS = 90      # sem menstruação registrada -> nudge de saúde
FERTILE_AGE_MAX = 50


def get_profile(athlete):
    return CycleProfile.objects.filter(athlete=athlete).first()


def is_enabled(athlete, profile=None):
    profile = profile if profile is not None else get_profile(athlete)
    return bool(profile and profile.enabled)


def learned_cycle_length(athlete, profile=None):
    """Mediana dos intervalos entre menstruações (>=2 registros), senão o padrão."""
    profile = profile if profile is not None else get_profile(athlete)
    starts = list(
        CycleEvent.objects.filter(athlete=athlete)
        .order_by("start_date")
        .values_list("start_date", flat=True)
    )
    gaps = sorted(
        (b - a).days for a, b in zip(starts, starts[1:]) if 18 <= (b - a).days <= 45
    )
    if len(gaps) >= 2:
        mid = len(gaps) // 2
        med = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        return int(round(med))
    return profile.avg_cycle_length if profile else 28


def phase_for(athlete, on_date=None, profile=None):
    """
    Estimativa de fase do ciclo numa data. Retorna dict (ou None).

    None quando: sem opt-in, ou nenhuma menstruação registrada.
    Em anticoncepcional, retorna um dict com `phase=None` (ciclo achatado).
    """
    profile = profile if profile is not None else get_profile(athlete)
    if not profile or not profile.enabled:
        return None
    on_date = on_date or timezone.localdate()

    if profile.on_contraception:
        return {
            "phase": None,
            "label": "Anticoncepcional — sem fase prevista",
            "predicted": False,
            "on_contraception": True,
            "is_heavier": False,
            "is_strong": False,
        }

    last = (
        CycleEvent.objects.filter(athlete=athlete, start_date__lte=on_date)
        .order_by("-start_date")
        .values_list("start_date", flat=True)
        .first()
    )
    if not last:
        return None

    cycle_len = learned_cycle_length(athlete, profile)
    period_len = max(2, min(10, profile.avg_period_length or 5))
    day = (on_date - last).days + 1  # dia 1 = 1º dia de menstruação
    ovu = max(period_len + 2, cycle_len - LUTEAL_LENGTH)

    if day <= period_len:
        phase = MENSTRUAL
    elif day < ovu - 1:
        phase = FOLLICULAR
    elif day <= ovu + 1:
        phase = OVULATION
    elif day < cycle_len - 3:
        phase = LUTEAL
    else:
        phase = LATE_LUTEAL

    heavier = phase_in_heavier(athlete, phase, profile)
    return {
        "phase": phase,
        "label": PHASE_LABEL[phase],
        "day": day,
        "cycle_length": cycle_len,
        "predicted": True,
        "on_contraception": False,
        "overdue": day > cycle_len + 2,
        "days_to_next": max(0, cycle_len - day + 1),
        "is_heavier": heavier,
        "is_strong": phase in STRONG_PHASES,
    }


def phase_in_heavier(athlete, phase, profile=None):
    """A fase é 'pesada' pra ELA? Usa o padrão aprendido; senão o padrão geral."""
    personal = personal_low_phases(athlete)
    if personal is not None:
        return phase in personal
    return phase in HEAVIER_PHASES


def personal_low_phases(athlete, min_samples=6):
    """
    Aprende em QUE fases ela cai: fases cuja energia média fica >=0.5 abaixo da
    média geral dela. Precisa de histórico mínimo; senão None (usa o padrão).
    """
    syms = list(
        CycleSymptom.objects.filter(athlete=athlete, energy__isnull=False)
    )
    if len(syms) < min_samples:
        return None
    profile = get_profile(athlete)
    by_phase, overall = {}, []
    for s in syms:
        ph = _phase_name_only(athlete, s.date, profile)
        if not ph:
            continue
        by_phase.setdefault(ph, []).append(s.energy)
        overall.append(s.energy)
    if not overall:
        return None
    avg = sum(overall) / len(overall)
    low = {
        ph for ph, vals in by_phase.items()
        if len(vals) >= 2 and sum(vals) / len(vals) <= avg - 0.5
    }
    return low or None


def _phase_name_only(athlete, on_date, profile):
    """Fase (string) numa data, sem recursão no aprendizado. Interno."""
    if not profile or not profile.enabled or profile.on_contraception:
        return None
    last = (
        CycleEvent.objects.filter(athlete=athlete, start_date__lte=on_date)
        .order_by("-start_date")
        .values_list("start_date", flat=True)
        .first()
    )
    if not last:
        return None
    cycle_len = learned_cycle_length(athlete, profile)
    period_len = max(2, min(10, profile.avg_period_length or 5))
    day = (on_date - last).days + 1
    ovu = max(period_len + 2, cycle_len - LUTEAL_LENGTH)
    if day <= period_len:
        return MENSTRUAL
    if day < ovu - 1:
        return FOLLICULAR
    if day <= ovu + 1:
        return OVULATION
    if day < cycle_len - 3:
        return LUTEAL
    return LATE_LUTEAL


def upcoming_phase_outlook(athlete, weeks=4, profile=None):
    """
    Periodização CONSULTIVA: olhar das próximas semanas x ciclo, pra a atleta
    planejar (que semana é boa pra puxar, qual aliviar). É estimativa — a
    confiança cai quanto mais longe; por isso não mexemos no plano sozinhos, só
    mostramos. Athlete-only. Retorna lista (ou []).
    """
    profile = profile if profile is not None else get_profile(athlete)
    if not profile or not profile.enabled or profile.on_contraception:
        return []
    if not CycleEvent.objects.filter(athlete=athlete).exists():
        return []
    today = timezone.localdate()
    out = []
    for w in range(weeks):
        mid = today + timedelta(days=w * 7 + 3)  # meio da semana como representativo
        ph = phase_for(athlete, mid, profile)
        if not ph or not ph.get("phase"):
            continue
        if ph["is_strong"]:
            tag, hint = "forte", "Boa semana pra puxar o treino-chave."
        elif ph["is_heavier"]:
            tag, hint = "leve", "Pode pesar — vá pela sensação, sem culpa."
        else:
            tag, hint = "neutra", "Semana neutra."
        out.append({
            "week_of": today + timedelta(days=w * 7),
            "label": ph["label"],
            "tag": tag,
            "hint": hint,
            "confidence": "alta" if w == 0 else ("média" if w <= 2 else "baixa"),
        })
    return out


def todays_symptom(athlete, on_date=None):
    on_date = on_date or timezone.localdate()
    return CycleSymptom.objects.filter(athlete=athlete, date=on_date).first()


def guidance(athlete, planned=None, on_date=None):
    """
    Oferta athlete-facing pra um treino, dada a fase prevista + sintomas do dia.
    NUNCA modifica o plano — só sugere; a decisão é sempre da atleta. None se não
    há nada a oferecer.
    """
    ph = phase_for(athlete, on_date)
    if not ph:
        return None
    sym = todays_symptom(athlete, on_date)
    low_energy = bool(sym and sym.energy and sym.energy <= 2)
    heavy_flow = bool(sym and sym.flow and sym.flow >= 2)
    feeling_fine = bool(sym and sym.energy and sym.energy >= 4)
    key = bool(planned and getattr(planned, "is_key_workout", False))

    # Ela já disse que está bem hoje — não insiste (a menos que fluxo intenso).
    if feeling_fine and not heavy_flow:
        return None

    if not (ph.get("is_heavier") or low_energy or heavy_flow):
        # Fase forte + treino-chave -> encoraja (positivo, sem impor).
        if ph.get("is_strong") and key:
            return {
                "kind": "encourage",
                "phase": ph,
                "title": "Boa fase pra um treino forte",
                "message": (
                    "Você costuma estar com mais energia nesta fase — se sentir "
                    "bem, é um ótimo dia pro treino-chave. 💪"
                ),
            }
        return None

    motivo = ph["label"].lower() if ph.get("phase") else "como você marcou hoje"
    extra = " e você marcou energia baixa" if low_energy else ""
    return {
        "kind": "offer" if key else "checkin",
        "phase": ph,
        "low_energy": low_energy,
        "title": "Como você está hoje?",
        "message": (
            f"Pelo seu ciclo, hoje ({motivo}) pode pesar mais pra algumas pessoas"
            f"{extra}. Sem regra fixa — como você está se sentindo? "
            "Se topar, segue o treino; se estiver pesado, dá pra adaptar ou folgar "
            "sem culpa."
        ),
    }


# --- Ações iniciadas PELA atleta (nunca automáticas) ---
def _todays_workouts(athlete, on_date):
    from runner.models import PlannedWorkout, live_planned_filter

    return list(
        PlannedWorkout.objects.filter(
            live_planned_filter(),
            athlete=athlete,
            date=on_date,
            status__in=[PlannedWorkout.STATUS_PLANNED, PlannedWorkout.STATUS_MISSED],
        ).exclude(workout_type="rest")
    )


def soften_today(athlete, on_date=None):
    """'Adapta': deixa o treino de hoje mais leve (chave -> fácil, -20% de volume,
    ritmo por sensação). Iniciado pela atleta, sem culpa."""
    on_date = on_date or timezone.localdate()
    changed = 0
    for w in _todays_workouts(athlete, on_date):
        if w.is_key_workout:
            w.workout_type = "easy"
            # Título neutro de propósito: o treinador vê o plano e o motivo
            # (ciclo) é PRIVADO — não pode vazar aqui.
            w.title = "Rodagem leve (ajuste do dia)"
            w.structure = []
        if w.target_distance_m:
            w.target_distance_m = max(3000, round(w.target_distance_m * 0.8))
        w.target_pace_low_s = None  # solta o ritmo: vai pela sensação
        w.target_pace_high_s = None
        w.save()
        changed += 1
    return changed


def skip_today(athlete, on_date=None):
    """'Hoje não': folga sem culpa — dispensa o treino de hoje."""
    from runner.models import PlannedWorkout

    on_date = on_date or timezone.localdate()
    n = 0
    for w in _todays_workouts(athlete, on_date):
        w.status = PlannedWorkout.STATUS_SKIPPED
        w.save(update_fields=["status", "updated_at"])
        n += 1
    return n


def _athlete_age(athlete):
    try:
        return athlete.anamnese.age
    except Exception:  # noqa: BLE001
        return None


def red_s_flag(athlete, profile=None):
    """
    Nudge de saúde (NÃO diagnóstico): em idade fértil, sem anticoncepcional e sem
    menstruação registrada há RED_S_GAP_DAYS+, pode indicar baixa disponibilidade
    de energia. Retorna dict gentil ou None. Só pra atleta.
    """
    profile = profile if profile is not None else get_profile(athlete)
    if not profile or not profile.enabled or profile.on_contraception:
        return None
    age = _athlete_age(athlete)
    if age is not None and age > FERTILE_AGE_MAX:
        return None
    last = (
        CycleEvent.objects.filter(athlete=athlete)
        .order_by("-start_date")
        .values_list("start_date", flat=True)
        .first()
    )
    if not last:
        return None
    gap = (timezone.localdate() - last).days
    if gap < RED_S_GAP_DAYS:
        return None
    return {
        "days": gap,
        "title": "Faz um tempo sem menstruação registrada",
        "message": (
            f"Você não registra menstruação há ~{gap} dias. Em quem corre, isso às "
            "vezes é sinal de que o corpo está com pouca energia disponível pro "
            "volume de treino — pode valer conversar com um médico ou nutricionista "
            "do esporte. Não é diagnóstico, e pode ser por vários motivos."
        ),
    }
