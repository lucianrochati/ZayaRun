"""
Casamento por SESSÃO (não por atividade solta).

Troca o FK `PlannedWorkout.matched_activity` por um M2M `matched_activities`:
um treino do dia pode ter virado várias atividades na Strava (para/recomeça
entre os blocos). O backfill recupera os blocos órfãos do mesmo dia que ficaram
de fora quando só o primeiro pedaço era casado — corrige o histórico já
sincronizado (ex.: tiros 2 km + 8 km + 1 km que tinham casado só como 2 km).
"""
from datetime import timedelta

from django.db import migrations, models
import django.db.models.deletion

GAP = timedelta(hours=4)       # corridas mais próximas que isso = mesma sessão
WINDOW = timedelta(days=1)     # janela p/ procurar blocos irmãos


def _backfill_sessions(apps, schema_editor):
    from django.db.models import Q

    PlannedWorkout = apps.get_model("runner", "PlannedWorkout")
    Activity = apps.get_model("runner", "Activity")

    # Atividades que já representam algum treino (o "principal" casado antes).
    claimed = set(
        PlannedWorkout.objects.exclude(matched_activity__isnull=True).values_list(
            "matched_activity_id", flat=True
        )
    )
    used = set()  # evita que um bloco órfão seja puxado por dois treinos

    planned_qs = (
        PlannedWorkout.objects.exclude(matched_activity__isnull=True)
        .select_related("matched_activity")
        .order_by("date", "id")
    )
    for p in planned_qs:
        primary = p.matched_activity
        if primary is None:
            continue
        # Pool: o bloco principal + corridas órfãs (não casadas) na janela do dia.
        pool = [primary]
        siblings = (
            Activity.objects.filter(
                Q(sport_type__icontains="run"),
                user_id=primary.user_id,
                start_date__range=(
                    primary.start_date - WINDOW,
                    primary.start_date + WINDOW,
                ),
            )
            .exclude(pk=primary.pk)
        )
        for a in siblings:
            if a.id in claimed or a.id in used:
                continue
            pool.append(a)

        # Agrupa por proximidade temporal; fica com a sessão que contém o principal.
        pool.sort(key=lambda a: a.start_date)
        sessions, current = [], []
        for a in pool:
            if not current:
                current = [a]
                continue
            prev = current[-1]
            prev_end = prev.start_date + timedelta(
                seconds=prev.elapsed_time_s or prev.moving_time_s or 0
            )
            if a.start_date - prev_end <= GAP:
                current.append(a)
            else:
                sessions.append(current)
                current = [a]
        if current:
            sessions.append(current)

        my = next(
            (s for s in sessions if any(a.pk == primary.pk for a in s)), [primary]
        )
        p.matched_activities.add(*my)
        for a in my:
            used.add(a.id)


def _restore_fk(apps, schema_editor):
    """Reverso: devolve um único 'principal' (o mais longo) ao FK."""
    PlannedWorkout = apps.get_model("runner", "PlannedWorkout")
    for p in PlannedWorkout.objects.all():
        acts = list(p.matched_activities.all())
        if acts:
            p.matched_activity = max(acts, key=lambda a: a.distance_m)
            p.save(update_fields=["matched_activity"])


class Migration(migrations.Migration):

    dependencies = [
        ("runner", "0007_coachsuggestion_profile_coach_autonomy"),
    ]

    operations = [
        # 1) Cria o M2M (sem reverse_accessor ainda, p/ não colidir com o FK).
        migrations.AddField(
            model_name="plannedworkout",
            name="matched_activities",
            field=models.ManyToManyField(
                blank=True, related_name="+", to="runner.activity"
            ),
        ),
        # 2) Backfill: FK -> M2M, recolhendo os blocos órfãos do mesmo dia.
        migrations.RunPython(_backfill_sessions, _restore_fk),
        # 3) Remove o FK antigo.
        migrations.RemoveField(
            model_name="plannedworkout",
            name="matched_activity",
        ),
        # 4) Agora o M2M pode assumir o related_name definitivo.
        migrations.AlterField(
            model_name="plannedworkout",
            name="matched_activities",
            field=models.ManyToManyField(
                blank=True, related_name="planned_workouts", to="runner.activity"
            ),
        ),
    ]
