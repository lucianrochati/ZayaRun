"""
Prontidão e risco — H3, com HONESTIDADE sobre o que é heurística e o que
seria machine learning.

O que este módulo FAZ hoje (real, determinístico):
  readiness(athlete) → combina carga (ACWR), PSE recente e o check-in do dia
  (sono/dor/estresse) num índice de prontidão 0–100 transparente. Cada
  desconto é rastreável (campo `factors`). NUNCA inventa HRV/sono que não
  existam.

O que este módulo AINDA NÃO FAZ (e por quê):
  Predição de lesão por ML. Um modelo treinado exigiria dados que o ZayaRun
  ainda não coleta de forma confiável:
    - HRV e sono objetivos (dependem da Garmin Health API — ver garmin.py);
    - histórico rotulado de lesões (precisa ser coletado dos atletas ao longo
      do tempo);
    - volume de dados suficiente para treinar/validar sem overfitting.
  Enquanto isso, `predict_injury_risk` devolve a heurística e se declara como
  tal (`method="heuristic"`). O ponto de extensão para trocar por um modelo
  treinado está marcado abaixo. Preferimos um número honesto e explicável a um
  "score de IA" caixa-preta sem base.
"""
from datetime import timedelta

from django.utils import timezone

from runner.services import metrics


def _today_checkin(athlete):
    from runner.models import DailyCheckin

    return DailyCheckin.objects.filter(athlete=athlete, date=timezone.localdate()).first()


def _avg_recent_rpe(athlete, days=10):
    from runner.models import WorkoutFeedback

    since = timezone.localdate() - timedelta(days=days)
    rpes = [
        f.rpe
        for f in WorkoutFeedback.objects.filter(athlete=athlete, date__gte=since)
        if f.rpe is not None
    ]
    return round(sum(rpes) / len(rpes), 1) if rpes else None


def _cycle_signal(athlete):
    """
    Sinal PRIVADO do ciclo pra prontidão — só entra no contexto da PRÓPRIA atleta
    (readiness com include_private=True). Nunca vai pro treinador.
    """
    from runner.services import cycle

    prof = cycle.get_profile(athlete)
    if not prof or not prof.enabled:
        return None
    ph = cycle.phase_for(athlete, profile=prof)
    sym = cycle.todays_symptom(athlete)
    penalty, factors = 0, []
    if ph and ph.get("is_heavier"):
        penalty += 8
        factors.append(f"Ciclo: {ph['label'].lower()} (fase que costuma pesar)")
    if sym and sym.energy and sym.energy < 3:
        penalty += (3 - sym.energy) * 6
        factors.append(f"Energia baixa hoje ({sym.energy}/5)")
    if sym and sym.flow and sym.flow >= 2:
        penalty += 5
        factors.append("Menstruação intensa hoje")
    return {"penalty": penalty, "factors": factors} if penalty else None


def readiness(athlete, activities=None, include_private=False):
    """
    Índice de prontidão para treinar HOJE (0–100), heurístico e explicável.
    Retorna None se não houver nenhum sinal (sem corridas e sem check-in).

    include_private: só quando é a PRÓPRIA atleta vendo (my_plan). Liga o sinal
    do ciclo menstrual, que é privado e nunca pode aparecer pro treinador.
    """
    if activities is None:
        activities = list(athlete.activities.all())
    acwr = metrics.acwr(activities)
    checkin = _today_checkin(athlete)
    avg_rpe = _avg_recent_rpe(athlete)
    cyc = _cycle_signal(athlete) if include_private else None

    if not acwr and not checkin and avg_rpe is None and not cyc:
        return None

    score = 100.0
    factors = []

    if acwr:
        if acwr["zone"] == "risco":
            score -= 40; factors.append(f"Carga alta (ACWR {acwr['ratio']})")
        elif acwr["zone"] == "atencao":
            score -= 20; factors.append(f"Carga subindo (ACWR {acwr['ratio']})")
    if checkin:
        if checkin.soreness and checkin.soreness > 2:
            pen = (checkin.soreness - 2) * 8
            score -= pen; factors.append(f"Dor muscular {checkin.soreness}/5")
        if checkin.sleep_quality and checkin.sleep_quality < 3:
            pen = (3 - checkin.sleep_quality) * 6
            score -= pen; factors.append(f"Sono ruim ({checkin.sleep_quality}/5)")
        if checkin.stress and checkin.stress > 3:
            pen = (checkin.stress - 3) * 5
            score -= pen; factors.append(f"Estresse alto ({checkin.stress}/5)")
    if avg_rpe is not None and avg_rpe > 7:
        pen = round((avg_rpe - 7) * 8)
        score -= pen; factors.append(f"PSE recente alto ({avg_rpe})")
    if cyc:
        score -= cyc["penalty"]; factors.extend(cyc["factors"])

    score = max(0, min(100, round(score)))
    if score >= 75:
        zone, label = "ideal", "Pronto para treinar forte"
    elif score >= 50:
        zone, label = "atencao", "Treine com moderação hoje"
    else:
        zone, label = "risco", "Priorize recuperação hoje"
    if not factors:
        factors.append("Sem sinais de alerta")
    return {
        "score": score,
        "zone": zone,
        "label": label,
        "factors": factors,
        "has_checkin": checkin is not None,
        "method": "heuristic",
    }


# --------------------------------------------------------------------------
# Ponto de extensão: predição de lesão por ML (NÃO implementado — ver topo).
# --------------------------------------------------------------------------
def predict_injury_risk(athlete, activities=None):
    """
    Risco de lesão. HOJE: deriva da prontidão heurística (method="heuristic").

    FUTURO (substituir o corpo abaixo, mantendo a assinatura): quando houver
    HRV/sono via Garmin + histórico rotulado de lesões, treinar um classificador
    (ex.: gradient boosting) sobre features como ACWR, monotonia/strain semanal,
    salto de volume, fade médio, PSE, sono e HRV. Retornar a probabilidade do
    modelo com method="model:<versão>". Não fabricar esse número antes da hora.
    """
    r = readiness(athlete, activities)
    if not r:
        return None
    return {
        "risk": round((100 - r["score"]) / 100, 2),  # 0..1 a partir da prontidão
        "zone": r["zone"],
        "drivers": r["factors"],
        "method": "heuristic",  # ainda NÃO é um modelo treinado
    }
