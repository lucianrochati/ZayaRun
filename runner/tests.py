"""Testes das metricas de treinador (logica central do ZayaRun)."""
import os
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from runner.models import (
    Activity,
    CoachAthlete,
    DailyCheckin,
    FitnessProfile,
    PlannedWorkout,
    StravaToken,
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

    def test_session_pace_judged_on_work_block_not_average(self):
        """Limiar 2km leve + 8km no alvo + 1km solto: o pace médio (5:39) não
        pode zerar a nota — avalia-se o bloco forte (5:00), dentro do alvo."""
        from runner.models import Activity, RealizedSession

        planned = PlannedWorkout(
            workout_type="tempo",
            date=timezone.localdate(),
            target_distance_m=11_000,
            target_pace_low_s=294,   # 4:54
            target_pace_high_s=308,  # 5:08
        )
        base = timezone.now()
        blocks = [
            Activity(sport_type="Run", distance_m=2_000, moving_time_s=780,
                     start_date=base),                                   # 6:30 aquecimento
            Activity(sport_type="Run", distance_m=8_000, moving_time_s=2_400,
                     start_date=base + timedelta(minutes=15)),           # 5:00 no alvo
            Activity(sport_type="Run", distance_m=1_000, moving_time_s=420,
                     start_date=base + timedelta(minutes=50)),           # 7:00 solto
        ]
        adh = metrics.adherence(planned, RealizedSession(blocks))
        self.assertEqual(adh["distance_pct"], 100)
        self.assertEqual(adh["pace_eval"], "on")     # avaliou o bloco de 5:00
        self.assertEqual(adh["pace_str"], "5:00")    # exibe o pace de trabalho
        self.assertGreaterEqual(adh["score"], 85)    # não mais 50%
        # Card expande mostrando os blocos lidos, com o trecho forte marcado.
        self.assertEqual(len(adh["blocks"]), 3)
        work = [b for b in adh["blocks"] if b["is_work"]]
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]["km"], 8.0)
        self.assertTrue(adh["summary"])

    def test_summary_explains_score_in_plain_words(self):
        """A nota vem com uma frase simples (sem fórmula) do porquê."""
        planned = PlannedWorkout(
            workout_type="tempo", date=timezone.localdate(),
            target_distance_m=11_000, target_pace_low_s=294, target_pace_high_s=308,
        )
        # 11 km na distância, mas média 5:39/km (mais lenta que o alvo).
        adh = metrics.adherence(planned, make_run(11_000, 11 * 339))
        self.assertIn("distância", adh["summary"].lower())
        self.assertIn("mais lento", adh["summary"].lower())
        self.assertNotIn("blocks", adh)  # atividade única não lista blocos

    def test_easy_run_faster_than_ceiling_is_not_a_failure(self):
        """Fácil feito mais rápido que o teto não pode dar 'fora do alvo' (era 54%)."""
        planned = PlannedWorkout(
            workout_type="easy", date=timezone.localdate(),
            target_distance_m=8_000, target_pace_low_s=395, target_pace_high_s=410,  # 6:35–6:50
        )
        adh = metrics.adherence(planned, make_run(8_000, 8 * 365))  # 8 km a 6:05/km
        self.assertEqual(adh["distance_pct"], 100)
        self.assertEqual(adh["pace_eval"], "fast")
        self.assertEqual(adh["zone"], "ideal")              # verde, não "risco"
        self.assertGreaterEqual(adh["score"], 85)           # não mais 54%
        self.assertNotIn("trecho", adh["summary"].lower())  # fácil não tem "trecho forte"

    def test_easy_run_slower_than_ceiling_is_fine(self):
        """Fácil mais devagar que o teto é ok — não penaliza."""
        planned = PlannedWorkout(
            workout_type="easy", date=timezone.localdate(),
            target_distance_m=8_000, target_pace_low_s=395, target_pace_high_s=410,
        )
        adh = metrics.adherence(planned, make_run(8_000, 8 * 430))  # 7:10/km
        self.assertEqual(adh["pace_eval"], "slow")
        self.assertEqual(adh["score"], 100)

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
        self.assertIn(act.pk, planned.matched_activities.values_list("pk", flat=True))

    def test_split_session_sums_into_one_realizado(self):
        """Tiros feitos em 3 blocos (2+8+1 km) = realizado de 11 km no treino."""
        planned = PlannedWorkout.objects.create(
            athlete=self.user,
            date=timezone.localdate(),
            workout_type="interval",
            target_distance_m=11_000,
        )
        base = timezone.now() - timedelta(hours=2)
        # 2 km leve -> 8 km forte -> 1 km leve, encadeados (mesma sessão).
        self._run(external_id="s1", distance_m=2_000, moving_time_s=720,
                  start_date=base)
        self._run(external_id="s2", distance_m=8_000, moving_time_s=2_160,
                  start_date=base + timedelta(minutes=15))
        self._run(external_id="s3", distance_m=1_000, moving_time_s=360,
                  start_date=base + timedelta(minutes=50))

        matching.reconcile(self.user)

        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedWorkout.STATUS_COMPLETED)
        self.assertEqual(planned.matched_activities.count(), 3)
        self.assertEqual(planned.realized.distance_km, 11.0)
        # Aderência de distância bate o previsto (não mais 2 de 11 km).
        adh = metrics.adherence(planned, planned.realized)
        self.assertEqual(adh["distance_pct"], 100)

    def test_two_sessions_same_day_match_separate_plans(self):
        """Leve de manhã + tiros à tarde: cada sessão casa com seu treino."""
        today = timezone.localdate()
        easy = PlannedWorkout.objects.create(
            athlete=self.user, date=today, workout_type="easy",
            target_distance_m=5_000,
        )
        tiros = PlannedWorkout.objects.create(
            athlete=self.user, date=today, workout_type="interval",
            target_distance_m=10_000,
        )
        morning = timezone.now() - timedelta(hours=10)
        self._run(external_id="am", distance_m=5_000, moving_time_s=1_650,
                  start_date=morning)
        self._run(external_id="pm", distance_m=10_000, moving_time_s=2_700,
                  start_date=morning + timedelta(hours=8))  # gap > 4h = outra sessão

        matching.reconcile(self.user)

        easy.refresh_from_db(); tiros.refresh_from_db()
        self.assertEqual(easy.realized.distance_km, 5.0)
        self.assertEqual(tiros.realized.distance_km, 10.0)

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
        # Rodagens fáceis ~5:35–5:50, um 5k forte (~4:40) e um longão de 16 km.
        runs = [
            make_run(10_000, 10 * p, days_ago=3 * i + 2)
            for i, p in enumerate((340, 345, 335, 350, 340, 345))
        ]
        runs.append(make_run(5_000, 1_400, days_ago=4))      # 5k forte ~4:40
        runs.append(make_run(16_000, 16 * 360, days_ago=7))  # longão 16k ~6:00
        return runs

    def test_plan_spans_to_race_day(self):
        from runner.services import plans

        race_date = timezone.localdate() + timedelta(days=7 * 12 + 1)
        spec = plans.generate_plan(self._activities(), 10_000, race_date)
        self.assertGreaterEqual(spec["meta"]["weeks"], 10)
        race_wks = [w for w in spec["workouts"] if w["workout_type"] == "race"]
        self.assertEqual(len(race_wks), 1)
        self.assertEqual(race_wks[0]["date"], race_date)

    def test_plan_starts_now_even_for_distant_race(self):
        """Prova distante (>20 sem) não pode adiar o início: começa nesta semana."""
        from runner.services import plans

        today = timezone.localdate()
        race_date = today + timedelta(days=7 * 26 + 1)  # ~6 meses
        spec = plans.generate_plan(self._activities(), 10_000, race_date)
        first = min(w["date"] for w in spec["workouts"])
        self.assertGreaterEqual(first, today)                 # nunca no passado
        self.assertLess(first, today + timedelta(days=8))     # já nesta semana
        self.assertEqual(spec["meta"]["start_date"], first)   # meta reflete o real
        # E a prova continua ancorada no dia certo.
        race_wks = [w for w in spec["workouts"] if w["workout_type"] == "race"]
        self.assertEqual(race_wks[0]["date"], race_date)

    def test_km_coherent_across_title_desc_target(self):
        """O nº de km do alvo bate com título/descrição (sem 6 km × 5,6 km)."""
        from runner.services import plans

        race_date = timezone.localdate() + timedelta(days=7 * 12 + 1)
        spec = plans.generate_plan(self._activities(), 10_000, race_date)
        for w in spec["workouts"]:
            if w["workout_type"] == "race" or not w.get("target_distance_m"):
                continue
            km = round(w["target_distance_m"] / 1000.0, 1)
            km_txt = f"{km:.0f}" if abs(km - round(km)) < 0.05 else f"{km:.1f}"
            haystack = f"{w['title']} {w['description']}"
            self.assertIn(km_txt, haystack, msg=f"alvo {km_txt} ausente em: {haystack!r}")

    def test_strides_show_pace_and_zone_for_trained(self):
        """Atleta treinado: tiros em Z5 com pace-alvo explícito (coach cognitivo)."""
        from runner.models import Anamnese
        from runner.services import plans

        user = User.objects.create_user("trained", password="x")
        an = Anamnese.objects.create(
            athlete=user, sessions_per_week=4,
            available_days=[1, 2, 4, 6], preferred_long_day=6,
        )
        race = timezone.localdate() + timedelta(days=7 * 12 + 1)
        spec = plans.generate_plan(self._activities(), 10_000, race, anamnese=an)
        strides = [w for w in spec["workouts"] if w["workout_type"] == "strides"]
        self.assertTrue(strides)
        s = strides[0]
        self.assertIn("Z5", s["description"])          # zona forte liberada
        self.assertIn("~", s["description"])            # pace-alvo dos tiros ("~m:ss/km")
        self.assertTrue(s["structure"][0].get("target_pace_low_s"))

    def test_rest_seconds_on_rep_workouts(self):
        """Treinos com séries informam o descanso (s) entre repetições."""
        from runner.models import Anamnese
        from runner.services import plans

        user = User.objects.create_user("rest", password="x")
        an = Anamnese.objects.create(
            athlete=user, sessions_per_week=4,
            available_days=[1, 2, 4, 6], preferred_long_day=6,
        )
        race = timezone.localdate() + timedelta(days=7 * 14 + 1)
        spec = plans.generate_plan(self._activities(), 10_000, race, anamnese=an)
        keyed = [w for w in spec["workouts"] if w["workout_type"] in ("interval", "strides")]
        self.assertTrue(keyed)
        for w in keyed:
            st = w["structure"][0]
            self.assertGreater(st.get("recovery_s", 0), 0)         # descanso em segundos
            self.assertIn(plans._rest_txt(st["recovery_s"]), w["description"])  # aparece no texto
        # Treino contínuo (longão) não inventa descanso entre repetições.
        longs = [w for w in spec["workouts"] if w["workout_type"] == "long"]
        self.assertTrue(all("recovery_s" not in (w["structure"][0] if w["structure"] else {})
                            for w in longs))

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
        self.assertFalse(spec["meta"]["from_history"])  # sem dados

    def test_plan_is_driven_by_history(self):
        from runner.services import plans

        race_date = timezone.localdate() + timedelta(days=7 * 8)
        spec = plans.generate_plan(self._activities(), 10_000, race_date)
        self.assertTrue(spec["meta"]["from_history"])
        self.assertLessEqual(spec["meta"]["runs_per_week"], 4)   # respeita preset 10k
        self.assertGreaterEqual(spec["meta"]["runs_per_week"], 3)
        # o longão não estoura muito além do maior real (16 km) — teto sensato
        self.assertLessEqual(spec["meta"]["long_cap_km"], 22)


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
        planned.matched_activities.add(act)
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

    def test_topbar_shows_full_name(self):
        """O topo mostra o NOME do usuário logado (não só a inicial)."""
        from runner.models import Profile

        Profile.objects.update_or_create(
            user=self.user, defaults={"display_name": "Lucian Rochati"}
        )
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, "Lucian Rochati")

    def test_workout_purpose_explains_each_type(self):
        """Cada treino tem um propósito real (sai da caixa-preta)."""
        from runner.services import plans

        for t in ("long", "easy", "tempo", "interval", "strides", "race", "rest"):
            self.assertTrue(plans.workout_purpose(t), t)
        self.assertIn("aeróbica", plans.workout_purpose("long").lower())

    def test_plan_detail_shows_planned_vs_realized_and_purpose(self):
        """No card: Planejado X × Realizado Y (da Strava) + 'Por que este treino'."""
        from runner.models import Activity, PlannedWorkout, TrainingPlan

        plan = TrainingPlan.objects.create(
            athlete=self.user, goal_distance_m=10000,
            goal_race_date=timezone.localdate() + timedelta(days=40),
            start_date=timezone.localdate(), status=TrainingPlan.STATUS_ACTIVE,
        )
        act = Activity.objects.create(
            user=self.user, source=Activity.SOURCE_STRAVA, external_id="pr1",
            sport_type="Run", start_date=timezone.now(), distance_m=5000, moving_time_s=1650,
        )
        w = PlannedWorkout.objects.create(
            athlete=self.user, plan=plan, date=timezone.localdate(), workout_type="long",
            title="Longão 9 km", target_distance_m=9000,
            status=PlannedWorkout.STATUS_COMPLETED,
        )
        w.matched_activities.add(act)
        resp = self.client.get(reverse("plan_detail", args=[plan.id]))
        self.assertContains(resp, "Planejado")
        # Bloco "Realizado" só renderiza quando há atividade da Strava casada (5 km).
        self.assertContains(resp, "Realizado")
        self.assertContains(resp, "Por que este treino")    # propósito do longão

    def test_plan_detail_expands_session_breakdown(self):
        """Card 'Realizado' expande: blocos lidos + trecho forte + frase da nota."""
        from runner.models import Activity, PlannedWorkout, TrainingPlan

        plan = TrainingPlan.objects.create(
            athlete=self.user, goal_distance_m=11000,
            goal_race_date=timezone.localdate() + timedelta(days=40),
            start_date=timezone.localdate(), status=TrainingPlan.STATUS_ACTIVE,
        )
        w = PlannedWorkout.objects.create(
            athlete=self.user, plan=plan, date=timezone.localdate(), workout_type="tempo",
            title="Ritmo 11 km", target_distance_m=11000,
            target_pace_low_s=294, target_pace_high_s=308,
            status=PlannedWorkout.STATUS_COMPLETED,
        )
        base = timezone.now()
        specs = [("c1", 2000, 780, 0), ("c2", 8000, 2400, 15), ("c3", 1000, 420, 50)]
        for ext, dist, secs, mins in specs:
            a = Activity.objects.create(
                user=self.user, source=Activity.SOURCE_STRAVA, external_id=ext,
                sport_type="Run", start_date=base + timedelta(minutes=mins),
                distance_m=dist, moving_time_s=secs,
            )
            w.matched_activities.add(a)
        resp = self.client.get(reverse("plan_detail", args=[plan.id]))
        self.assertContains(resp, "O que a Zaya leu")
        self.assertContains(resp, "trecho forte")
        self.assertContains(resp, "3 atividades")

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

    def test_disconnect_purges_strava_data(self):
        StravaToken.objects.create(
            user=self.user, athlete_id=1, access_token="a", refresh_token="r",
            expires_at=timezone.now() + timedelta(days=1),
        )
        Activity.objects.create(
            user=self.user, source=Activity.SOURCE_STRAVA, external_id="z1",
            sport_type="Run", start_date=timezone.now(),
            distance_m=10_000, moving_time_s=3_000,
        )
        FitnessProfile.objects.create(athlete=self.user)
        self.client.post(reverse("strava_disconnect"))
        self.assertFalse(StravaToken.objects.filter(user=self.user).exists())
        self.assertFalse(Activity.objects.filter(user=self.user).exists())
        self.assertFalse(FitnessProfile.objects.filter(athlete=self.user).exists())

    def test_plan_crud(self):
        from runner.models import TrainingPlan
        from runner.services import plans

        race = timezone.localdate() + timedelta(days=7 * 6)
        spec = plans.generate_plan([], 10_000, race)
        plan = plans.materialize_plan(self.user, spec, created_by=self.user)

        # Ver o plano completo
        self.assertEqual(
            self.client.get(reverse("plan_detail", args=[plan.id])).status_code, 200
        )
        w = PlannedWorkout.objects.filter(plan=plan).first()

        # Editar um treino
        self.client.post(reverse("edit_workout", args=[w.id]), {
            "date": w.date.isoformat(), "workout_type": "tempo", "title": "Editado",
            "distance_km": "8", "pace": "4:50", "status": "completed",
        })
        w.refresh_from_db()
        self.assertEqual(w.title, "Editado")
        self.assertEqual(w.workout_type, "tempo")
        self.assertEqual(w.status, "completed")

        # Adicionar e remover um treino
        self.client.post(reverse("add_workout_to_plan", args=[plan.id]),
                         {"date": timezone.localdate().isoformat(), "workout_type": "easy"})
        wid = w.id
        self.client.post(reverse("delete_workout", args=[wid]))
        self.assertFalse(PlannedWorkout.objects.filter(pk=wid).exists())

        # Excluir o plano
        self.client.post(reverse("delete_plan", args=[plan.id]))
        self.assertFalse(TrainingPlan.objects.filter(pk=plan.id).exists())

    def test_complete_workout_toggle(self):
        """Concluir (1 toque) marca realizado; repetir reabre para planejado."""
        w = PlannedWorkout.objects.create(
            athlete=self.user, date=timezone.localdate(), workout_type="easy",
            status=PlannedWorkout.STATUS_PLANNED,
        )
        self.client.post(reverse("complete_workout", args=[w.id]))
        w.refresh_from_db()
        self.assertEqual(w.status, PlannedWorkout.STATUS_COMPLETED)
        self.client.post(reverse("complete_workout", args=[w.id]))
        w.refresh_from_db()
        self.assertEqual(w.status, PlannedWorkout.STATUS_PLANNED)

    def test_copilot_saves_and_caps_history(self):
        """A Zaya guarda o histórico e mostra as últimas 5 conversas."""
        from runner.models import CopilotChat

        with self.settings(INSIGHT_PROVIDER="rules"):
            self.client.post(reverse("copilot"), {"question": "Como está meu pace de 5 km?"})
            self.assertEqual(CopilotChat.objects.filter(athlete=self.user).count(), 1)
            resp = self.client.get(reverse("copilot"))
        self.assertContains(resp, "Como está meu pace de 5 km?")  # aparece no histórico

        for i in range(7):
            CopilotChat.objects.create(athlete=self.user, question=f"Q{i}", answer="ok")
        with self.settings(INSIGHT_PROVIDER="rules"):
            resp = self.client.get(reverse("copilot"))
        self.assertEqual(len(resp.context["history"]), 5)  # teto de 5

    def test_multiple_plans_with_one_active(self):
        """Atleta pode ter vários planos; só um ativo; dá para alternar."""
        from runner.models import TrainingPlan

        r1 = (timezone.localdate() + timedelta(days=56)).isoformat()
        r2 = (timezone.localdate() + timedelta(days=70)).isoformat()
        self.client.post(reverse("create_my_plan"), {"goal_distance_m": "10000", "race_date": r1})
        self.client.post(reverse("create_my_plan"), {"goal_distance_m": "21097", "race_date": r2})
        plans_qs = list(self.user.training_plans.order_by("created_at"))
        self.assertEqual(len(plans_qs), 2)
        p1, p2 = plans_qs
        self.assertEqual(p1.status, TrainingPlan.STATUS_INACTIVE)  # antigo arquivado
        self.assertEqual(p2.status, TrainingPlan.STATUS_ACTIVE)    # novo ativo
        self.assertTrue(p1.workouts.exists())                      # treinos preservados

        self.client.post(reverse("activate_plan", args=[p1.id]))   # volta pro p1
        p1.refresh_from_db(); p2.refresh_from_db()
        self.assertEqual(p1.status, TrainingPlan.STATUS_ACTIVE)
        self.assertEqual(p2.status, TrainingPlan.STATUS_INACTIVE)

    def test_cannot_manage_others_workout(self):
        other = User.objects.create_user("intruso", password="x")
        w = PlannedWorkout.objects.create(
            athlete=other, date=timezone.localdate(), workout_type="easy"
        )
        self.assertEqual(self.client.get(reverse("edit_workout", args=[w.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("delete_workout", args=[w.id])).status_code, 404)

    def test_anamnese_form_and_save(self):
        from runner.models import Anamnese

        self.assertEqual(self.client.get(reverse("edit_anamnese")).status_code, 200)
        self.client.post(reverse("edit_anamnese"), {
            "available_days": ["1", "3", "5"], "sessions_per_week": "3",
            "preferred_long_day": "6", "has_pain_now": "on", "injury_history": "canelite",
        })
        an = Anamnese.objects.get(athlete=self.user)
        self.assertEqual(an.available_days, [1, 3, 5])
        self.assertTrue(an.has_pain_now)


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class StravaLoginTests(TestCase):
    """Login COM Strava: criação/reuso de usuário a partir do atleta (sem rede)."""

    def _data(self, athlete_id, first="João", last="Silva"):
        return {
            "access_token": "a", "refresh_token": "r",
            "expires_at": int(timezone.now().timestamp()) + 3600, "scope": "read",
            "athlete": {"id": athlete_id, "firstname": first, "lastname": last},
        }

    def test_creates_user_from_athlete(self):
        from runner.services import strava

        user = strava.get_or_create_user_from_athlete(self._data(999))
        self.assertEqual(user.username, "strava_999")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.profile.display_name, "João Silva")

    def test_is_idempotent(self):
        from runner.services import strava

        u1 = strava.get_or_create_user_from_athlete(self._data(777))
        u2 = strava.get_or_create_user_from_athlete(self._data(777))
        self.assertEqual(u1.pk, u2.pk)

    def test_reuses_user_with_existing_token(self):
        from runner.models import StravaToken
        from runner.services import strava

        u = User.objects.create_user("existente", password="x")
        StravaToken.objects.create(
            user=u, athlete_id=555, access_token="a", refresh_token="r",
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(strava.get_or_create_user_from_athlete(self._data(555)).pk, u.pk)

    def test_login_page_has_strava_button(self):
        resp = Client().get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("strava_login"))


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


class FitnessProfileTests(TestCase):
    """Perfil de aptidão derivado do histórico (services.fitness)."""

    def setUp(self):
        self.user = User.objects.create_user("fp", password="x")

    def _seed(self, specs):
        for i, (dist_m, moving_s, ago) in enumerate(specs):
            Activity.objects.create(
                user=self.user, source=Activity.SOURCE_STRAVA, external_id=f"fp{i}",
                sport_type="Run", start_date=timezone.now() - timedelta(days=ago),
                distance_m=dist_m, moving_time_s=moving_s,
            )

    def test_none_without_runs(self):
        from runner.services import fitness

        self.assertIsNone(fitness.compute_profile([]))

    def test_extracts_history_signals(self):
        from runner.services import fitness

        specs = []
        for wk in range(12):
            specs.append((10_000, 3_300, wk * 7 + 1))
            specs.append((8_000, 2_640, wk * 7 + 3))
            if wk % 2 == 0:
                specs.append((18_000, 6_480, wk * 7 + 6))
        specs.append((5_000, 1_320, 5))  # 5k forte (4:24)
        self._seed(specs)
        p = fitness.compute_profile(list(self.user.activities.all()))
        self.assertIsNotNone(p)
        self.assertGreaterEqual(p["longest_run_km"], 17)
        self.assertGreaterEqual(p["runs_per_week"], 2)
        self.assertIsNotNone(p["easy_pace_s"])
        self.assertIsNotNone(p["threshold_pace_s"])
        self.assertIn(p["experience_level"], {"beginner", "intermediate", "advanced"})
        self.assertEqual(p["confidence"], "high")

    def test_update_profile_persists(self):
        from runner.services import fitness

        self._seed([(10_000, 3_300, 2), (8_000, 2_640, 5), (12_000, 4_200, 9)])
        prof = fitness.update_profile(self.user)
        self.assertIsNotNone(prof)
        self.assertTrue(FitnessProfile.objects.filter(athlete=self.user).exists())
        self.assertGreater(prof.weekly_volume_km, 0)


class ScheduleTests(TestCase):
    """Agenda da semana: respeita dias e espaça os treinos-chave."""

    def test_hard_days_never_adjacent_default(self):
        from runner.services import plans

        sched = plans.weekly_schedule([0, 1, 2, 3, 4, 5, 6], 4, 6)
        self.assertFalse(plans.hard_days_adjacent(sched))

    def test_respects_available_days(self):
        from runner.services import plans

        sched = plans.weekly_schedule([0, 2, 4, 6], 4, 6)  # seg/qua/sex/dom
        self.assertEqual([wd for wd, _ in sched], [0, 2, 4, 6])
        self.assertFalse(plans.hard_days_adjacent(sched))

    def test_long_day_is_honored(self):
        from runner.services import plans

        sched = plans.weekly_schedule([1, 2, 3, 5], 3, 5)  # longão no sábado
        self.assertEqual([wd for wd, r in sched if r == "long"], [5])


class AnamneseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("an", password="x")

    def test_plan_uses_anamnese_days(self):
        from runner.models import Anamnese
        from runner.services import plans

        an = Anamnese.objects.create(
            athlete=self.user, sessions_per_week=4,
            available_days=[0, 2, 4, 6], preferred_long_day=6,
        )
        race = timezone.localdate() + timedelta(days=7 * 8)
        spec = plans.generate_plan([], 10_000, race, anamnese=an)
        wkdays = {w["weekday"] for w in spec["workouts"] if w["workout_type"] != "race"}
        self.assertTrue(wkdays.issubset({0, 2, 4, 6}))
        self.assertTrue(spec["meta"]["from_anamnese"])

    def test_pain_triggers_conservative_and_warning(self):
        from runner.models import Anamnese
        from runner.services import plans

        an = Anamnese.objects.create(
            athlete=self.user, available_days=[1, 3, 5],
            has_pain_now=True, pain_where="joelho",
        )
        race = timezone.localdate() + timedelta(days=7 * 10)
        spec = plans.generate_plan([], 21_097, race, anamnese=an)
        self.assertTrue(spec["meta"]["conservative"])
        self.assertTrue(any("dor" in w.lower() for w in spec["meta"]["safety_warnings"]))

    def test_conservative_plan_avoids_z5(self):
        """Blindagem: dor/lesão (conservador) não prescreve trabalho forte (Z5)."""
        from runner.models import Anamnese
        from runner.services import plans

        an = Anamnese.objects.create(
            athlete=self.user, sessions_per_week=4,
            available_days=[1, 2, 4, 6], preferred_long_day=6,
            has_pain_now=True, pain_where="joelho",
        )
        race = timezone.localdate() + timedelta(days=7 * 12)
        spec = plans.generate_plan([], 10_000, race, anamnese=an)
        self.assertTrue(spec["meta"]["conservative"])
        blob = " ".join(w["description"] for w in spec["workouts"])
        self.assertNotIn("Z5", blob)            # iniciante/conservador não recebe Z5
        self.assertNotIn("Intervalado", " ".join(w["title"] for w in spec["workouts"]))


class CopilotContextTests(TestCase):
    """O copiloto recebe a LISTA real de corridas (datas/distância/pace)."""

    def test_context_lists_real_runs_with_dates(self):
        runs = [
            make_run(5000, 1650, days_ago=3),    # 5 km
            make_run(8000, 2760, days_ago=10),   # 8 km
            make_run(5000, 1700, days_ago=17),   # 5 km
            make_run(8000, 2800, days_ago=24),   # 8 km
        ]
        ctx = insights.build_copilot_context(runs)
        self.assertEqual(ctx["runs_count"], 4)
        first = ctx["runs"][0]
        for key in ("data", "distancia_km", "pace_km"):
            self.assertIn(key, first)
        self.assertRegex(first["data"], r"^\d{4}-\d{2}-\d{2}$")          # data ISO
        dists = sorted({round(r["distancia_km"]) for r in ctx["runs"]})
        self.assertEqual(dists, [5, 8])    # 5 km e 8 km distinguíveis (não se misturam)

    def test_context_none_without_runs(self):
        self.assertIsNone(insights.build_copilot_context([]))


class AwarenessTests(TestCase):
    """Zaya consciente: padrão do atleta + sinais fora do comum (heurística honesta)."""

    def test_signals_flag_volume_drop_and_layoff(self):
        from runner.services import insights

        # Baseline frequente (8–34 dias atrás) e NADA nos últimos 7 dias.
        runs = [make_run(10_000, 3_000, days_ago=a)
                for a in (8, 9, 11, 13, 15, 16, 18, 20, 22, 24, 26, 28, 30, 33)]
        kinds = {s["kind"] for s in insights.behavioral_signals(runs)}
        self.assertIn("volume_baixo", kinds)   # 0 km nos últimos 7 dias vs média
        self.assertIn("parado", kinds)         # dias sem correr acima do normal

    def test_pattern_present_in_context(self):
        from runner.services import insights

        runs = [make_run(8_000, 2_640, days_ago=2 * i + 1) for i in range(8)]
        ctx = insights.build_copilot_context(runs)
        self.assertIsNotNone(ctx.get("padrao"))
        self.assertIn("sessoes_por_semana", ctx["padrao"])

    def test_no_signals_with_thin_history(self):
        from runner.services import insights

        self.assertEqual(insights.behavioral_signals([make_run(5_000, 1_650)]), [])


class CopilotMemoryTests(TestCase):
    """A Zaya lembra das conversas anteriores (continuidade, não-robótica)."""

    def setUp(self):
        self.user = User.objects.create_user("mem", password="x")
        Activity.objects.create(
            user=self.user, source=Activity.SOURCE_STRAVA, external_id="r1",
            sport_type="Run", start_date=timezone.now() - timedelta(days=2),
            distance_m=8_000, moving_time_s=2_640,
        )

    def test_prompt_includes_prior_conversation(self):
        from runner.models import CopilotChat
        from runner.services import coaching

        CopilotChat.objects.create(
            athlete=self.user, question="Pergunta antiga ABC", answer="Resposta antiga XYZ"
        )
        captured = {}

        def fake_complete(system, user_text, **kw):
            captured["u"] = user_text
            return "ok"

        with mock.patch.object(coaching.ai, "is_enabled", return_value=True), \
                mock.patch.object(coaching.ai, "complete", side_effect=fake_complete):
            out = coaching.answer_question(self.user, "Pergunta atual DEF")
        self.assertEqual(out, "ok")
        self.assertIn("Pergunta antiga ABC", captured["u"])   # memória no prompt
        self.assertIn("Pergunta atual DEF", captured["u"])


class MultiPlanGatingTests(TestCase):
    """Múltiplos planos: só o ATIVO conta no dia a dia (calendário + matching)."""

    def setUp(self):
        self.user = User.objects.create_user("gate", password="x")

    def _plan(self, status, dist=10000, days=40):
        from runner.models import TrainingPlan

        return TrainingPlan.objects.create(
            athlete=self.user, goal_distance_m=dist,
            goal_race_date=timezone.localdate() + timedelta(days=days),
            start_date=timezone.localdate(), status=status,
        )

    def test_live_filter_excludes_inactive_plan(self):
        from runner.models import PlannedWorkout, TrainingPlan, live_planned_filter

        active = self._plan(TrainingPlan.STATUS_ACTIVE)
        inactive = self._plan(TrainingPlan.STATUS_INACTIVE, dist=21097, days=60)
        d = timezone.localdate() + timedelta(days=1)
        wa = PlannedWorkout.objects.create(athlete=self.user, plan=active, date=d, workout_type="easy")
        wi = PlannedWorkout.objects.create(athlete=self.user, plan=inactive, date=d, workout_type="easy")
        wad = PlannedWorkout.objects.create(athlete=self.user, date=d, workout_type="easy")  # avulso
        ids = set(
            PlannedWorkout.objects.filter(live_planned_filter(), athlete=self.user)
            .values_list("id", flat=True)
        )
        self.assertIn(wa.id, ids)       # plano ativo
        self.assertIn(wad.id, ids)      # avulso (sem plano)
        self.assertNotIn(wi.id, ids)    # plano guardado fica de fora

    def test_matching_ignores_inactive_plan(self):
        from runner.models import Activity, PlannedWorkout, TrainingPlan
        from runner.services import matching

        inactive = self._plan(TrainingPlan.STATUS_INACTIVE)
        when = timezone.now()
        w = PlannedWorkout.objects.create(
            athlete=self.user, plan=inactive, date=when.date(), workout_type="easy"
        )
        act = Activity.objects.create(
            user=self.user, source=Activity.SOURCE_STRAVA, external_id="m1",
            sport_type="Run", start_date=when, distance_m=5000, moving_time_s=1650,
        )
        self.assertIsNone(matching.match_activity_to_plan(act))  # não casa
        w.refresh_from_db()
        self.assertEqual(w.matched_activities.count(), 0)


class AutoregTests(TestCase):
    """Autorregulação: por padrão SUGERE (não impõe); respeita a autonomia do atleta."""

    def setUp(self):
        self.user = User.objects.create_user("auto", password="x")

    def _plan_with_upcoming(self):
        from runner.models import PlannedWorkout, TrainingPlan

        plan = TrainingPlan.objects.create(
            athlete=self.user, goal_distance_m=10000,
            goal_race_date=timezone.localdate() + timedelta(days=40),
            start_date=timezone.localdate(), status=TrainingPlan.STATUS_ACTIVE,
        )
        d = timezone.localdate()
        hard = PlannedWorkout.objects.create(
            athlete=self.user, plan=plan, date=d + timedelta(days=1),
            workout_type="interval", title="Intervalado 5x1000 m", target_distance_m=8000,
        )
        easy = PlannedWorkout.objects.create(
            athlete=self.user, plan=plan, date=d + timedelta(days=2),
            workout_type="easy", title="Rodagem 6 km", target_distance_m=6000,
        )
        return plan, hard, easy

    def _feedback(self, **kw):
        from runner.models import WorkoutFeedback

        defaults = dict(athlete=self.user, date=timezone.localdate(),
                        rpe=9, feeling="bad", soreness=4)
        defaults.update(kw)
        return WorkoutFeedback.objects.create(**defaults)

    def _set_autonomy(self, value):
        from runner.models import Profile

        Profile.objects.update_or_create(user=self.user, defaults={"coach_autonomy": value})

    def test_default_only_suggests_does_not_touch_plan(self):
        """Postura padrão: SUGERE; nunca muda o treino sem o atleta aceitar."""
        from runner.models import CoachSuggestion
        from runner.services import autoreg

        plan, hard, easy = self._plan_with_upcoming()
        out = autoreg.autoregulate(self.user, self._feedback(), None)
        self.assertEqual(out["mode"], "suggest")
        self.assertFalse(out["applied"])
        self.assertIsNotNone(out["suggestion"])
        hard.refresh_from_db(); easy.refresh_from_db()
        self.assertEqual(hard.workout_type, "interval")    # NÃO mexeu
        self.assertEqual(easy.target_distance_m, 6000)     # NÃO reduziu
        self.assertEqual(
            CoachSuggestion.objects.filter(athlete=self.user, status="pending").count(), 1
        )

    def test_accept_applies_the_adjustment(self):
        from runner.services import autoreg

        plan, hard, easy = self._plan_with_upcoming()
        out = autoreg.autoregulate(self.user, self._feedback(), None)
        autoreg.accept_suggestion(out["suggestion"])
        hard.refresh_from_db(); easy.refresh_from_db()
        self.assertEqual(hard.workout_type, "easy")        # só agora vira leve
        self.assertLess(easy.target_distance_m, 6000)
        out["suggestion"].refresh_from_db()
        self.assertEqual(out["suggestion"].status, "accepted")

    def test_decline_keeps_plan_and_records_decision(self):
        from runner.services import autoreg

        plan, hard, easy = self._plan_with_upcoming()
        out = autoreg.autoregulate(self.user, self._feedback(), None)
        autoreg.decline_suggestion(out["suggestion"])
        hard.refresh_from_db()
        self.assertEqual(hard.workout_type, "interval")    # mantido (respeita o atleta)
        out["suggestion"].refresh_from_db()
        self.assertEqual(out["suggestion"].status, "declined")

    def test_auto_mode_applies_and_notes_plan(self):
        from runner.services import autoreg

        self._set_autonomy("auto")
        plan, hard, easy = self._plan_with_upcoming()
        out = autoreg.autoregulate(self.user, self._feedback(), None)
        self.assertEqual(out["mode"], "auto")
        self.assertTrue(out["applied"])
        hard.refresh_from_db()
        self.assertEqual(hard.workout_type, "easy")
        plan.refresh_from_db()
        self.assertIn("avaliação", plan.notes.lower())     # dor alta → recomenda profissional

    def test_off_mode_does_nothing(self):
        from runner.services import autoreg

        self._set_autonomy("off")
        plan, hard, easy = self._plan_with_upcoming()
        out = autoreg.autoregulate(self.user, self._feedback(), None)
        self.assertEqual(out["mode"], "off")
        hard.refresh_from_db()
        self.assertEqual(hard.workout_type, "interval")

    def test_positive_feedback_changes_nothing(self):
        from runner.services import autoreg

        plan, hard, easy = self._plan_with_upcoming()
        out = autoreg.autoregulate(self.user, self._feedback(rpe=4, feeling="good", soreness=1), None)
        self.assertEqual(out["mode"], "none")
        hard.refresh_from_db()
        self.assertEqual(hard.workout_type, "interval")

    def test_declined_suggestions_become_awareness_signal(self):
        """Ignorar sugestões alimenta a consciência da Zaya (sinal honesto)."""
        from runner.models import CoachSuggestion
        from runner.services import insights

        for _ in range(2):
            CoachSuggestion.objects.create(
                athlete=self.user, level="reduce", reasons=["x"],
                summary="s", status=CoachSuggestion.STATUS_DECLINED,
            )
        runs = [make_run(8_000, 2_640, days_ago=2 * i + 1) for i in range(6)]
        kinds = {s["kind"] for s in insights.behavioral_signals(runs, athlete=self.user)}
        self.assertIn("mantem_treino", kinds)


class AIProviderTests(TestCase):
    """Resolução de provedor de IA (gratuitos Groq > Gemini > Claude > regras)."""

    NO_KEYS = {"GROQ_API_KEY": "", "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""}

    def test_no_keys_falls_back_to_rules(self):
        from runner.services import ai

        with mock.patch.dict(os.environ, self.NO_KEYS), self.settings(AI_PROVIDER="auto"):
            self.assertIsNone(ai.provider())
            self.assertFalse(ai.is_enabled())

    def test_gemini_key_enables_free_provider(self):
        from runner.services import ai

        with mock.patch.dict(os.environ, {**self.NO_KEYS, "GEMINI_API_KEY": "k"}), \
                self.settings(AI_PROVIDER="auto"):
            self.assertEqual(ai.provider(), "gemini")
            self.assertTrue(ai.is_enabled())

    def test_groq_preferred_in_auto(self):
        """Groq (grátis, sem cartão) vem antes do Gemini no modo auto."""
        from runner.services import ai

        with mock.patch.dict(os.environ, {**self.NO_KEYS, "GROQ_API_KEY": "g", "GEMINI_API_KEY": "k"}), \
                self.settings(AI_PROVIDER="auto"):
            self.assertEqual(ai.provider(), "groq")

    def test_explicit_provider_respected(self):
        from runner.services import ai

        with mock.patch.dict(os.environ, {**self.NO_KEYS, "GROQ_API_KEY": "g", "GEMINI_API_KEY": "k"}), \
                self.settings(AI_PROVIDER="gemini"):
            self.assertEqual(ai.provider(), "gemini")

    def test_rules_setting_disables_even_with_key(self):
        from runner.services import ai

        with mock.patch.dict(os.environ, {**self.NO_KEYS, "GROQ_API_KEY": "g"}), \
                self.settings(AI_PROVIDER="rules"):
            self.assertIsNone(ai.provider())

    def test_groq_request_without_key(self):
        """Sem chave, groq_request devolve (None, motivo) sem levantar exceção."""
        from runner.services import ai

        with mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            text, debug = ai.groq_request("sys", "oi", max_tokens=10)
            self.assertIsNone(text)
            self.assertIn("GROQ_API_KEY", debug)


class PWATests(TestCase):
    """PWA: service worker em escopo raiz + banner de instalação."""

    def test_service_worker_served_at_root(self):
        resp = Client().get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("javascript", resp["Content-Type"])
        self.assertEqual(resp["Service-Worker-Allowed"], "/")
        self.assertIn(b"addEventListener", resp.content)  # corpo real do SW

    def test_install_banner_present(self):
        resp = Client().get(reverse("login"))
        self.assertContains(resp, "pwaInstall")          # banner existe
        self.assertContains(resp, "/sw.js")              # registro do SW


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class CycleAwareTests(TestCase):
    """Treino ciente do ciclo — PRIVADO da atleta, sempre pergunta (nunca decreta)."""

    def setUp(self):
        from runner.models import CycleProfile, Profile

        self.user = User.objects.create_user("ciclista", password="x")
        Profile.objects.create(
            user=self.user, sex=Profile.SEX_FEMALE, sex_confirmed=True,
        )
        self.prof = CycleProfile.objects.create(
            athlete=self.user, enabled=True, avg_cycle_length=28, avg_period_length=5,
        )

    def _period(self, days_ago):
        from runner.models import CycleEvent

        return CycleEvent.objects.create(
            athlete=self.user,
            start_date=timezone.localdate() - timedelta(days=days_ago),
        )

    # --- Previsão de fase ---
    def test_phase_menstrual_then_follicular(self):
        from runner.services import cycle

        self._period(2)  # começou há 2 dias -> hoje é dia 3
        ph = cycle.phase_for(self.user)
        self.assertEqual(ph["phase"], "menstrual")
        self.assertEqual(ph["day"], 3)
        later = cycle.phase_for(self.user, on_date=timezone.localdate() + timedelta(days=8))
        self.assertEqual(later["phase"], "follicular")

    def test_contraception_disables_phase(self):
        from runner.services import cycle

        self._period(2)
        self.prof.on_contraception = True
        self.prof.save()
        ph = cycle.phase_for(self.user)
        self.assertIsNone(ph["phase"])
        self.assertTrue(ph["on_contraception"])

    def test_no_event_or_disabled_gives_no_phase(self):
        from runner.services import cycle

        self.assertIsNone(cycle.phase_for(self.user))  # sem registro
        self._period(2)
        self.prof.enabled = False
        self.prof.save()
        self.assertIsNone(cycle.phase_for(self.user))  # sem opt-in

    def test_learned_cycle_length_from_history(self):
        from runner.services import cycle

        for d in (62, 32, 2):  # menstruações a cada 30 dias
            self._period(d)
        self.assertEqual(cycle.learned_cycle_length(self.user), 30)

    # --- Saúde (RED-S) ---
    def test_red_s_flag_after_90_days(self):
        from runner.models import Anamnese
        from runner.services import cycle

        Anamnese.objects.create(athlete=self.user, age=30)
        self._period(100)
        flag = cycle.red_s_flag(self.user)
        self.assertIsNotNone(flag)
        self.assertGreaterEqual(flag["days"], 90)
        # Dentro de 90 dias -> sem flag.
        self.user.cycle_events.all().delete()
        self._period(40)
        self.assertIsNone(cycle.red_s_flag(self.user))

    def test_red_s_skipped_on_contraception(self):
        from runner.models import Anamnese
        from runner.services import cycle

        Anamnese.objects.create(athlete=self.user, age=30)
        self._period(100)
        self.prof.on_contraception = True
        self.prof.save()
        self.assertIsNone(cycle.red_s_flag(self.user))

    # --- Oferta (pergunta, não decreta) ---
    def test_guidance_offers_on_heavy_phase_and_stops_when_fine(self):
        from runner.models import CycleSymptom
        from runner.services import cycle

        self._period(1)  # menstrual (fase que costuma pesar)
        key = PlannedWorkout.objects.create(
            athlete=self.user, date=timezone.localdate(),
            workout_type="interval", target_distance_m=10_000,
        )
        g = cycle.guidance(self.user, key)
        self.assertIsNotNone(g)
        self.assertEqual(g["kind"], "offer")
        # Ela diz que está bem -> a Zaya não insiste.
        CycleSymptom.objects.create(athlete=self.user, date=timezone.localdate(), energy=4)
        self.assertIsNone(cycle.guidance(self.user, key))

    # --- Ações iniciadas pela atleta ---
    def test_soften_today_lightens_and_does_not_leak_cycle(self):
        from runner.services import cycle

        w = PlannedWorkout.objects.create(
            athlete=self.user, date=timezone.localdate(), workout_type="tempo",
            target_distance_m=10_000, target_pace_low_s=300, target_pace_high_s=315,
            title="Limiar 10 km",
        )
        self.assertEqual(cycle.soften_today(self.user), 1)
        w.refresh_from_db()
        self.assertEqual(w.workout_type, "easy")
        self.assertIsNone(w.target_pace_low_s)
        self.assertLess(w.target_distance_m, 10_000)
        self.assertNotIn("ciclo", w.title.lower())  # privacidade: treinador vê o plano

    def test_skip_today_marks_skipped(self):
        from runner.services import cycle

        w = PlannedWorkout.objects.create(
            athlete=self.user, date=timezone.localdate(), workout_type="easy",
            target_distance_m=6_000,
        )
        cycle.skip_today(self.user)
        w.refresh_from_db()
        self.assertEqual(w.status, PlannedWorkout.STATUS_SKIPPED)

    # --- Privacidade na prontidão ---
    def test_readiness_cycle_factor_is_private(self):
        from runner.services import wellness

        self._period(1)  # menstrual (pesada) -> sinal de ciclo
        r_priv = wellness.readiness(self.user, activities=[], include_private=True)
        self.assertIsNotNone(r_priv)
        self.assertIn("ciclo", " ".join(r_priv["factors"]).lower())
        # Sem include_private (qualquer contexto que NÃO seja a própria atleta):
        # nada de ciclo. Aqui sem outros sinais, vira None — e isso já garante
        # que o fator de ciclo não aparece.
        r_pub = wellness.readiness(self.user, activities=[], include_private=False)
        if r_pub:
            self.assertNotIn("ciclo", " ".join(r_pub["factors"]).lower())
        else:
            self.assertIsNone(r_pub)

    # --- Periodização consultiva ---
    def test_outlook_tags_weeks(self):
        from runner.services import cycle

        self._period(1)
        outlook = cycle.upcoming_phase_outlook(self.user, weeks=4)
        self.assertTrue(outlook)
        self.assertIn(outlook[0]["tag"], ("forte", "leve", "neutra"))

    # --- Views (athlete-only) ---
    def test_my_plan_renders_cycle_card(self):
        self.client.force_login(self.user)
        self._period(2)
        resp = self.client.get(reverse("my_plan"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Seu ciclo")

    def test_cycle_log_view_records_period_and_symptom(self):
        from runner.models import CycleSymptom

        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("cycle_log"), {"period_started": "1", "energy": "3"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.user.cycle_events.exists())
        self.assertTrue(CycleSymptom.objects.filter(athlete=self.user).exists())

    def test_cycle_action_adapta_lightens_today(self):
        self.client.force_login(self.user)
        self._period(1)
        w = PlannedWorkout.objects.create(
            athlete=self.user, date=timezone.localdate(),
            workout_type="interval", target_distance_m=10_000,
        )
        resp = self.client.post(reverse("cycle_action"), {"action": "adapta"})
        self.assertEqual(resp.status_code, 302)
        w.refresh_from_db()
        self.assertEqual(w.workout_type, "easy")

    def test_coach_view_never_shows_cycle(self):
        """Garantia de privacidade: a página do treinador não expõe nada de ciclo."""
        from runner.models import CoachAthlete, CycleSymptom, Profile

        self._period(1)
        CycleSymptom.objects.create(athlete=self.user, date=timezone.localdate(), energy=1)
        coach = User.objects.create_user("treina", password="x")
        Profile.objects.create(user=coach, is_coach=True)
        CoachAthlete.objects.create(
            coach=coach, athlete=self.user, status=CoachAthlete.STATUS_ACTIVE,
        )
        self.client.force_login(coach)
        resp = self.client.get(reverse("coach_athlete", args=[self.user.id]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode().lower()
        # Marcadores específicos da feature de ciclo — nunca no lado-treinador.
        # (Evita casar "macrociclo", que é legítimo no contexto de plano.)
        self.assertNotIn("seu ciclo", body)
        self.assertNotIn("menstru", body)
        self.assertNotIn("anticoncep", body)

    # --- Sexo: dica da Strava, confirmação no login, gate da feature ---
    def test_sex_hint_from_strava_does_not_override_confirmation(self):
        from runner.models import Profile
        from runner.services import strava

        strava._capture_sex_hint(self.user, "M")  # já é F confirmado
        self.assertEqual(Profile.objects.get(user=self.user).sex, "F")

    def test_sex_hint_prefills_when_unconfirmed(self):
        from runner.models import Profile
        from runner.services import strava

        u = User.objects.create_user("novata", password="x")
        Profile.objects.create(user=u)
        strava._capture_sex_hint(u, "F")
        p = Profile.objects.get(user=u)
        self.assertEqual(p.sex, "F")
        self.assertFalse(p.sex_confirmed)

    def test_confirm_sex_view_sets_and_confirms(self):
        from runner.models import Profile

        u = User.objects.create_user("confirma", password="x")
        Profile.objects.create(user=u)
        c = Client()
        c.force_login(u)
        resp = c.post(reverse("confirm_sex"), {"sex": "F"})
        self.assertEqual(resp.status_code, 302)
        p = Profile.objects.get(user=u)
        self.assertEqual(p.sex, "F")
        self.assertTrue(p.sex_confirmed)

    def test_cycle_hidden_for_men(self):
        from runner.models import Profile

        man = User.objects.create_user("corredor", password="x")
        Profile.objects.create(user=man, sex=Profile.SEX_MALE, sex_confirmed=True)
        c = Client()
        c.force_login(man)
        resp = c.get(reverse("my_plan"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Seu ciclo")
        self.assertNotContains(resp, "Se você menstrua")

    def test_dashboard_prompts_sex_confirmation(self):
        from runner.models import Profile

        u = User.objects.create_user("pednova", password="x")
        Profile.objects.create(user=u, sex="F")  # dica, ainda não confirmada
        c = Client()
        c.force_login(u)
        resp = c.get(reverse("dashboard"))
        self.assertContains(resp, "Confirma pra personalizar")
