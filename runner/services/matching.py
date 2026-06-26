"""
Casamento planejado x realizado.

Liga as atividades sincronizadas da Strava ao treino prescrito (PlannedWorkout)
que elas cumpriram, por proximidade de data. Também marca como 'perdido' os
treinos vencidos sem atividade correspondente. Chamado após cada sync.

IMPORTANTE — sessão x atividade: um treino do dia frequentemente vira VÁRIAS
atividades na Strava (você para e recomeça entre os blocos: 2 km leve, 8 km
forte, 1 km leve = 11 km). Por isso o casamento é por SESSÃO, não por atividade
solta: corridas próximas no tempo são agrupadas e somadas, e o conjunto inteiro
vira o 'realizado' do treino (M2M `matched_activities`).
"""
import logging
from datetime import timedelta

from django.utils import timezone

from runner.models import (
    Activity,
    PlannedWorkout,
    RealizedSession,
    live_planned_filter,
)

logger = logging.getLogger(__name__)

# Janela de tolerancia (em dias) para casar sessão com treino prescrito.
MATCH_WINDOW_DAYS = 1
# Intervalo maximo entre corridas para serem a MESMA sessão (para/recomeça).
SESSION_GAP_HOURS = 4


def _cluster_sessions(activities):
    """
    Agrupa corridas próximas no tempo numa mesma sessão. Duas corridas com
    intervalo (fim de uma -> início da outra) <= SESSION_GAP_HOURS são o mesmo
    treino partido em blocos; acima disso, são sessões distintas (ex.: leve de
    manhã + tiros à tarde). Retorna lista de listas de Activity, em ordem.
    """
    acts = sorted(activities, key=lambda a: a.start_date)
    sessions = []
    current = []
    for a in acts:
        if not current:
            current = [a]
            continue
        prev = current[-1]
        prev_end = prev.start_date + timedelta(
            seconds=prev.elapsed_time_s or prev.moving_time_s or 0
        )
        if a.start_date - prev_end <= timedelta(hours=SESSION_GAP_HOURS):
            current.append(a)
        else:
            sessions.append(current)
            current = [a]
    if current:
        sessions.append(current)
    return sessions


def _unmatched_sessions(user, days=45):
    """Sessões (clusters) de corridas recentes ainda sem treino vinculado."""
    cutoff = timezone.now() - timedelta(days=days)
    acts = (
        Activity.objects.filter(
            user=user,
            start_date__gte=cutoff,
            planned_workouts__isnull=True,  # ainda nao casadas
        )
        .order_by("start_date")
    )
    runs = [a for a in acts if a.is_run]
    return _cluster_sessions(runs)


def _distance_penalty(planned, realized):
    """Quão longe a distância da sessão ficou do alvo do treino (0 = no alvo).

    Só desempata quando há MAIS DE UM treino candidato no mesmo dia (ex.: dois
    treinos prescritos): cada sessão fica com o treino de distância mais próxima.
    """
    target = planned.target_distance_m
    if not target:
        return 0.0
    return abs(realized.distance_m - target) / target


def match_session_to_plan(session):
    """
    Acha o PlannedWorkout que ESTA SESSÃO cumpriu e vincula TODAS as atividades
    dela (o realizado vira a soma dos blocos). Retorna o treino casado, ou None.

    Critério: mesmo atleta, treino ainda não casado, status planejado/perdido,
    data dentro de +-MATCH_WINDOW_DAYS. Prefere mesmo dia; depois distância mais
    próxima do alvo; depois treino-chave. Só corridas.
    """
    runs = [a for a in session if a.is_run]
    if not runs:
        return None

    realized = RealizedSession(runs)
    day = timezone.localtime(realized.start_date).date()
    candidates = list(
        PlannedWorkout.objects.filter(
            live_planned_filter(),  # só o plano ATIVO (ou avulsos); ignora planos guardados
            athlete=runs[0].user,
            date__range=(
                day - timedelta(days=MATCH_WINDOW_DAYS),
                day + timedelta(days=MATCH_WINDOW_DAYS),
            ),
            matched_activities__isnull=True,
            status__in=[PlannedWorkout.STATUS_PLANNED, PlannedWorkout.STATUS_MISSED],
        ).exclude(workout_type="rest")
    )
    if not candidates:
        return None

    # Mesmo dia primeiro; depois distancia mais proxima; depois treino-chave.
    best = min(
        candidates,
        key=lambda p: (
            abs((p.date - day).days),
            _distance_penalty(p, realized),
            0 if p.is_key_workout else 1,
        ),
    )
    best.matched_activities.add(*runs)
    best.status = PlannedWorkout.STATUS_COMPLETED
    best.save(update_fields=["status", "updated_at"])
    return best


def match_activity_to_plan(activity):
    """
    Casa o treino que a SESSÃO desta atividade cumpriu (não só este bloco).
    Reúne a sessão (corridas próximas ainda sem treino) e delega. Atalho usado
    quando se quer casar a partir de uma atividade específica.
    """
    if not activity.is_run:
        return None
    for session in _unmatched_sessions(activity.user):
        if any(a.pk == activity.pk for a in session):
            return match_session_to_plan(session)
    return None


def match_user_activities(user, days=45):
    """Casa todas as sessões recentes ainda sem treino vinculado. Pos-sync."""
    matched = 0
    for session in _unmatched_sessions(user, days=days):
        if match_session_to_plan(session):
            matched += 1
    return matched


def mark_missed_workouts(user):
    """Marca como 'perdido' treinos planejados ja vencidos e sem atividade."""
    today = timezone.localdate()
    return (
        PlannedWorkout.objects.filter(
            live_planned_filter(),  # não marca 'perdido' treino de plano guardado
            athlete=user,
            date__lt=today,
            status=PlannedWorkout.STATUS_PLANNED,
            matched_activities__isnull=True,
        )
        .exclude(workout_type="rest")
        .update(status=PlannedWorkout.STATUS_MISSED)
    )


def reconcile(user):
    """Conveniencia pos-sync: casa sessões e marca perdidos. Nunca lanca."""
    try:
        matched = match_user_activities(user)
        missed = mark_missed_workouts(user)
        return matched, missed
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao reconciliar planejado x realizado")
        return 0, 0
