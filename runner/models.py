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


# ==========================================================================
# Lado-treinador (assessoria): perfil, relacao coach<->atleta, prescricao,
# feedback subjetivo e plano de prova. Tudo opcional — o lado-corredor
# (Activity/StravaToken acima) continua funcionando sozinho.
# ==========================================================================
def _gen_invite_code():
    """Codigo de convite curto, legivel, sem caracteres ambiguos."""
    import secrets

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem I,O,0,1
    return "".join(secrets.choice(alphabet) for _ in range(6))


# Dias da semana (0=segunda ... 6=domingo) para anamnese e agenda de treino.
WEEKDAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def weekday_labels(days):
    return [WEEKDAYS[d] for d in sorted(days) if 0 <= d <= 6]


class Profile(models.Model):
    """Perfil do usuario: papel (atleta/treinador) e parametros fisiologicos."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    is_coach = models.BooleanField(default=False)
    display_name = models.CharField(max_length=120, blank=True, default="")
    # Usados para zonas/zonas-alvo e leitura de wellness (futuro Garmin).
    resting_hr = models.IntegerField(null=True, blank=True)
    max_hr = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        papel = "treinador" if self.is_coach else "atleta"
        return f"{self.display_name or self.user.get_username()} ({papel})"

    @property
    def name(self):
        return self.display_name or self.user.get_username()


class CoachAthlete(models.Model):
    """Vinculo entre um treinador e um atleta (com aceite por codigo)."""

    STATUS_INVITED = "invited"
    STATUS_ACTIVE = "active"
    STATUS_INACTIVE = "inactive"
    STATUS_CHOICES = [
        (STATUS_INVITED, "Convidado"),
        (STATUS_ACTIVE, "Ativo"),
        (STATUS_INACTIVE, "Inativo"),
    ]

    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coaching_links",
    )
    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_links",
        null=True,
        blank=True,
    )
    # Apelido que o coach da ao convite antes do atleta entrar.
    label = models.CharField(max_length=120, blank=True, default="")
    invite_code = models.CharField(
        max_length=12, unique=True, default=_gen_invite_code, db_index=True
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_INVITED
    )
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-invited_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["coach", "athlete"],
                name="unique_coach_athlete",
                condition=models.Q(athlete__isnull=False),
            )
        ]

    def __str__(self):
        who = self.athlete.get_username() if self.athlete else f"convite {self.invite_code}"
        return f"{self.coach.get_username()} -> {who}"

    def accept(self, athlete):
        self.athlete = athlete
        self.status = self.STATUS_ACTIVE
        self.accepted_at = timezone.now()
        self.save(update_fields=["athlete", "status", "accepted_at"])


# Tipos de treino prescrito (vocabulario de treinador).
WORKOUT_TYPES = [
    ("easy", "Regenerativo / leve"),
    ("long", "Longão"),
    ("tempo", "Ritmo / limiar"),
    ("interval", "Intervalado / tiros"),
    ("fartlek", "Fartlek"),
    ("strides", "Educativos / progressivo"),
    ("race", "Prova"),
    ("rest", "Descanso"),
    ("cross", "Cross-training"),
]
WORKOUT_TYPE_KEYS = {k for k, _ in WORKOUT_TYPES}


class TrainingPlan(models.Model):
    """Macrociclo: do estado atual ate uma prova-alvo, em semanas."""

    STATUS_ACTIVE = "active"
    STATUS_DONE = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Ativo"),
        (STATUS_DONE, "Concluído"),
        (STATUS_CANCELLED, "Cancelado"),
    ]

    GEN_RULES = "rules"
    GEN_AI = "ai"

    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="training_plans",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_plans",
    )
    goal_distance_m = models.FloatField()  # 5000, 10000, 21097, 42195...
    goal_label = models.CharField(max_length=64, blank=True, default="")
    goal_race_date = models.DateField()
    goal_time_s = models.IntegerField(null=True, blank=True)  # meta de tempo (opcional)
    start_date = models.DateField()
    weekly_base_km = models.FloatField(default=0)  # volume semanal inicial
    peak_weekly_km = models.FloatField(default=0)  # pico planejado
    generated_by = models.CharField(max_length=8, default=GEN_RULES)
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_ACTIVE
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Plano {self.goal_label or self.goal_distance_m} de {self.athlete.get_username()}"

    @property
    def weeks_to_race(self):
        return max(0, (self.goal_race_date - timezone.localdate()).days // 7)


class PlannedWorkout(models.Model):
    """Treino prescrito para um atleta em uma data (o 'planejado')."""

    SOURCE_COACH = "coach"
    SOURCE_AI = "ai"
    SOURCE_SELF = "self"
    SOURCE_CHOICES = [
        (SOURCE_COACH, "Treinador"),
        (SOURCE_AI, "IA"),
        (SOURCE_SELF, "Próprio atleta"),
    ]

    STATUS_PLANNED = "planned"
    STATUS_COMPLETED = "completed"
    STATUS_MISSED = "missed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_PLANNED, "Planejado"),
        (STATUS_COMPLETED, "Realizado"),
        (STATUS_MISSED, "Perdido"),
        (STATUS_SKIPPED, "Dispensado"),
    ]

    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="planned_workouts",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prescribed_workouts",
    )
    plan = models.ForeignKey(
        TrainingPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workouts",
    )
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default=SOURCE_COACH)
    date = models.DateField()
    workout_type = models.CharField(max_length=16, choices=WORKOUT_TYPES, default="easy")
    title = models.CharField(max_length=160, blank=True, default="")
    description = models.TextField(blank=True, default="")

    target_distance_m = models.FloatField(null=True, blank=True)
    target_duration_s = models.IntegerField(null=True, blank=True)
    # Faixa de pace-alvo em segundos por km (lo = mais rapido).
    target_pace_low_s = models.IntegerField(null=True, blank=True)
    target_pace_high_s = models.IntegerField(null=True, blank=True)
    # Estrutura de series, ex: [{"reps":6,"distance_m":800,"pace_s":240,"recovery":"200m trote"}]
    structure = models.JSONField(default=list, blank=True)

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PLANNED
    )
    # Atividade da Strava que cumpriu este treino (preenchido pelo matching).
    matched_activity = models.ForeignKey(
        Activity,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_workouts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date"]
        indexes = [models.Index(fields=["athlete", "date"])]

    def __str__(self):
        return f"{self.get_workout_type_display()} {self.date:%d/%m} - {self.athlete.get_username()}"

    @property
    def target_distance_km(self):
        if not self.target_distance_m:
            return None
        km = round(self.target_distance_m / 1000.0, 1)
        return int(km) if km == int(km) else km  # 9 em vez de 9.0; 8.9 fica 8.9

    @property
    def target_pace_str(self):
        from runner.services import metrics

        lo, hi = self.target_pace_low_s, self.target_pace_high_s
        if lo and hi and lo != hi:
            return f"{metrics.format_pace(lo)}–{metrics.format_pace(hi)}"
        single = lo or hi
        return metrics.format_pace(single) if single else None

    @property
    def is_key_workout(self):
        """Treino-chave (quali): longão, ritmo, intervalado, prova."""
        return self.workout_type in ("long", "tempo", "interval", "fartlek", "race")


class WorkoutFeedback(models.Model):
    """Percepcao subjetiva do atleta sobre um treino (PSE + sensacao)."""

    FEELING_CHOICES = [
        ("great", "Ótimo 😀"),
        ("good", "Bem 🙂"),
        ("ok", "Ok 😐"),
        ("tired", "Cansado 😕"),
        ("bad", "Mal 😣"),
    ]

    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workout_feedbacks",
    )
    planned_workout = models.OneToOneField(
        PlannedWorkout,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="feedback",
    )
    activity = models.ForeignKey(
        Activity,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="feedbacks",
    )
    date = models.DateField(default=timezone.localdate)
    rpe = models.IntegerField(null=True, blank=True)  # PSE 1-10 (esforco percebido)
    feeling = models.CharField(max_length=8, choices=FEELING_CHOICES, blank=True, default="")
    soreness = models.IntegerField(null=True, blank=True)  # dor muscular 1-5
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"Feedback {self.date:%d/%m} PSE={self.rpe} - {self.athlete.get_username()}"


class DailyCheckin(models.Model):
    """
    Check-in diario de prontidao (sono/dor/estresse). Campos de HRV e FC de
    repouso ficam aqui para receber dados do Garmin no futuro — hoje sao
    preenchidos manualmente (ou ficam nulos). NUNCA inventamos esses numeros.
    """

    SOURCE_MANUAL = "manual"
    SOURCE_GARMIN = "garmin"

    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="checkins",
    )
    date = models.DateField(default=timezone.localdate)
    sleep_hours = models.FloatField(null=True, blank=True)
    sleep_quality = models.IntegerField(null=True, blank=True)  # 1-5
    soreness = models.IntegerField(null=True, blank=True)  # 1-5
    stress = models.IntegerField(null=True, blank=True)  # 1-5
    # Wellness objetivo (futuro Garmin Health API). Nulo enquanto nao houver fonte.
    resting_hr = models.IntegerField(null=True, blank=True)
    hrv_ms = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=8, default=SOURCE_MANUAL)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["athlete", "date"], name="unique_athlete_checkin_day"
            )
        ]

    def __str__(self):
        return f"Check-in {self.date:%d/%m} - {self.athlete.get_username()}"


class FitnessProfile(models.Model):
    """
    Retrato de aptidao DERIVADO de todo o historico de corridas do atleta.
    Atualizado a cada sync (services.fitness). E o que o gerador de plano
    consulta para montar um treino baseado no que o atleta REALMENTE fez.
    """

    LEVEL_CHOICES = [
        ("beginner", "Iniciante"),
        ("intermediate", "Intermediário"),
        ("advanced", "Avançado"),
    ]
    TREND_CHOICES = [
        ("building", "Em evolução"),
        ("stable", "Estável"),
        ("detraining", "Destreino"),
    ]
    CONFIDENCE_CHOICES = [
        ("low", "Baixa"),
        ("medium", "Média"),
        ("high", "Alta"),
    ]

    athlete = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fitness_profile",
    )
    computed_at = models.DateTimeField(auto_now=True)

    weeks_of_data = models.IntegerField(default=0)
    activities_count = models.IntegerField(default=0)
    weekly_volume_km = models.FloatField(default=0)        # mediana recente (robusta)
    volume_trend = models.CharField(max_length=16, default="stable")
    runs_per_week = models.IntegerField(default=0)
    longest_run_km = models.FloatField(default=0)          # maior longão recente
    peak_weekly_km = models.FloatField(default=0)          # pico histórico
    # Zonas de pace (segundos/km) extraídas dos dados reais.
    easy_pace_s = models.IntegerField(null=True, blank=True)
    threshold_pace_s = models.IntegerField(null=True, blank=True)
    recent_effort_pace_s = models.IntegerField(null=True, blank=True)
    recent_effort_distance_m = models.FloatField(null=True, blank=True)
    acwr = models.FloatField(null=True, blank=True)
    acwr_zone = models.CharField(max_length=16, blank=True, default="")
    consistency_pct = models.IntegerField(default=0)
    experience_level = models.CharField(max_length=16, default="beginner")
    confidence = models.CharField(max_length=8, default="low")

    def __str__(self):
        return f"Aptidão de {self.athlete.get_username()} ({self.experience_level})"

    def _pace_str(self, secs):
        from runner.services import metrics

        return metrics.format_pace(secs) if secs else None

    @property
    def easy_pace_str(self):
        return self._pace_str(self.easy_pace_s)

    @property
    def threshold_pace_str(self):
        return self._pace_str(self.threshold_pace_s)

    @property
    def recent_effort_pace_str(self):
        return self._pace_str(self.recent_effort_pace_s)

    @property
    def level_label(self):
        return dict(self.LEVEL_CHOICES).get(self.experience_level, self.experience_level)

    @property
    def trend_label(self):
        return dict(self.TREND_CHOICES).get(self.volume_trend, self.volume_trend)

    @property
    def confidence_label(self):
        return dict(self.CONFIDENCE_CHOICES).get(self.confidence, self.confidence)


class Anamnese(models.Model):
    """
    Anamnese do atleta (intake de treinador com olhar clínico): disponibilidade,
    histórico de lesão, dor atual e contexto. Base para o gerador montar um
    plano seguro e realista — e para a blindagem (avisos de risco).
    """

    SURFACE_CHOICES = [
        ("road", "Rua / asfalto"),
        ("trail", "Trail / montanha"),
        ("both", "Ambos"),
    ]
    TIME_CHOICES = [
        ("morning", "Manhã"),
        ("afternoon", "Tarde"),
        ("evening", "Noite"),
        ("vary", "Varia"),
    ]

    athlete = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="anamnese"
    )
    # Disponibilidade de treino
    sessions_per_week = models.IntegerField(default=4)
    available_days = models.JSONField(default=list)      # ints 0=Seg ... 6=Dom
    preferred_long_day = models.IntegerField(default=6)  # domingo
    preferred_time = models.CharField(max_length=10, choices=TIME_CHOICES, default="vary")
    # Contexto de treino
    does_strength = models.BooleanField(default=False)
    surface = models.CharField(max_length=8, choices=SURFACE_CHOICES, default="road")
    age = models.IntegerField(null=True, blank=True)
    goal_note = models.TextField(blank=True, default="")
    # Olhar clínico / blindagem
    injury_history = models.TextField(blank=True, default="")
    has_pain_now = models.BooleanField(default=False)
    pain_where = models.CharField(max_length=200, blank=True, default="")
    health_notes = models.TextField(blank=True, default="")   # condições, medicação
    medical_clearance = models.BooleanField(default=False)    # liberado por profissional

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Anamnese de {self.athlete.get_username()}"

    @property
    def available_days_labels(self):
        return weekday_labels(self.available_days or [])

    @property
    def long_day_label(self):
        d = self.preferred_long_day
        return WEEKDAYS[d] if 0 <= d <= 6 else "—"

    @property
    def has_risk_flags(self):
        return self.has_pain_now or bool((self.injury_history or "").strip())
