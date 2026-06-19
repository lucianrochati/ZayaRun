"""Testes das metricas de treinador (logica central do ZayaRun)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from runner.models import Activity
from runner.services import metrics


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


class ACWRTests(TestCase):
    def test_ideal_zone(self):
        # Carga estavel: ~10km/semana nas ultimas 4 semanas.
        runs = [make_run(10_000, 3_000, days_ago=d) for d in (2, 9, 16, 23)]
        result = metrics.acwr(runs)
        self.assertIsNotNone(result)
        self.assertEqual(result["zone"], "ideal")
