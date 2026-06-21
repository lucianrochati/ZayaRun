from django.contrib import admin

from runner.models import (
    Activity,
    CoachAthlete,
    DailyCheckin,
    FitnessProfile,
    PlannedWorkout,
    Profile,
    StravaToken,
    TrainingPlan,
    WorkoutFeedback,
)


@admin.register(StravaToken)
class StravaTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "athlete_id", "expires_at", "updated_at")
    search_fields = ("user__username", "athlete_id")


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "user",
        "source",
        "sport_type",
        "start_date",
        "distance_km",
        "pace_str",
    )
    list_filter = ("source", "sport_type", "start_date")
    search_fields = ("name", "user__username", "external_id")
    date_hierarchy = "start_date"


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "is_coach", "created_at")
    list_filter = ("is_coach",)
    search_fields = ("user__username", "display_name")


@admin.register(CoachAthlete)
class CoachAthleteAdmin(admin.ModelAdmin):
    list_display = ("coach", "athlete", "label", "status", "invite_code", "invited_at")
    list_filter = ("status",)
    search_fields = ("coach__username", "athlete__username", "invite_code", "label")


@admin.register(TrainingPlan)
class TrainingPlanAdmin(admin.ModelAdmin):
    list_display = (
        "athlete",
        "goal_label",
        "goal_race_date",
        "status",
        "generated_by",
        "created_at",
    )
    list_filter = ("status", "generated_by")
    search_fields = ("athlete__username", "goal_label")
    date_hierarchy = "goal_race_date"


@admin.register(PlannedWorkout)
class PlannedWorkoutAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "athlete",
        "workout_type",
        "title",
        "status",
        "source",
        "matched_activity",
    )
    list_filter = ("status", "workout_type", "source")
    search_fields = ("athlete__username", "title")
    date_hierarchy = "date"
    raw_id_fields = ("matched_activity", "plan")


@admin.register(WorkoutFeedback)
class WorkoutFeedbackAdmin(admin.ModelAdmin):
    list_display = ("date", "athlete", "rpe", "feeling", "soreness")
    list_filter = ("feeling",)
    search_fields = ("athlete__username",)
    date_hierarchy = "date"


@admin.register(DailyCheckin)
class DailyCheckinAdmin(admin.ModelAdmin):
    list_display = ("date", "athlete", "sleep_hours", "soreness", "stress", "source")
    list_filter = ("source",)
    search_fields = ("athlete__username",)
    date_hierarchy = "date"


@admin.register(FitnessProfile)
class FitnessProfileAdmin(admin.ModelAdmin):
    list_display = (
        "athlete",
        "experience_level",
        "weekly_volume_km",
        "longest_run_km",
        "runs_per_week",
        "confidence",
        "computed_at",
    )
    list_filter = ("experience_level", "confidence", "volume_trend")
    search_fields = ("athlete__username",)
