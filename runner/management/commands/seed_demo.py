"""
Cria dados de DEMONSTRAÇÃO para testar o ZayaRun localmente sem Strava.

Gera um treinador e um atleta (com histórico de corridas, plano de prova,
treinos casados, feedback e check-in) para que todas as telas — painel do
corredor, lado-treinador, plano, copiloto — apareçam populadas.

Uso:  python manage.py seed_demo
Login:  treinador / demo12345    e    atleta / demo12345
"""
import datetime as dt

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from runner.models import (
    Activity,
    CoachAthlete,
    DailyCheckin,
    PlannedWorkout,
    Profile,
    StravaToken,
    WorkoutFeedback,
)
from runner.services import matching, plans

PASSWORD = "demo12345"


class Command(BaseCommand):
    help = "Cria dados de demonstração (treinador + atleta) para testar local sem Strava."

    def handle(self, *args, **options):
        # Recria do zero (idempotente).
        User.objects.filter(username__in=["treinador", "atleta"]).delete()

        coach = User.objects.create_user("treinador", password=PASSWORD)
        athlete = User.objects.create_user("atleta", password=PASSWORD)
        Profile.objects.create(user=coach, is_coach=True, display_name="Treinador Demo")
        Profile.objects.create(user=athlete, display_name="Ana Corredora")
        CoachAthlete.objects.create(
            coach=coach, athlete=athlete, status=CoachAthlete.STATUS_ACTIVE,
            label="Ana Corredora", accepted_at=timezone.now(),
        )
        # Token fake só para a dashboard do atleta abrir populada (NÃO sincronizar).
        StravaToken.objects.create(
            user=athlete, athlete_id=1, access_token="demo", refresh_token="demo",
            expires_at=timezone.now() + dt.timedelta(days=3650),
            scope="read,activity:read_all",
        )

        today = timezone.localdate()
        this_monday = today - dt.timedelta(days=today.weekday())

        # 16 semanas de corridas, pace melhorando ~2s/sem, com cadência.
        week_template = [(1, 8, 30), (2, 6, -15), (4, 6, 25), (6, 14, 45)]  # (weekday, km, pace_off)
        ext = 0
        for wk in range(16, -1, -1):
            wmon = this_monday - dt.timedelta(weeks=wk)
            base_pace = 330 + wk * 2  # agora ~5:30/km; 16 sem atrás ~6:02/km
            for weekday, km, pace_off in week_template:
                day = wmon + dt.timedelta(days=weekday)
                if day > today:
                    continue
                pace = base_pace + pace_off
                dist = km * 1000
                moving = int(dist / 1000 * pace)
                start = timezone.make_aware(dt.datetime.combine(day, dt.time(7, 0)))
                ext += 1
                Activity.objects.create(
                    user=athlete, source=Activity.SOURCE_STRAVA, external_id=f"demo-{ext}",
                    name=f"Corrida {km} km", sport_type="Run", start_date=start,
                    distance_m=dist, moving_time_s=moving, elapsed_time_s=moving + 120,
                    total_elevation_gain_m=km * 6, average_speed_ms=1000.0 / pace,
                    average_heartrate=150 + (8 if km > 10 else 0),
                    max_heartrate=175, average_cadence=85 + (16 - wk) * 0.1,
                )

        # Splits com "fade" no último treino (demonstra perda de ritmo na 2ª metade).
        last = Activity.objects.filter(user=athlete).order_by("-start_date").first()
        if last:
            n = max(int(last.distance_km), 4)
            base = last.pace_seconds_per_km
            last.splits = [
                {
                    "km": i + 1, "distance_m": 1000,
                    "moving_time_s": int(base + (12 if i >= n / 2 else 0)),
                    "pace_seconds_per_km": round(base + (12 if i >= n / 2 else 0), 1),
                    "pace_str": "", "elevation_diff": 0, "average_heartrate": 155,
                }
                for i in range(n)
            ]
            last.save(update_fields=["splits", "synced_at"])

        # Plano de prova: meia maratona em 10 semanas (cria treinos desta semana em diante).
        race = today + dt.timedelta(weeks=10)
        spec = plans.generate_plan(list(athlete.activities.all()), 21_097, race)
        plans.materialize_plan(athlete, spec, created_by=coach, source=PlannedWorkout.SOURCE_COACH)

        # Treinos PASSADOS (antes desta semana) para demonstrar planejado × realizado.
        past_acts = athlete.activities.filter(
            start_date__lt=timezone.make_aware(dt.datetime.combine(this_monday, dt.time(0, 0))),
            start_date__gte=timezone.now() - dt.timedelta(days=35),
        )
        for a in past_acts:
            day = timezone.localtime(a.start_date).date()
            is_long = a.distance_km > 12
            PlannedWorkout.objects.create(
                athlete=athlete, created_by=coach, source=PlannedWorkout.SOURCE_COACH,
                date=day, workout_type="long" if is_long else "easy",
                title=("Longão" if is_long else "Rodagem") + f" {a.distance_km:.0f} km",
                target_distance_m=a.distance_m,
                target_pace_low_s=int(a.pace_seconds_per_km) - 10,
                target_pace_high_s=int(a.pace_seconds_per_km) + 10,
            )

        # Casa realizado x planejado (passado e semana atual).
        matching.reconcile(athlete)

        # Feedback (PSE) num treino realizado + check-in de hoje.
        done = (
            PlannedWorkout.objects.filter(
                athlete=athlete, status=PlannedWorkout.STATUS_COMPLETED
            ).order_by("-date").first()
        )
        if done:
            WorkoutFeedback.objects.create(
                athlete=athlete, planned_workout=done, activity=done.representative_activity,
                date=done.date, rpe=7, feeling="good", soreness=2,
            )
        DailyCheckin.objects.create(
            athlete=athlete, sleep_hours=7.0, sleep_quality=4, soreness=2, stress=2,
        )

        n_acts = athlete.activities.count()
        n_plan = athlete.planned_workouts.count()
        self.stdout.write(self.style.SUCCESS(
            f"Demo criada: {n_acts} corridas, {n_plan} treinos prescritos."
        ))
        self.stdout.write("  Treinador -> usuario 'treinador' / senha 'demo12345'")
        self.stdout.write("  Atleta    -> usuario 'atleta'    / senha 'demo12345'")
        self.stdout.write(self.style.WARNING(
            "  Obs.: o atleta usa um token Strava fake - NAO clique em 'Sincronizar'."
        ))
