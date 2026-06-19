"""Modelos do ZayaRun: tokens das integracoes e atividades sincronizadas."""
from django.conf import settings
from django.db import models
from django.utils import timezone


class StravaToken(models.Model):
    """Tokens OAuth da Strava para um usuario (um por usuario)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="strava_token",
    )
    athlete_id = models.BigIntegerField(null=True, blank=True)
    access_token = models.CharField(max_length=255)
    refresh_token = models.CharField(max_length=255)
    expires_at = models.DateTimeField()
    scope = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Strava de {self.user} (atleta {self.athlete_id})"

    @property
    def is_expired(self):
        # Margem de 60s para evitar usar token quase vencido.
        return timezone.now() >= (self.expires_at - timezone.timedelta(seconds=60))


class Activity(models.Model):
    """Uma atividade (corrida, bike, etc.) sincronizada de uma integracao."""

    SOURCE_STRAVA = "strava"
    SOURCE_GARMIN = "garmin"
    SOURCE_CHOICES = [
        (SOURCE_STRAVA, "Strava"),
        (SOURCE_GARMIN, "Garmin"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    source = models.CharField(
        max_length=16, choices=SOURCE_CHOICES, default=SOURCE_STRAVA
    )
    external_id = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True, default="")
    sport_type = models.CharField(max_length=64, blank=True, default="")
    start_date = models.DateTimeField()

    distance_m = models.FloatField(default=0)  # metros
    moving_time_s = models.IntegerField(default=0)  # segundos
    elapsed_time_s = models.IntegerField(default=0)  # segundos
    total_elevation_gain_m = models.FloatField(default=0)
    average_speed_ms = models.FloatField(default=0)  # m/s
    max_speed_ms = models.FloatField(default=0)
    average_heartrate = models.FloatField(null=True, blank=True)
    max_heartrate = models.FloatField(null=True, blank=True)
    average_cadence = models.FloatField(null=True, blank=True)  # RPM (1 perna)

    # Splits por km (lista de dicts vinda da API; preenchido sob demanda).
    splits = models.JSONField(default=list, blank=True)

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="unique_source_activity"
            )
        ]
        indexes = [models.Index(fields=["user", "-start_date"])]

    def __str__(self):
        return f"{self.name or self.sport_type} - {self.start_date:%d/%m/%Y}"

    # --- Propriedades de conveniencia (calculos sao centralizados em services.metrics) ---
    @property
    def distance_km(self):
        return self.distance_m / 1000.0

    @property
    def pace_seconds_per_km(self):
        """Pace em segundos por km (usa tempo em movimento)."""
        if self.distance_m <= 0 or self.moving_time_s <= 0:
            return 0
        return self.moving_time_s / (self.distance_m / 1000.0)

    @property
    def pace_str(self):
        """Pace formatado como m:ss /km."""
        secs = self.pace_seconds_per_km
        if not secs:
            return "--"
        minutes = int(secs // 60)
        seconds = int(round(secs % 60))
        if seconds == 60:
            minutes, seconds = minutes + 1, 0
        return f"{minutes}:{seconds:02d}"

    @property
    def duration_str(self):
        total = self.moving_time_s
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}min"
        return f"{m}min{s:02d}s"

    @property
    def cadence_spm(self):
        """Cadencia em passos por minuto (Strava reporta RPM de 1 perna)."""
        if not self.average_cadence:
            return None
        return round(self.average_cadence * 2)

    @property
    def is_run(self):
        return "run" in (self.sport_type or "").lower()
