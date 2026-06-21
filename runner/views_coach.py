"""
Views do lado-treinador e dos recursos novos do atleta:
roster + triagem do coach, prescrição (manual/IA), plano de prova,
feedback (PSE), check-in diário e copiloto conversacional.
"""
import logging
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from runner.models import (
    WORKOUT_TYPES,
    CoachAthlete,
    DailyCheckin,
    PlannedWorkout,
    Profile,
    TrainingPlan,
    WorkoutFeedback,
)
from runner.services import ai, coaching, fitness, metrics, plans, wellness

logger = logging.getLogger(__name__)

# Opções de prova para os formulários de plano (metros, rótulo).
RACE_CHOICES = [(5_000, "5 km"), (10_000, "10 km"),
                (21_097, "21 km (meia)"), (42_195, "42 km (maratona)")]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def get_profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def _require_coach_link(coach, athlete_id):
    link = (
        CoachAthlete.objects.filter(
            coach=coach, athlete_id=athlete_id, status=CoachAthlete.STATUS_ACTIVE
        )
        .select_related("athlete")
        .first()
    )
    if not link:
        raise Http404("Atleta não encontrado para este treinador.")
    return link


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_pace(text):
    """'5:00' -> 300 segundos/km. Vazio/invalido -> None."""
    if not text or ":" not in text:
        return None
    try:
        m, s = text.strip().split(":")
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def _parse_duration(text):
    """'1:45:00' ou '50:00' -> segundos. Vazio/invalido -> None."""
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return None


def _parse_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def planned_week(athlete, ref_date=None):
    """Semana atual de treinos prescritos, com aderência nos já realizados."""
    today = timezone.localdate()
    ref = ref_date or today
    monday = ref - timedelta(days=ref.weekday())
    workouts = list(
        athlete.planned_workouts.filter(
            date__range=(monday, monday + timedelta(days=6))
        ).select_related("matched_activity")
    )
    for w in workouts:
        w.adherence = metrics.adherence(w, w.matched_activity) if w.matched_activity else None
    by_date = {}
    for w in workouts:
        by_date.setdefault(w.date, []).append(w)
    items = [
        {
            "date": monday + timedelta(days=i),
            "weekday": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][i],
            "is_today": (monday + timedelta(days=i)) == today,
            "workouts": by_date.get(monday + timedelta(days=i), []),
        }
        for i in range(7)
    ]
    return {"monday": monday, "items": items, "workouts": workouts}


# --------------------------------------------------------------------------
# Coach: ativação, roster, convites
# --------------------------------------------------------------------------
@login_required
def coach_dashboard(request):
    profile = get_profile(request.user)
    if not profile.is_coach:
        return render(request, "runner/coach_dashboard.html", {"is_coach": False})
    cards = coaching.triage_roster(request.user)
    pending = CoachAthlete.objects.filter(
        coach=request.user, status=CoachAthlete.STATUS_INVITED
    )
    return render(request, "runner/coach_dashboard.html", {
        "is_coach": True,
        "cards": cards,
        "summary": coaching.roster_summary(cards),
        "pending": pending,
    })


@login_required
@require_POST
def become_coach(request):
    profile = get_profile(request.user)
    profile.is_coach = True
    profile.save(update_fields=["is_coach"])
    messages.success(request, "Modo treinador ativado. Convide seu primeiro atleta.")
    return redirect("coach_dashboard")


@login_required
@require_POST
def coach_invite(request):
    get_profile(request.user)
    label = request.POST.get("label", "").strip()
    link = CoachAthlete.objects.create(coach=request.user, label=label)
    messages.success(
        request,
        f"Convite criado para “{label or 'novo atleta'}”. "
        f"Código: {link.invite_code} — envie ao atleta.",
    )
    return redirect("coach_dashboard")


@login_required
def accept_invite(request):
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()
        link = CoachAthlete.objects.filter(
            invite_code=code, status=CoachAthlete.STATUS_INVITED
        ).first()
        if not link:
            messages.error(request, "Código inválido ou já utilizado.")
        elif link.coach == request.user:
            messages.error(request, "Você não pode ser atleta de si mesmo.")
        else:
            link.accept(request.user)
            messages.success(
                request, f"Pronto! Você agora é atleta de {link.coach.get_username()}."
            )
            return redirect("dashboard")
    return render(request, "runner/accept_invite.html", {})


# --------------------------------------------------------------------------
# Coach: detalhe do atleta + prescrição
# --------------------------------------------------------------------------
@login_required
def coach_athlete(request, athlete_id):
    link = _require_coach_link(request.user, athlete_id)
    athlete = link.athlete
    activities = list(athlete.activities.all())
    today = timezone.localdate()
    recent_planned = list(
        athlete.planned_workouts.filter(date__gte=today - timedelta(days=28))
    )
    return render(request, "runner/coach_athlete.html", {
        "athlete": athlete,
        "athlete_name": coaching._athlete_name(athlete),
        "link": link,
        "summary": metrics.summary(activities),
        "acwr": metrics.acwr(activities),
        "trend": metrics.evolution_trend(activities),
        "week": planned_week(athlete),
        "adherence": metrics.plan_adherence_rate(recent_planned),
        "fitness": fitness.update_profile(athlete),
        "plan": athlete.training_plans.filter(status=TrainingPlan.STATUS_ACTIVE).first(),
        "workout_types": WORKOUT_TYPES,
        "race_choices": RACE_CHOICES,
        "today": today,
        "ai_enabled": ai.is_enabled(),
    })


@login_required
@require_POST
def prescribe_workout(request, athlete_id):
    link = _require_coach_link(request.user, athlete_id)
    day = _parse_date(request.POST.get("date"))
    if not day:
        messages.error(request, "Informe uma data válida para o treino.")
        return redirect("coach_athlete", athlete_id=athlete_id)
    wtype = request.POST.get("workout_type", "easy")
    if wtype not in dict(WORKOUT_TYPES):
        wtype = "easy"
    pace_s = _parse_pace(request.POST.get("pace", ""))
    dist_km = _to_float(request.POST.get("distance_km"))
    PlannedWorkout.objects.create(
        athlete=link.athlete,
        created_by=request.user,
        source=PlannedWorkout.SOURCE_COACH,
        date=day,
        workout_type=wtype,
        title=request.POST.get("title", "").strip() or dict(WORKOUT_TYPES)[wtype],
        description=request.POST.get("description", "").strip(),
        target_distance_m=dist_km * 1000 if dist_km else None,
        target_pace_low_s=pace_s,
        target_pace_high_s=pace_s,
    )
    messages.success(request, "Treino prescrito.")
    return redirect("coach_athlete", athlete_id=athlete_id)


@login_required
@require_POST
def auto_prescribe_week(request, athlete_id):
    link = _require_coach_link(request.user, athlete_id)
    count, note = coaching.auto_prescribe(link.athlete, created_by=request.user)
    messages.success(request, f"{count} treinos gerados pela IA. {note}")
    return redirect("coach_athlete", athlete_id=athlete_id)


@login_required
@require_POST
def create_plan(request, athlete_id):
    link = _require_coach_link(request.user, athlete_id)
    plan = _build_plan_from_post(request, link.athlete, created_by=request.user,
                                 source=PlannedWorkout.SOURCE_COACH)
    if plan:
        messages.success(request, "Plano de prova criado e treinos prescritos.")
    return redirect("coach_athlete", athlete_id=athlete_id)


def _build_plan_from_post(request, athlete, created_by, source):
    goal_distance_m = _to_float(request.POST.get("goal_distance_m"))
    race_date = _parse_date(request.POST.get("race_date"))
    if not goal_distance_m or not race_date:
        messages.error(request, "Escolha a prova e a data para gerar o plano.")
        return None
    if race_date <= timezone.localdate():
        messages.error(request, "A data da prova precisa ser no futuro.")
        return None
    goal_time_s = _parse_duration(request.POST.get("goal_time", ""))
    spec = plans.generate_plan(
        list(athlete.activities.all()), goal_distance_m, race_date, goal_time_s=goal_time_s
    )
    return plans.materialize_plan(
        athlete, spec, created_by=created_by, source=source,
        generated_by=TrainingPlan.GEN_RULES,
    )


# --------------------------------------------------------------------------
# Atleta: plano próprio, feedback, check-in, copiloto
# --------------------------------------------------------------------------
@login_required
def my_plan(request):
    plan = request.user.training_plans.filter(status=TrainingPlan.STATUS_ACTIVE).first()
    upcoming = list(
        request.user.planned_workouts.filter(date__gte=timezone.localdate())
        .order_by("date")[:21]
    )
    return render(request, "runner/plan.html", {
        "plan": plan,
        "week": planned_week(request.user),
        "upcoming": upcoming,
        "race_choices": RACE_CHOICES,
        "fitness": fitness.update_profile(request.user),
        "readiness": wellness.readiness(request.user),
        "today_checkin": DailyCheckin.objects.filter(
            athlete=request.user, date=timezone.localdate()
        ).first(),
    })


@login_required
@require_POST
def create_my_plan(request):
    plan = _build_plan_from_post(request, request.user, created_by=request.user,
                                 source=PlannedWorkout.SOURCE_SELF)
    if plan:
        messages.success(request, "Seu plano de prova foi criado!")
    return redirect("my_plan")


@login_required
@require_POST
def workout_feedback(request, planned_id):
    planned = get_object_or_404(PlannedWorkout, pk=planned_id, athlete=request.user)
    WorkoutFeedback.objects.update_or_create(
        planned_workout=planned,
        defaults={
            "athlete": request.user,
            "activity": planned.matched_activity,
            "date": planned.date,
            "rpe": _to_int(request.POST.get("rpe")),
            "feeling": request.POST.get("feeling", ""),
            "soreness": _to_int(request.POST.get("soreness")),
            "notes": request.POST.get("notes", "").strip(),
        },
    )
    messages.success(request, "Feedback registrado — isso calibra seus próximos treinos.")
    return redirect(request.POST.get("next") or "my_plan")


@login_required
@require_POST
def daily_checkin(request):
    DailyCheckin.objects.update_or_create(
        athlete=request.user,
        date=timezone.localdate(),
        defaults={
            "sleep_hours": _to_float(request.POST.get("sleep_hours")),
            "sleep_quality": _to_int(request.POST.get("sleep_quality")),
            "soreness": _to_int(request.POST.get("soreness")),
            "stress": _to_int(request.POST.get("stress")),
            "notes": request.POST.get("notes", "").strip(),
            "source": DailyCheckin.SOURCE_MANUAL,
        },
    )
    messages.success(request, "Check-in do dia salvo.")
    return redirect("dashboard")


@login_required
def copilot(request):
    answer = None
    question = ""
    if request.method == "POST":
        question = request.POST.get("question", "").strip()
        if question:
            answer = coaching.answer_question(request.user, question)
    return render(request, "runner/copilot.html", {
        "answer": answer,
        "question": question,
        "ai_enabled": ai.is_enabled(),
        "suggestions": [
            "Como está minha evolução de pace?",
            "Estou correndo risco de lesão?",
            "Que treino devo fazer amanhã?",
            "Estou pronto para uma meia maratona?",
        ],
    })
