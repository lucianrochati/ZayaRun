from django.contrib import admin

from runner.models import Activity, StravaToken


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
