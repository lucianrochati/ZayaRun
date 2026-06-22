"""
Camada de inteligência do treinador (H2 + copiloto do H3).

- auto_prescribe : gera a próxima semana de treinos (regras + ajuste por carga),
                   com nota opcional da IA.
- triage_roster  : prioriza os atletas do coach por risco/abandono/conquista.
- analyze_workout: feedback qualitativo do planejado × realizado (IA + regras).
- answer_question: copiloto conversacional sobre os dados do atleta.

Tudo degrada com elegância: sem ANTHROPIC_API_KEY, cai para texto de regras.
"""
import json
import logging
from datetime import timedelta

from django.utils import timezone

from runner.models import CoachAthlete, PlannedWorkout
from runner.services import ai, fitness, insights, metrics, plans

logger = logging.getLogger(__name__)


def _next_monday(today=None):
    today = today or timezone.localdate()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


# --------------------------------------------------------------------------
# Prescrição automática (a "planilha que se escreve")
# --------------------------------------------------------------------------
def auto_prescribe(athlete, week_start=None, created_by=None, use_ai=True):
    """
    Monta a próxima semana de treinos para o atleta, ajustando o volume pela
    carga atual (ACWR), e grava como PlannedWorkout (source=ai). Substitui
    treinos planejados ainda não realizados naquela semana.

    Retorna (qtd_criados, nota_textual).
    """
    activities = list(athlete.activities.all())
    monday = week_start or _next_monday()
    base = max(plans.current_base_km(activities), 12.0)
    acwr = metrics.acwr(activities)

    if acwr and acwr["zone"] == "risco":
        factor, phase = 0.80, "Base"
    elif acwr and acwr["zone"] == "atencao":
        factor, phase = 0.95, "Base"
    elif acwr and acwr["zone"] == "baixa":
        factor, phase = 1.08, "Construção"
    else:
        factor, phase = 1.05, "Construção"
    volume = round(base * factor, 1)

    preset = plans.closest_preset(10_000)
    goal_pace, _ = plans.estimate_goal_pace(activities, 10_000)
    paces = plans.training_paces(goal_pace, preset)
    # Intensidade calibrada pelo nível real + blindagem (ACWR alto → sem Z5).
    profile = fitness.compute_profile(activities)
    conservative = bool(acwr and acwr["zone"] in ("risco", "atencao"))
    intensity = plans._intensity_policy(profile, conservative)
    week = {"week_no": 1, "phase": phase, "volume_km": volume,
            "cutback": False, "is_race_week": False}
    day_workouts = plans.build_week(
        monday, week, paces, preset, 10_000, race_date=None, intensity=intensity
    )

    PlannedWorkout.objects.filter(
        athlete=athlete,
        date__range=(monday, monday + timedelta(days=6)),
        status=PlannedWorkout.STATUS_PLANNED,
        matched_activity__isnull=True,
    ).delete()
    objs = [
        PlannedWorkout(
            athlete=athlete, created_by=created_by, source=PlannedWorkout.SOURCE_AI,
            date=w["date"], workout_type=w["workout_type"], title=w["title"],
            description=w["description"], target_distance_m=w.get("target_distance_m"),
            target_pace_low_s=w.get("target_pace_low_s"),
            target_pace_high_s=w.get("target_pace_high_s"),
            structure=w.get("structure", []),
        )
        for w in day_workouts
    ]
    PlannedWorkout.objects.bulk_create(objs)

    note = _prescription_note(athlete, activities, acwr, volume, phase, use_ai)
    return len(objs), note


def _prescription_note(athlete, activities, acwr, volume, phase, use_ai):
    rules = (
        f"Semana de {volume:.0f} km, foco em {phase.lower()}. "
        + (acwr["label"] + "." if acwr else "Mantenha a constância.")
    )
    if not (use_ai and ai.is_enabled()):
        return rules
    ctx = insights.build_context(activities, athlete=athlete) or {}
    text = ai.complete(
        SYSTEM_PRESCRIBE,
        f"Volume previsto: {volume:.0f} km/sem, fase {phase}.\n"
        f"Dados do atleta (JSON):\n{json.dumps(ctx, ensure_ascii=False)}",
        max_tokens=250,
    )
    return text or rules


SYSTEM_PRESCRIBE = (
    "Você é um treinador de corrida. Em no máximo 2 frases (PT-BR, sem listas, "
    "sem markdown), explique o foco da semana prescrita e dê 1 orientação prática. "
    "Use apenas os dados fornecidos."
)


# --------------------------------------------------------------------------
# Triagem do roster (o coach atende quem precisa primeiro)
# --------------------------------------------------------------------------
def _priority(acwr, days_since, adherence, has_recent_pr, has_runs):
    """Maior score = mais urgente. Retorna (score, flag, motivo)."""
    if not has_runs:
        return 55, "new", "Sem treinos sincronizados ainda"
    if acwr and acwr["zone"] == "risco":
        return 100, "risk", f"Risco de lesão — ACWR {acwr['ratio']}"
    if days_since is not None and days_since >= 7:
        return 90, "idle", f"Sem correr há {days_since} dias"
    if acwr and acwr["zone"] == "atencao":
        return 70, "attention", f"Carga subindo rápido — ACWR {acwr['ratio']}"
    if adherence and adherence["pct"] < 50:
        return 65, "attention", f"Aderência baixa ({adherence['pct']}%)"
    if has_recent_pr:
        return 40, "celebrate", "Bateu recorde recente 🎉"
    return 10, "ok", "Em dia"


def triage_roster(coach):
    """Cartões dos atletas do coach, ordenados por prioridade de atenção."""
    links = (
        CoachAthlete.objects.filter(coach=coach, status=CoachAthlete.STATUS_ACTIVE)
        .select_related("athlete")
    )
    cards = []
    today = timezone.localdate()
    for link in links:
        athlete = link.athlete
        activities = list(athlete.activities.all())
        runs = metrics.only_runs(activities)
        acwr = metrics.acwr(activities)
        last = max((a.start_date for a in runs), default=None)
        days_since = (timezone.now() - last).days if last else None
        recent_planned = list(
            athlete.planned_workouts.filter(date__gte=today - timedelta(days=28))
        )
        adherence = metrics.plan_adherence_rate(recent_planned)
        prs = metrics.personal_records(activities)
        has_recent_pr = any(p["is_recent"] for p in prs)
        score, flag, reason = _priority(acwr, days_since, adherence, has_recent_pr, bool(runs))
        cards.append({
            "link": link,
            "athlete": athlete,
            "name": _athlete_name(athlete),
            "score": score,
            "flag": flag,
            "reason": reason,
            "acwr": acwr,
            "days_since": days_since,
            "adherence": adherence,
            "week_km": round(sum(
                a.distance_m for a in runs
                if a.start_date >= timezone.now() - timedelta(days=7)
            ) / 1000.0, 1),
        })
    cards.sort(key=lambda c: -c["score"])
    return cards


def roster_summary(cards):
    """Resumo curto do dia para o topo do painel do coach (regras)."""
    need = [c for c in cards if c["score"] >= 60]
    wins = [c for c in cards if c["flag"] == "celebrate"]
    parts = []
    if need:
        parts.append(f"{len(need)} atleta(s) pedem atenção hoje")
    if wins:
        parts.append(f"{len(wins)} bateram recorde recente 🎉")
    if not parts:
        parts.append("Time em dia — sem alertas hoje")
    return " · ".join(parts)


def _athlete_name(user):
    profile = getattr(user, "profile", None)
    if profile and profile.display_name:
        return profile.display_name
    return user.get_username()


# --------------------------------------------------------------------------
# Feedback qualitativo do treino (planejado × realizado)
# --------------------------------------------------------------------------
def _safe_feedback(planned):
    """Feedback do treino (reverse OneToOne) sem estourar quando não existe."""
    try:
        return planned.feedback
    except Exception:  # noqa: BLE001
        return None


def analyze_workout(planned):
    """Devolve um feedback do treino realizado vs prescrito (IA + regras)."""
    activity = planned.matched_activity
    if not activity:
        return None
    adh = metrics.adherence(planned, activity)
    rules_text = " ".join(adh["notes"]) or f"Treino cumprido — {adh['score']}% de aderência ao prescrito."
    result = {"adherence": adh, "text": rules_text, "source": "análise"}

    if not ai.is_enabled():
        return result

    feedback = _safe_feedback(planned)
    payload = {
        "prescrito": {
            "tipo": planned.get_workout_type_display(),
            "distancia_km": planned.target_distance_km,
            "pace_alvo": planned.target_pace_str,
            "descricao": planned.description,
        },
        "realizado": {
            "distancia_km": round(activity.distance_km, 1),
            "pace": activity.pace_str,
            "fade_pct": adh.get("fade_pct"),
        },
        "aderencia": {"score": adh["score"], "status": adh["status"], "obs": adh["notes"]},
        "percepcao": {
            "pse": getattr(feedback, "rpe", None),
            "sensacao": getattr(feedback, "get_feeling_display", lambda: None)()
            if feedback else None,
        } if feedback else None,
    }
    text = ai.complete(
        SYSTEM_FEEDBACK,
        f"Dados (JSON):\n{json.dumps(payload, ensure_ascii=False)}",
        max_tokens=280,
    )
    if text:
        result["text"], result["source"] = text, "treinador IA"
    return result


SYSTEM_FEEDBACK = (
    "Você é um treinador de corrida analisando um treino prescrito × realizado. "
    "Em no máximo 3 frases (PT-BR, sem markdown), diga se o atleta cumpriu o "
    "objetivo, aponte 1 ponto de atenção real dos dados e dê 1 ajuste para o "
    "próximo treino. Não invente números."
)


# --------------------------------------------------------------------------
# Copiloto conversacional (H3)
# --------------------------------------------------------------------------
def answer_question(athlete, question):
    """Responde uma pergunta em linguagem natural sobre os dados do atleta."""
    activities = list(athlete.activities.all())
    ctx = insights.build_copilot_context(activities, athlete=athlete)
    if not ctx:
        return ("Ainda não há treinos suficientes para eu analisar. Conecte a "
                "Strava e sincronize algumas corridas primeiro.")
    if not ai.is_enabled():
        return ("A Zaya (IA) está desligada. Configure uma chave gratuita "
                "GROQ_API_KEY (console.groq.com, sem cartão) — ou GEMINI_API_KEY / "
                "ANTHROPIC_API_KEY — no ambiente. Enquanto isso, seu painel já traz "
                "pace, carga (ACWR) e projeção de provas — boa parte das respostas está lá.")
    # Memória: as últimas conversas (cronológico) para a Zaya dar continuidade.
    prior = list(athlete.copilot_chats.all()[:5])[::-1]
    convo = "\n".join(f"Atleta: {c.question}\nZaya: {c.answer}" for c in prior)
    user_text = ""
    if convo:
        user_text += (
            "Conversa anterior com este atleta (use para continuidade; não repita à toa):\n"
            f"{convo}\n\n"
        )
    user_text += (
        f"Pergunta atual do atleta: {question}\n\n"
        f"Dados do atleta (JSON):\n{json.dumps(ctx, ensure_ascii=False)}"
    )
    text = ai.complete(SYSTEM_COPILOT, user_text, max_tokens=900)
    return text or (
        "A Zaya está temporariamente indisponível (limite de uso da IA ou "
        "instabilidade). Tente de novo em alguns minutos — seus dados de pace, carga "
        "(ACWR) e projeções continuam no painel."
    )


SYSTEM_COPILOT = (
    "Você é a Zaya, a treinadora de corrida do ZayaRun (fale em 1ª pessoa como Zaya). "
    "Fale EXCLUSIVAMENTE sobre corrida e o treino deste atleta: pace, volume, carga "
    "(ACWR), zonas de intensidade, plano de prova, longão, tiros, recuperação, "
    "prevenção de lesão e hábitos que afetam a corrida (sono, hidratação, alimentação "
    "no contexto do treino). Se a pergunta NÃO for sobre corrida/treino, RECUSE com "
    "gentileza em 1 frase e reconduza ao tema (ex.: 'Sou a Zaya, sua treinadora de "
    "corrida — posso te ajudar com pace, carga, plano ou a próxima prova.').\n"
    "Os dados (JSON) trazem um resumo E uma lista `runs` com CADA corrida recente "
    "(campos: data AAAA-MM-DD, distancia_km, pace_km, duracao_min, cadencia_spm, nome). "
    "Para perguntas sobre DATAS, um PERÍODO ou uma DISTÂNCIA específica (ex.: 'meus "
    "treinos de 5 km', 'corridas de 8 km nos últimos 2 meses'), FILTRE você mesmo a "
    "lista `runs`: selecione as corridas cuja distancia_km fica próxima da pedida "
    "(±0,7 km) e/ou dentro do período, e RESPONDA citando as DATAS e os PACES REAIS de "
    "CADA corrida que se encaixa. NUNCA reutilize o pace/números de uma distância "
    "diferente da perguntada. Se não houver corridas que se encaixem, diga isso "
    "claramente (não invente). Quando listar várias corridas, use uma lista curta "
    "(uma linha por corrida: data — distância — pace).\n"
    "MEMÓRIA: quando houver 'Conversa anterior', use-a para dar CONTINUIDADE — "
    "referencie o que já foi dito, não se repita e não recomece do zero como um robô.\n"
    "CONSCIÊNCIA: o JSON traz `padrao` (os hábitos NORMAIS deste atleta — dias, volume "
    "típico, pace fácil, nível) e pode trazer `sinais_fora_do_padrao` (desvios do normal "
    "dele, ex.: volume caiu, dias parado, pace mais lento, PSE subindo). Você CONHECE "
    "este atleta: quando for relevante para a pergunta, comente proativamente esses "
    "desvios, relacione ao histórico e ajuste sua orientação ao estado real dele.\n"
    "Responda em PT-BR, direto e objetivo, sem markdown pesado. Nunca invente números "
    "que não estejam nos dados. Não dê diagnóstico médico: em caso de dor ou lesão, "
    "oriente procurar um profissional de saúde."
)
