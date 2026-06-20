"""Testes das metricas de treinador (logica central do ZayaRun)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from runner.models import (
    Activity,
    CoachAthlete,
    DailyCheckin,
    PlannedWorkout,
    WorkoutFeedback,
)
from runner.services import insights, matching, metrics


def make_run(distance_m, moving_time_s, days_ago=0, sport_type="Run", cadence=None):
    """Cria uma Activity em memoria (sem salvar) para testar metricas puras."""
    return Activity(
        source=Activity.SOURCE_STRAVA,
        external_id=f"x{distance_m}{moving_time_s}{days_ago}",
        sport_type=sport_type,
        start_date=timezone.now() - timedelta(days=days_ago),
        distance_m=distance_m,
        moving_time_s=moving_time_s,
        average_cadence=cadence,
    )


class PaceTests(TestCase):
    def test_pace_str_5min_per_km(self):
        run = make_run(10_000, 3_000)  # 10km em 50min => 5:00/km
        self.assertEqual(run.pace_str, "5:00")

    def test_pace_seconds(self):
        run = make_run(5_000, 1_500)  # 5km em 25min => 5:00/km => 300s
        self.assertAlmostEqual(run.pace_seconds_per_km, 300.0)

    def test_format_pace_rounds_seconds(self):
        self.assertEqual(metrics.format_pace(305.6), "5:06")

    def test_zero_distance_is_safe(self):
        run = make_run(0, 100)
        self.assertEqual(run.pace_str, "--")


class SummaryTests(TestCase):
    def test_summary_totals(self):
        runs = [make_run(5_000, 1_500), make_run(10_000, 3_000)]
        s = metrics.summary(runs)
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["total_distance_km"], 15.0)
        self.assertEqual(s["avg_pace_str"], "5:00")

    def test_only_runs_filters_bikes(self):
        items = [make_run(5_000, 1_500), make_run(20_000, 2_400, sport_type="Ride")]
        s = metrics.summary(items)
        self.assertEqual(s["count"], 1)


class TrendTests(TestCase):
    def test_improvement_detected(self):
        # 5 corridas antigas lentas, 5 recentes rapidas.
        runs = []
        for i in range(5):
            runs.append(make_run(10_000, 3_600, days_ago=40 - i))  # 6:00/km
        for i in range(5):
            runs.append(make_run(10_000, 3_000, days_ago=10 - i))  # 5:00/km
        trend = metrics.evolution_trend(runs)
        self.assertIsNotNone(trend)
        self.assertTrue(trend["improved"])
        self.assertLess(trend["delta_seconds"], 0)


class RaceProjectionTests(TestCase):
    def test_riegel_projection_marathon(self):
        # 10km em 50min (5:00/km). Riegel deve prever maratona plausivel.
        runs = [make_run(10_000, 3_000)]
        race = metrics.race_projections(runs)
        labels = [p["label"] for p in race["projections"]]
        self.assertIn("42 km (maratona)", labels)
        marathon = next(p for p in race["projections"] if "42" in p["label"])
        # T = 3000 * (42195/10000)^1.06 ~ 13780s ~ 3h49min
        self.assertRegex(marathon["time_str"], r"3h\d{2}min")

    def test_no_runs_returns_empty(self):
        self.assertEqual(metrics.race_projections([]), [])


class PeriodFilterTests(TestCase):
    def test_week_filters_old_runs(self):
        runs = [make_run(5_000, 1_500, days_ago=2), make_run(5_000, 1_500, days_ago=40)]
        self.assertEqual(len(metrics.filter_by_period(runs, "week")), 1)

    def test_year_keeps_recent_year(self):
        runs = [make_run(5_000, 1_500, days_ago=100), make_run(5_000, 1_500, days_ago=400)]
        self.assertEqual(len(metrics.filter_by_period(runs, "year")), 1)

    def test_all_keeps_everything(self):
        runs = [make_run(5_000, 1_500, days_ago=d) for d in (1, 100, 1000)]
        self.assertEqual(len(metrics.filter_by_period(runs, "all")), 3)

    def test_invalid_period_defaults_to_month(self):
        self.assertEqual(metrics.normalize_period("xpto"), "month")


class VolumeSeriesTests(TestCase):
    def test_week_has_seven_daily_buckets(self):
        series = metrics.volume_series([make_run(5_000, 1_500, days_ago=1)], "week")
        self.assertEqual(len(series), 7)
        self.assertEqual(sum(b["km"] for b in series), 5.0)

    def test_year_has_twelve_monthly_buckets(self):
        series = metrics.volume_series([make_run(10_000, 3_000, days_ago=5)], "year")
        self.assertEqual(len(series), 12)


class CadenceTests(TestCase):
    def test_cadence_spm_doubles_rpm(self):
        run = make_run(10_000, 3_000, cadence=88)  # 88 rpm => 176 spm
        self.assertEqual(run.cadence_spm, 176)

    def test_summary_average_cadence(self):
        runs = [make_run(10_000, 3_000, cadence=85), make_run(10_000, 3_000, cadence=89)]
        self.assertEqual(metrics.summary(runs)["avg_cadence_spm"], 174)  # média 87 rpm => 174

    def test_evolution_series_marks_cadence_presence(self):
        with_cad = metrics.evolution_series([make_run(10_000, 3_000, cadence=85)])
        self.assertTrue(with_cad["has_cadence"])
        without = metrics.evolution_series([make_run(10_000, 3_000)])
        self.assertFalse(without["has_cadence"])


class PersonalRecordsTests(TestCase):
    def test_best_effort_per_distance(self):
        runs = [
            make_run(5_000, 1_500, days_ago=30),  # 5k em 25:00
            make_run(5_100, 1_440, days_ago=2),   # 5k mais rapido (24:00) e recente
            make_run(10_000, 3_000, days_ago=10),  # 10k
        ]
        prs = metrics.personal_records(runs)
        labels = {r["label"]: r for r in prs}
        self.assertIn("5 km", labels)
        self.assertIn("10 km", labels)
        # o melhor 5k deve ser o de 24:00 e marcado como recente
        self.assertEqual(labels["5 km"]["time_str"], "24min00s")
        self.assertTrue(labels["5 km"]["is_recent"])

    def test_distance_outside_band_ignored(self):
        prs = metrics.personal_records([make_run(3_000, 900)])  # 3k nao vira PR de 5k
        self.assertEqual(prs, [])


class InsightTests(TestCase):
    def test_no_runs_returns_none(self):
        self.assertIsNone(insights.daily_insight([]))

    def test_rules_insight_mentions_load(self):
        runs = [make_run(10_000, 3_000, days_ago=d) for d in (1, 3, 9, 16, 23)]
        with self.settings(INSIGHT_PROVIDER="rules"):
            insight = insights.daily_insight(runs)
        self.assertIsNotNone(insight)
        self.assertEqual(insight["source"], "análise")
        self.assertTrue(insight["body"])  # tem ao menos uma frase

    def test_split_fade_detected(self):
        run = make_run(10_000, 3_000)
        run.splits = [
            {"pace_seconds_per_km": 290},
            {"pace_seconds_per_km": 295},
            {"pace_seconds_per_km": 320},
            {"pace_seconds_per_km": 330},
        ]
        ctx = insights.build_context([run])
        self.assertIsNotNone(ctx["last_run"]["fade_pct"])
        self.assertGreater(ctx["last_run"]["fade_pct"], 4)


class ACWRTests(TestCase):
    def test_ideal_zone(self):
        # Carga estavel: ~10km/semana nas ultimas 4 semanas.
        runs = [make_run(10_000, 3_000, days_ago=d) for d in (2, 9, 16, 23)]
        result = metrics.acwr(runs)
        self.assertIsNotNone(result)
        self.assertEqual(result["zone"], "ideal")


class AdherenceTests(TestCase):
    def test_distance_and_pace_on_target(self):
        planned = PlannedWorkout(
            workout_type="long",
            date=timezone.localdate(),
            target_distance_m=10_000,
            target_pace_low_s=300,
            target_pace_high_s=315,
        )
        act = make_run(10_000, 3_060)  # 10km a 5:06/km
        adh = metrics.adherence(planned, act)
        self.assertGreaterEqual(adh["score"], 85)
        self.assertEqual(adh["zone"], "ideal")
        self.assertEqual(adh["pace_eval"], "on")

    def test_short_and_slow_is_flagged(self):
        planned = PlannedWorkout(
            workout_type="long",
            date=timezone.localdate(),
            target_distance_m=20_000,
            target_pace_low_s=300,
            target_pace_high_s=300,
        )
        act = make_run(10_000, 3_600)  # metade da distancia, 6:00/km (lento)
        adh = metrics.adherence(planned, act)
        self.assertLess(adh["score"], 85)
        self.assertEqual(adh["pace_eval"], "slow")
        self.assertTrue(any("abaixo" in n.lower() for n in adh["notes"]))

    def test_no_activity_returns_none(self):
        planned = PlannedWorkout(workout_type="easy", date=timezone.localdate())
        self.assertIsNone(metrics.adherence(planned, None))

    def test_no_targets_counts_as_completed(self):
        planned = PlannedWorkout(workout_type="easy", date=timezone.localdate())
        adh = metrics.adherence(planned, make_run(6_000, 1_800))
        self.assertEqual(adh["score"], 100)


class MatchingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ath", password="x")

    def _run(self, **kw):
        defaults = dict(
            user=self.user,
            source=Activity.SOURCE_STRAVA,
            external_id=f"act{Activity.objects.count()}",
            sport_type="Run",
            start_date=timezone.now(),
            distance_m=10_000,
            moving_time_s=3_000,
        )
        defaults.update(kw)
        return Activity.objects.create(**defaults)

    def test_activity_matches_same_day_plan(self):
        planned = PlannedWorkout.objects.create(
            athlete=self.user,
            date=timezone.localdate(),
            workout_type="long",
            target_distance_m=10_000,
        )
        act = self._run()
        matched = matching.match_activity_to_plan(act)
        self.assertEqual(matched.pk, planned.pk)
        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedWorkout.STATUS_COMPLETED)
        self.assertEqual(planned.matched_activity_id, act.pk)

    def test_rest_day_is_never_matched(self):
        PlannedWorkout.objects.create(
            athlete=self.user, date=timezone.localdate(), workout_type="rest"
        )
        self.assertIsNone(matching.match_activity_to_plan(self._run()))

    def test_mark_missed_past_planned(self):
        p = PlannedWorkout.objects.create(
            athlete=self.user,
            date=timezone.localdate() - timedelta(days=2),
            workout_type="easy",
        )
        matching.mark_missed_workouts(self.user)
        p.refresh_from_db()
        self.assertEqual(p.status, PlannedWorkout.STATUS_MISSED)


class PlanGeneratorTests(TestCase):
    def _activities(self):
        # ~base de treino com melhor 10k a 5:00/km
        return [make_run(10_000, 3_000, days_ago=d) for d in (2, 5, 9, 12, 16, 19, 23, 26)]

    def test_plan_spans_to_race_day(self):
        from runner.services import plans

        race_date = timezone.localdate() + timedelta(days=7 * 12 + 1)
        spec = plans.generate_plan(self._activities(), 10_000, race_date)
        self.assertGreaterEqual(spec["meta"]["weeks"], 10)
        race_wks = [w for w in spec["workouts"] if w["workout_type"] == "race"]
        self.assertEqual(len(race_wks), 1)
        self.assertEqual(race_wks[0]["date"], race_date)

    def test_volumes_positive_and_taper(self):
        from runner.services import plans

        race_date = timezone.localdate() + timedelta(days=7 * 10)
        spec = plans.generate_plan(self._activities(), 21_097, race_date)
        self.assertTrue(all(w["volume_km"] > 0 for w in spec["weeks"]))
        self.assertLess(spec["weeks"][-1]["volume_km"], spec["meta"]["peak_km"])

    def test_easy_pace_slower_than_goal(self):
        from runner.services import plans

        race_date = timezone.localdate() + timedelta(days=7 * 8)
        spec = plans.generate_plan(self._activities(), 10_000, race_date, goal_time_s=3000)
        easy = [w for w in spec["workouts"] if w["workout_type"] == "easy"]
        self.assertTrue(easy)
        self.assertTrue(all(w["target_pace_low_s"] > 300 for w in easy))

    def test_materialize_creates_rows(self):
        from runner.models import TrainingPlan
        from runner.services import plans

        user = User.objects.create_user("planuser", password="x")
        race_date = timezone.localdate() + timedelta(days=7 * 6)
        spec = plans.generate_plan([], 5_000, race_date)  # sem dados => baixa confiança
        plan = plans.materialize_plan(user, spec, created_by=user)
        self.assertTrue(TrainingPlan.objects.filter(pk=plan.pk).exists())
        self.assertEqual(
            PlannedWorkout.objects.filter(plan=plan).count(), len(spec["workouts"])
        )
        self.assertFalse(spec["meta"]["confident"])


class CoachingTests(TestCase):
    """Camada de IA nos caminhos de REGRAS (sem chamar a API)."""

    def setUp(self):
        from runner.models import CoachAthlete

        self.coach = User.objects.create_user("coach", password="x")
        self.ath = User.objects.create_user("athlete1", password="x")
        CoachAthlete.objects.create(
            coach=self.coach, athlete=self.ath, status=CoachAthlete.STATUS_ACTIVE
        )

    def _seed_runs(self, user, n=6):
        for d in range(n):
            Activity.objects.create(
                user=user, source=Activity.SOURCE_STRAVA, external_id=f"{user.pk}-{d}",
                sport_type="Run", start_date=timezone.now() - timedelta(days=d * 3 + 1),
                distance_m=10_000, moving_time_s=3_000,
            )

    def test_auto_prescribe_creates_week(self):
        from runner.services import coaching

        self._seed_runs(self.ath)
        with self.settings(INSIGHT_PROVIDER="rules"):
            count, note = coaching.auto_prescribe(self.ath, created_by=self.coach)
        self.assertGreaterEqual(count, 3)
        self.assertTrue(
            PlannedWorkout.objects.filter(
                athlete=self.ath, source=PlannedWorkout.SOURCE_AI
            ).exists()
        )
        self.assertIn("km", note)

    def test_triage_returns_card_for_athlete(self):
        from runner.services import coaching

        with self.settings(INSIGHT_PROVIDER="rules"):
            cards = coaching.triage_roster(self.coach)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["athlete"], self.ath)
        self.assertIn(cards[0]["flag"], {"new", "idle", "risk", "attention", "ok", "celebrate"})

    def test_answer_question_without_key_is_graceful(self):
        from runner.services import coaching

        self._seed_runs(self.ath)
        with self.settings(INSIGHT_PROVIDER="rules"):
            ans = coaching.answer_question(self.ath, "Como está minha evolução?")
        self.assertIsInstance(ans, str)
        self.assertGreater(len(ans), 10)

    def test_analyze_workout_rules_path(self):
        from runner.services import coaching

        planned = PlannedWorkout.objects.create(
            athlete=self.ath, date=timezone.localdate(), workout_type="long",
            target_distance_m=10_000, target_pace_low_s=300, target_pace_high_s=315,
        )
        act = Activity.objects.create(
            user=self.ath, source=Activity.SOURCE_STRAVA, external_id="match1",
            sport_type="Run", start_date=timezone.now(),
            distance_m=10_000, moving_time_s=3_060,
        )
        planned.matched_activity = act
        planned.save()
        with self.settings(INSIGHT_PROVIDER="rules"):
            res = coaching.analyze_workout(planned)
        self.assertIsNotNone(res)
        self.assertIn("adherence", res)
        self.assertEqual(res["source"], "análise")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ViewSmokeTests(TestCase):
    """Renderiza as páginas novas (pega erros de template/URL)."""

    def setUp(self):
        self.user = User.objects.create_user("runner1", password="pw12345!")
        self.client.force_login(self.user)

    def test_core_pages_render(self):
        for name in ("dashboard", "my_plan", "accept_invite"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        with self.settings(INSIGHT_PROVIDER="rules"):
            self.assertEqual(self.client.get(reverse("copilot")).status_code, 200)
            self.assertEqual(self.client.get(reverse("coach_dashboard")).status_code, 200)

    def test_coach_flow_end_to_end(self):
        self.client.post(reverse("become_coach"))
        with self.settings(INSIGHT_PROVIDER="rules"):
            self.assertEqual(self.client.get(reverse("coach_dashboard")).status_code, 200)
        self.client.post(reverse("coach_invite"), {"label": "Atleta X"})
        link = CoachAthlete.objects.get(coach=self.user)

        athlete = User.objects.create_user("ath2", password="x")
        other = Client()
        other.force_login(athlete)
        other.post(reverse("accept_invite"), {"code": link.invite_code})
        link.refresh_from_db()
        self.assertEqual(link.status, CoachAthlete.STATUS_ACTIVE)

        with self.settings(INSIGHT_PROVIDER="rules"):
            self.assertEqual(
                self.client.get(reverse("coach_athlete", args=[athlete.id])).status_code, 200
            )
        self.client.post(reverse("prescribe_workout", args=[athlete.id]), {
            "date": timezone.localdate().isoformat(), "workout_type": "long",
            "distance_km": "12", "pace": "5:30", "title": "Longão",
        })
        self.assertEqual(PlannedWorkout.objects.filter(athlete=athlete).count(), 1)

        race = (timezone.localdate() + timedelta(days=70)).isoformat()
        self.client.post(reverse("create_plan", args=[athlete.id]),
                         {"goal_distance_m": "10000", "race_date": race})
        self.assertTrue(athlete.training_plans.exists())

    def test_self_plan_feedback_checkin(self):
        race = (timezone.localdate() + timedelta(days=56)).isoformat()
        self.client.post(reverse("create_my_plan"),
                         {"goal_distance_m": "21097", "race_date": race})
        self.assertTrue(self.user.training_plans.exists())

        pw = PlannedWorkout.objects.filter(athlete=self.user).first()
        self.client.post(reverse("workout_feedback", args=[pw.id]),
                         {"rpe": "6", "feeling": "good"})
        self.assertTrue(WorkoutFeedback.objects.filter(planned_workout=pw).exists())

        self.client.post(reverse("daily_checkin"), {"sleep_hours": "7.5", "soreness": "2"})
        self.assertTrue(DailyCheckin.objects.filter(athlete=self.user).exists())
        # Renderiza o plano já com prontidão/check-in preenchidos.
        self.assertEqual(self.client.get(reverse("my_plan")).status_code, 200)


class WellnessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("well", password="x")

    def test_readiness_none_without_any_signal(self):
        from runner.services import wellness

        self.assertIsNone(wellness.readiness(self.user, activities=[]))

    def test_readiness_penalized_by_poor_checkin(self):
        from runner.services import wellness

        DailyCheckin.objects.create(
            athlete=self.user, soreness=5, sleep_quality=1, stress=5
        )
        r = wellness.readiness(self.user, activities=[])
        self.assertIsNotNone(r)
        self.assertLess(r["score"], 75)
        self.assertEqual(r["method"], "heuristic")

    def test_injury_risk_declares_heuristic(self):
        from runner.services import wellness

        DailyCheckin.objects.create(athlete=self.user, soreness=4)
        risk = wellness.predict_injury_risk(self.user, activities=[])
        self.assertIsNotNone(risk)
        self.assertEqual(risk["method"], "heuristic")
        self.assertGreaterEqual(risk["risk"], 0)
