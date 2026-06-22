"""
Autorregulação — fecha o ciclo feedback → plano.

Quando o atleta conclui um treino e relata esforço/sensação NEGATIVOS, analisamos
os sinais (PSE, sensação, dor) e ajustamos os PRÓXIMOS treinos do plano ATIVO para
evitar sobrecarga. Heurística TRANSPARENTE (sem número falso): cada ajuste deixa
uma nota explicando o porquê, e dor relatada recomenda avaliação profissional.

É determinístico e testável. A narrativa da Zaya (IA) pode comentar por cima, mas
o ajuste nasce destas regras de treinador.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from runner.models import PlannedWorkout, TrainingPlan, live_planned_filter

logger = logging.getLogger(__name__)

# Marca para não reajustar o mesmo treino várias vezes (idempotente no horizonte).
ADJUST_TAG = "Ajuste Zaya"
HARD_TYPES = ("interval", "tempo", "fartlek")
HORIZON_DAYS = 7
MIN_KM = 3.0  # piso para não zerar um treino


def _km_txt(km):
    """Número de km igual ao chip do alvo: inteiro quando exato, senão 1 casa."""
    return f"{km:.0f}" if abs(km - round(km)) < 0.05 else f"{km:.1f}"


def assess(feedback, planned=None):
    """
    Classifica o feedback do treino em nível de ação: 'none' | 'ease' | 'reduce'.
    Retorna (level, reasons). Combina PSE, sensação e dor muscular — quanto mais
    sinais (e mais fortes), mais conservador o ajuste.
    """
    rpe = feedback.rpe or 0
    feeling = feedback.feeling or ""
    soreness = feedback.soreness or 0
    was_hard = bool(planned and planned.is_key_workout)

    score = 0
    reasons = []
    if rpe >= 9:
        score += 2
        reasons.append(f"esforço muito alto (PSE {rpe}/10)")
    elif rpe >= 8 and not was_hard:
        score += 2
        reasons.append(f"esforço alto num treino que era leve (PSE {rpe}/10)")
    elif rpe >= 8:
        score += 1
        reasons.append(f"esforço alto (PSE {rpe}/10)")

    if feeling == "bad":
        score += 2
        reasons.append("você se sentiu mal")
    elif feeling == "tired":
        score += 1
        reasons.append("você se sentiu muito cansado")

    if soreness >= 4:
        score += 2
        reasons.append(f"dor muscular alta ({soreness}/5)")
    elif soreness == 3:
        score += 1
        reasons.append("dor muscular moderada")

    strong = score >= 4 or soreness >= 4 or rpe >= 9
    if strong:
        return "reduce", reasons
    if score >= 2:
        return "ease", reasons
    return "none", reasons


def _easy_pace(athlete):
    profile = getattr(athlete, "fitness_profile", None)
    lo = getattr(profile, "easy_pace_s", None) if profile else None
    return (lo, lo + 20) if lo else (None, None)


def _autonomy(athlete):
    from runner.models import Profile

    profile = getattr(athlete, "profile", None)
    return getattr(profile, "coach_autonomy", Profile.AUTONOMY_SUGGEST) if profile else Profile.AUTONOMY_SUGGEST


def _adjust(athlete, level, reasons, apply=True):
    """
    Calcula (e, se `apply`, aplica) o ajuste nos próximos treinos do plano ATIVO.
    Retorna {changed, softened, pct, motivo}. Idempotente: pula treinos já ajustados.
    """
    today = timezone.localdate()
    upcoming = list(
        athlete.planned_workouts.filter(
            live_planned_filter(),
            date__gte=today, date__lte=today + timedelta(days=HORIZON_DAYS),
            status=PlannedWorkout.STATUS_PLANNED,
        ).order_by("date")
    )
    factor = 0.85 if level == "reduce" else 0.93
    pct = round((1 - factor) * 100)
    motivo = "; ".join(reasons) or "seu feedback recente"
    easy_lo, easy_hi = _easy_pace(athlete)
    changed = 0
    softened = False

    for w in upcoming:
        if ADJUST_TAG in (w.description or ""):
            continue  # já ajustado — não compõe redução em cima de redução
        softening = level == "reduce" and not softened and w.workout_type in HARD_TYPES
        if not apply:  # preview: só conta o que mudaria
            changed += 1
            softened = softened or softening
            continue
        if w.target_distance_m:
            w.target_distance_m = max(round(w.target_distance_m * factor), MIN_KM * 1000)
        if softening:
            w.workout_type = "easy"
            w.structure = []
            if easy_lo:
                w.target_pace_low_s, w.target_pace_high_s = easy_lo, easy_hi
            km = (w.target_distance_m or 6000) / 1000.0
            w.title = f"Rodagem leve {_km_txt(km)} km (ajustado)"
            softened = True
        w.description = (
            f"{(w.description or '').strip()} [{ADJUST_TAG}: −{pct}% após {motivo}.]"
        ).strip()
        w.save(update_fields=[
            "workout_type", "structure", "target_distance_m",
            "target_pace_low_s", "target_pace_high_s", "title", "description", "updated_at",
        ])
        changed += 1
    return {"changed": changed, "softened": softened, "pct": pct, "motivo": motivo}


def _summary(level, d, done):
    """Texto transparente — `done`=True (já feito) ou False (proposta)."""
    if done:
        verbo, troca = ("Reduzi" if level == "reduce" else "Suavizei"), "e troquei o próximo treino forte por uma rodagem leve"
    else:
        verbo, troca = ("Sugiro reduzir" if level == "reduce" else "Sugiro suavizar"), "e trocar o próximo treino forte por uma rodagem leve"
    parts = [f"{verbo} os próximos {d['changed']} treino(s) do plano (−{d['pct']}%)"]
    if d["softened"]:
        parts.append(troca)
    return " ".join(parts) + f" porque {d['motivo']}."


def _note_plan(athlete, text):
    active = athlete.training_plans.filter(status=TrainingPlan.STATUS_ACTIVE).first()
    if active:
        active.notes = (active.notes + "\n" + text).strip() if active.notes else text
        active.save(update_fields=["notes", "updated_at"])


def autoregulate(athlete, feedback, planned=None):
    """
    Pós-feedback. RESPEITA a autonomia do atleta (Profile.coach_autonomy):
      - off    : não interfere;
      - auto   : ajusta e avisa (reversível);
      - suggest: (PADRÃO) cria uma SUGESTÃO pendente — o atleta é quem decide.
    Nunca impõe por padrão. Retorna {mode, level, applied, suggestion, summary}.
    """
    from runner.models import CoachSuggestion, Profile

    out = {"mode": "none", "level": "none", "applied": False, "suggestion": None, "summary": ""}
    level, reasons = assess(feedback, planned)
    if level == "none":
        return out
    out["level"] = level
    pain = (feedback.soreness or 0) >= 4
    autonomy = _autonomy(athlete)

    if autonomy == Profile.AUTONOMY_OFF:
        out["mode"] = "off"
        return out

    if autonomy == Profile.AUTONOMY_AUTO:
        d = _adjust(athlete, level, reasons, apply=True)
        if not d["changed"]:
            return out
        summary = _summary(level, d, done=True)
        if pain:
            summary += " Como você relatou dor, considere uma avaliação profissional."
        _note_plan(athlete, f"{timezone.localdate():%d/%m}: {summary}")
        CoachSuggestion.objects.create(
            athlete=athlete, level=level, reasons=reasons, summary=summary,
            status=CoachSuggestion.STATUS_AUTO, resolved_at=timezone.now(),
        )
        out.update(mode="auto", applied=True, summary=summary)
        return out

    # suggest (padrão): NÃO aplica — propõe e deixa o atleta decidir.
    d = _adjust(athlete, level, reasons, apply=False)
    if not d["changed"]:
        return out
    proposal = _summary(level, d, done=False)
    if pain:
        proposal += " (Você relatou dor — se persistir, procure avaliação profissional.)"
    sug = CoachSuggestion.objects.create(
        athlete=athlete, level=level, reasons=reasons, summary=proposal,
        status=CoachSuggestion.STATUS_PENDING,
    )
    out.update(mode="suggest", suggestion=sug, summary=proposal)
    return out


def pending_suggestion(athlete, max_age_days=2):
    """A última sugestão pendente do atleta (expira as antigas)."""
    from runner.models import CoachSuggestion

    cutoff = timezone.now() - timedelta(days=max_age_days)
    CoachSuggestion.objects.filter(
        athlete=athlete, status=CoachSuggestion.STATUS_PENDING, created_at__lt=cutoff
    ).update(status=CoachSuggestion.STATUS_EXPIRED, resolved_at=timezone.now())
    return (
        CoachSuggestion.objects.filter(athlete=athlete, status=CoachSuggestion.STATUS_PENDING)
        .order_by("-created_at").first()
    )


def accept_suggestion(suggestion):
    """O atleta concordou: aplica o ajuste e devolve o texto do que foi feito."""
    from runner.models import CoachSuggestion

    d = _adjust(suggestion.athlete, suggestion.level, list(suggestion.reasons or []), apply=True)
    done = _summary(suggestion.level, d, done=True)
    _note_plan(suggestion.athlete, f"{timezone.localdate():%d/%m}: {done}")
    suggestion.status = CoachSuggestion.STATUS_ACCEPTED
    suggestion.resolved_at = timezone.now()
    suggestion.save(update_fields=["status", "resolved_at"])
    return done


def decline_suggestion(suggestion):
    """O atleta MANTEVE o treino: registra (alimenta a consciência da Zaya)."""
    from runner.models import CoachSuggestion

    suggestion.status = CoachSuggestion.STATUS_DECLINED
    suggestion.resolved_at = timezone.now()
    suggestion.save(update_fields=["status", "resolved_at"])
