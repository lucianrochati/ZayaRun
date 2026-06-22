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


def autoregulate(athlete, feedback, planned=None):
    """
    Avalia o feedback e, se negativo, ajusta os próximos treinos do plano ATIVO.
    Retorna dict: {level, reasons, changed, softened, summary}. Não levanta.
    """
    level, reasons = assess(feedback, planned)
    result = {"level": level, "reasons": reasons, "changed": 0,
              "softened": False, "summary": ""}
    if level == "none":
        return result

    active = athlete.training_plans.filter(status=TrainingPlan.STATUS_ACTIVE).first()
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
    motivo = "; ".join(reasons) or "feedback recente"
    easy_lo, easy_hi = _easy_pace(athlete)
    softened = False

    for w in upcoming:
        if ADJUST_TAG in (w.description or ""):
            continue  # já ajustado neste ciclo — não compõe redução em cima de redução
        softening = level == "reduce" and not softened and w.workout_type in HARD_TYPES
        # 1) Reduz a distância-alvo (com piso).
        if w.target_distance_m:
            w.target_distance_m = max(round(w.target_distance_m * factor), MIN_KM * 1000)
        # 2) No caso forte, troca o PRIMEIRO treino forte por rodagem leve.
        if softening:
            w.workout_type = "easy"
            w.structure = []
            if easy_lo:
                w.target_pace_low_s, w.target_pace_high_s = easy_lo, easy_hi
            km = (w.target_distance_m or 6000) / 1000.0
            w.title = f"Rodagem leve {_km_txt(km)} km (ajustado)"
            softened = True
            result["softened"] = True
        # 3) Nota transparente do porquê (e marca para idempotência).
        w.description = (
            f"{(w.description or '').strip()} "
            f"[{ADJUST_TAG}: −{pct}% após {motivo}.]"
        ).strip()
        w.save(update_fields=[
            "workout_type", "structure", "target_distance_m",
            "target_pace_low_s", "target_pace_high_s", "title", "description", "updated_at",
        ])
        result["changed"] += 1

    # Registra na "nota do treinador" do plano (visível em plan_detail).
    pain = (feedback.soreness or 0) >= 4
    if active and result["changed"]:
        extra = []
        extra.append(
            f"{today:%d/%m}: ajuste automático (−{pct}%) nos próximos {result['changed']} "
            f"treino(s) após {motivo}."
        )
        if pain:
            extra.append(
                "Dor muscular alta relatada — se persistir, reduza ainda mais e procure "
                "avaliação de um profissional de saúde."
            )
        active.notes = (active.notes + "\n" + "\n".join(extra)).strip() if active.notes else "\n".join(extra)
        active.save(update_fields=["notes", "updated_at"])

    # Mensagem para o atleta (transparente).
    verbo = "Reduzi" if level == "reduce" else "Suavizei"
    parts = [f"{verbo} os próximos {result['changed']} treino(s) do seu plano (−{pct}%)"]
    if result["softened"]:
        parts.append("e troquei o próximo treino forte por uma rodagem leve")
    summary = " ".join(parts) + f" porque {motivo}."
    if pain:
        summary += " Como você relatou dor, pegue leve e considere uma avaliação profissional."
    result["summary"] = summary
    return result
