"""
Casamento planejado x realizado.

Liga uma atividade sincronizada da Strava ao treino prescrito (PlannedWorkout)
que ela cumpriu, por proximidade de data. Tambem marca como 'perdido' os
treinos vencidos sem atividade correspondente. Chamado apos cada sync.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from runner.models import Activity, PlannedWorkout

logger = logging.getLogger(__name__)

# Janela de tolerancia (em dias) para casar atividade com treino prescrito.
MATCH_WINDOW_DAYS = 1


def match_activity_to_plan(activity):
    """
    Acha o PlannedWorkout que esta atividade cumpriu e os vincula.

    Criterio: mesmo atleta, treino ainda nao casado, status planejado/perdido,
    data dentro de +-MATCH_WINDOW_DAYS. Prefere o do mesmo dia. So corridas.
    Retorna o PlannedWorkout casado, ou None.
    """
    if not activity.is_run:
        return None

    day = timezone.localtime(activity.start_date).date()
    candidates = list(
        PlannedWorkout.objects.filter(
            athlete=activity.user,
            date__range=(
                day - timedelta(days=MATCH_WINDOW_DAYS),
                day + timedelta(days=MATCH_WINDOW_DAYS),
            ),
            matched_activity__isnull=True,
            status__in=[PlannedWorkout.STATUS_PLANNED, PlannedWorkout.STATUS_MISSED],
        ).exclude(workout_type="rest")
    )
    if not candidates:
        return None

    # Mesmo dia primeiro; depois treino-chave; depois o mais proximo na data.
    best = min(
        candidates,
        key=lambda p: (abs((p.date - day).days), 0 if p.is_key_workout else 1),
    )
    best.matched_activity = activity
    best.status = PlannedWorkout.STATUS_COMPLETED
    best.save(update_fields=["matched_activity", "status", "updated_at"])
    return best


def match_user_activities(user, days=45):
    """Casa todas as corridas recentes ainda sem treino vinculado. Pos-sync."""
    cutoff = timezone.now() - timedelta(days=days)
    acts = (
        Activity.objects.filter(user=user, start_date__gte=cutoff)
        .filter(planned_workouts__isnull=True)  # ainda nao casadas
        .order_by("start_date")
    )
    matched = 0
    for activity in acts:
        if match_activity_to_plan(activity):
            matched += 1
    return matched


def mark_missed_workouts(user):
    """Marca como 'perdido' treinos planejados ja vencidos e sem atividade."""
    today = timezone.localdate()
    return (
        PlannedWorkout.objects.filter(
            athlete=user,
            date__lt=today,
            status=PlannedWorkout.STATUS_PLANNED,
            matched_activity__isnull=True,
        )
        .exclude(workout_type="rest")
        .update(status=PlannedWorkout.STATUS_MISSED)
    )


def reconcile(user):
    """Conveniencia pos-sync: casa atividades e marca perdidos. Nunca lanca."""
    try:
        matched = match_user_activities(user)
        missed = mark_missed_workouts(user)
        return matched, missed
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao reconciliar planejado x realizado")
        return 0, 0
