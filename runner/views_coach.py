"""
Views do lado-treinador e dos recursos novos do atleta:
roster + triagem do coach, prescrição (manual/IA), plano de prova,
feedback (PSE), check-in diário e copiloto conversacional.
"""
import json
import logging
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from runner.models import (
    WORKOUT_TYPES,
    Anamnese,
    CoachAthlete,
    CoachSuggestion,
    CopilotChat,
    DailyCheckin,
    PlannedWorkout,
    Profile,
    TrainingPlan,
    WorkoutFeedback,
    live_planned_filter,
)
from runner.services import ai, autoreg, coaching, fitness, metrics, plans, wellness

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
            live_planned_filter(),
            date__range=(monday, monday + timedelta(days=6)),
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
        "anamnese": _get_anamnese(athlete),
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
        return redirect("plan_detail", plan_id=plan.id)
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
        list(athlete.activities.all()), goal_distance_m, race_date,
        goal_time_s=goal_time_s, anamnese=_get_anamnese(athlete),
    )
    return plans.materialize_plan(
        athlete, spec, created_by=created_by, source=source,
        generated_by=TrainingPlan.GEN_RULES,
    )


def _get_anamnese(athlete):
    """Anamnese do atleta (reverse OneToOne) sem estourar quando não existe."""
    try:
        return athlete.anamnese
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Atleta: plano próprio, feedback, check-in, copiloto
# --------------------------------------------------------------------------
@login_required
def my_plan(request):
    plan = request.user.training_plans.filter(status=TrainingPlan.STATUS_ACTIVE).first()
    # O atleta pode ter vários planos guardados; só um fica ATIVO (o que ele segue).
    plans_all = list(request.user.training_plans.all())
    others = [p for p in plans_all if p.id != (plan.id if plan else None)]
    upcoming = list(
        request.user.planned_workouts.filter(
            live_planned_filter(), date__gte=timezone.localdate()
        ).order_by("date")[:21]
    )
    return render(request, "runner/plan.html", {
        "plan": plan,
        "other_plans": others,
        "week": planned_week(request.user),
        "upcoming": upcoming,
        "race_choices": RACE_CHOICES,
        "fitness": fitness.update_profile(request.user),
        "anamnese": _get_anamnese(request.user),
        "readiness": wellness.readiness(request.user),
        "today_checkin": DailyCheckin.objects.filter(
            athlete=request.user, date=timezone.localdate()
        ).first(),
        "zaya_suggestion": autoreg.pending_suggestion(request.user),
        "autonomy": get_profile(request.user).coach_autonomy,
        "autonomy_choices": Profile.AUTONOMY_CHOICES,
    })


@login_required
@require_POST
def create_my_plan(request):
    plan = _build_plan_from_post(request, request.user, created_by=request.user,
                                 source=PlannedWorkout.SOURCE_SELF)
    if plan:
        messages.success(request, "Seu plano de prova foi criado!")
        return redirect("plan_detail", plan_id=plan.id)
    return redirect("my_plan")


@login_required
@require_POST
def workout_feedback(request, planned_id):
    planned = get_object_or_404(PlannedWorkout, pk=planned_id, athlete=request.user)
    feedback, _ = WorkoutFeedback.objects.update_or_create(
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
    # Autorregulação: respeita a autonomia (por padrão SUGERE, não impõe).
    try:
        adj = autoreg.autoregulate(request.user, feedback, planned)
        if adj["mode"] == "auto" and adj["applied"]:
            messages.warning(request, "Zaya: " + adj["summary"])
        elif adj["mode"] == "suggest" and adj["suggestion"]:
            messages.info(request, "A Zaya tem uma sugestão para o seu plano — veja abaixo.")
            return redirect("my_plan")  # mostra o card de sugestão (você decide)
    except Exception:  # noqa: BLE001 — ajuste nunca pode quebrar o registro do feedback
        logger.exception("Falha na autorregulação após feedback")
    return redirect(request.POST.get("next") or "my_plan")


@login_required
@require_POST
def resolve_suggestion(request, suggestion_id):
    """O atleta aceita (aplica) ou mantém o treino (registra a decisão)."""
    sug = get_object_or_404(
        CoachSuggestion, pk=suggestion_id, athlete=request.user,
        status=CoachSuggestion.STATUS_PENDING,
    )
    if request.POST.get("action") == "accept":
        messages.success(request, "Zaya: " + autoreg.accept_suggestion(sug))
    else:
        autoreg.decline_suggestion(sug)
        messages.info(request, "Beleza — mantive seu treino como estava. 💪 Sente o corpo durante a corrida.")
    return redirect(request.POST.get("next") or "my_plan")


@login_required
@require_POST
def set_zaya_autonomy(request):
    """Define quanto a Zaya pode mexer no plano (só sugerir / ajustar / não interferir)."""
    value = request.POST.get("coach_autonomy")
    if value in dict(Profile.AUTONOMY_CHOICES):
        get_profile(request.user)  # garante o Profile
        Profile.objects.filter(user=request.user).update(coach_autonomy=value)
        messages.success(request, "Preferência da Zaya salva.")
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
            CopilotChat.objects.create(
                athlete=request.user, question=question, answer=answer
            )
    # Histórico: as últimas 5 conversas com a Zaya (mais recente primeiro).
    history = list(request.user.copilot_chats.all()[:5])
    return render(request, "runner/copilot.html", {
        "answer": answer,
        "question": question,
        "history": history,
        "ai_enabled": ai.is_enabled(),
        "suggestions": [
            "Como está minha evolução de pace?",
            "Estou correndo risco de lesão?",
            "Que treino devo fazer amanhã?",
            "Estou pronto para uma meia maratona?",
        ],
    })


# --------------------------------------------------------------------------
# CRUD do plano: ver (macrociclo), editar treino, excluir treino, excluir plano
# --------------------------------------------------------------------------
def _can_manage(user, athlete):
    """O usuário pode gerir este atleta? (é ele mesmo ou seu treinador ativo)."""
    if user == athlete:
        return True
    return CoachAthlete.objects.filter(
        coach=user, athlete=athlete, status=CoachAthlete.STATUS_ACTIVE
    ).exists()


def _back_to(request, workout):
    if workout.plan_id:
        return reverse("plan_detail", args=[workout.plan_id])
    if request.user == workout.athlete:
        return reverse("my_plan")
    return reverse("coach_athlete", args=[workout.athlete_id])


def _plan_weeks(plan):
    """Agrupa os treinos do plano por semana (segunda→domingo), com aderência."""
    workouts = list(plan.workouts.select_related("matched_activity").order_by("date"))
    for w in workouts:
        w.adherence = metrics.adherence(w, w.matched_activity) if w.matched_activity else None
    today = timezone.localdate()
    by_monday = {}
    for w in workouts:
        monday = w.date - timedelta(days=w.date.weekday())
        by_monday.setdefault(monday, []).append(w)
    weeks = []
    for index, monday in enumerate(sorted(by_monday), start=1):
        ws = by_monday[monday]
        sunday = monday + timedelta(days=6)
        active = [w for w in ws if w.workout_type != "rest"]
        done_ws = [w for w in ws if w.status == PlannedWorkout.STATUS_COMPLETED]
        weeks.append({
            "index": index,
            "monday": monday,
            "sunday": sunday,
            "workouts": ws,
            "total_km": round(sum((w.target_distance_m or 0) for w in ws) / 1000.0, 1),
            "done_km": round(sum((w.target_distance_m or 0) for w in done_ws) / 1000.0, 1),
            "done": len(done_ws),
            "count": len(active),
            "is_current": monday <= today <= sunday,
            "is_past": sunday < today,
        })
    return weeks


def _plan_chart(weeks):
    """Série semanal planejado × realizado (km) + aderência acumulada (%)."""
    labels, planned, done, adherence = [], [], [], []
    cum_done = cum_count = 0
    for w in weeks:
        labels.append(f"S{w['index']}")
        planned.append(w["total_km"])
        done.append(w["done_km"])
        cum_done += w["done"]
        cum_count += w["count"]
        adherence.append(round(cum_done / cum_count * 100) if cum_count else 0)
    return {"labels": labels, "planned": planned, "done": done, "adherence": adherence}


@login_required
def plan_detail(request, plan_id):
    plan = get_object_or_404(TrainingPlan, pk=plan_id)
    if not _can_manage(request.user, plan.athlete):
        raise Http404("Plano não encontrado.")
    weeks = _plan_weeks(plan)
    done_total = sum(w["done"] for w in weeks)
    count_total = sum(w["count"] for w in weeks)
    return render(request, "runner/plan_detail.html", {
        "plan": plan,
        "weeks": weeks,
        "is_coach_view": request.user != plan.athlete,
        "athlete_name": coaching._athlete_name(plan.athlete),
        "workout_types": WORKOUT_TYPES,
        "done_total": done_total,
        "count_total": count_total,
        "adherence_pct": round(done_total / count_total * 100) if count_total else None,
        "chart_json": json.dumps(_plan_chart(weeks)),
        "today": timezone.localdate(),
    })


@login_required
@require_POST
def delete_plan(request, plan_id):
    plan = get_object_or_404(TrainingPlan, pk=plan_id)
    athlete = plan.athlete
    if not _can_manage(request.user, athlete):
        raise Http404("Plano não encontrado.")
    # Remove treinos futuros ainda não realizados; mantém o histórico (plan→null).
    PlannedWorkout.objects.filter(
        plan=plan, date__gte=timezone.localdate(), status=PlannedWorkout.STATUS_PLANNED
    ).delete()
    plan.delete()
    messages.info(request, "Plano excluído. Os treinos já realizados foram mantidos no histórico.")
    if request.user == athlete:
        return redirect("my_plan")
    return redirect("coach_athlete", athlete_id=athlete.id)


@login_required
@require_POST
def activate_plan(request, plan_id):
    """Define este plano como o ATIVO (o que aparece no painel); os demais ficam guardados."""
    plan = get_object_or_404(TrainingPlan, pk=plan_id)
    athlete = plan.athlete
    if not _can_manage(request.user, athlete):
        raise Http404("Plano não encontrado.")
    TrainingPlan.objects.filter(
        athlete=athlete, status=TrainingPlan.STATUS_ACTIVE
    ).exclude(pk=plan.pk).update(status=TrainingPlan.STATUS_INACTIVE)
    plan.status = TrainingPlan.STATUS_ACTIVE
    plan.save(update_fields=["status", "updated_at"])
    messages.success(request, f"Plano “{plan.goal_label}” ativado — agora é ele que aparece no seu painel.")
    return redirect("plan_detail", plan_id=plan.id)


@login_required
def edit_workout(request, planned_id):
    workout = get_object_or_404(PlannedWorkout, pk=planned_id)
    if not _can_manage(request.user, workout.athlete):
        raise Http404("Treino não encontrado.")
    if request.method == "POST":
        workout.date = _parse_date(request.POST.get("date")) or workout.date
        wtype = request.POST.get("workout_type", workout.workout_type)
        workout.workout_type = wtype if wtype in dict(WORKOUT_TYPES) else workout.workout_type
        workout.title = request.POST.get("title", "").strip() or dict(WORKOUT_TYPES)[workout.workout_type]
        workout.description = request.POST.get("description", "").strip()
        dist_km = _to_float(request.POST.get("distance_km"))
        workout.target_distance_m = dist_km * 1000 if dist_km else None
        pace_s = _parse_pace(request.POST.get("pace", ""))
        if pace_s:
            workout.target_pace_low_s = pace_s
            workout.target_pace_high_s = pace_s
        status = request.POST.get("status")
        if status in dict(PlannedWorkout.STATUS_CHOICES):
            workout.status = status
        workout.save()
        messages.success(request, "Treino atualizado.")
        return redirect(_back_to(request, workout))
    return render(request, "runner/edit_workout.html", {
        "workout": workout,
        "workout_types": WORKOUT_TYPES,
        "status_choices": PlannedWorkout.STATUS_CHOICES,
        "athlete_name": coaching._athlete_name(workout.athlete),
        "back_url": _back_to(request, workout),
    })


@login_required
@require_POST
def delete_workout(request, planned_id):
    workout = get_object_or_404(PlannedWorkout, pk=planned_id)
    if not _can_manage(request.user, workout.athlete):
        raise Http404("Treino não encontrado.")
    back = _back_to(request, workout)
    workout.delete()
    messages.info(request, "Treino removido.")
    return redirect(back)


@login_required
@require_POST
def complete_workout(request, planned_id):
    """Marca/desmarca um treino como concluído (1 toque) — reforça a evolução."""
    workout = get_object_or_404(PlannedWorkout, pk=planned_id)
    if not _can_manage(request.user, workout.athlete):
        raise Http404("Treino não encontrado.")
    if workout.status == PlannedWorkout.STATUS_COMPLETED:
        # Reabre: volta a planejado (mantém o vínculo da Strava, se houver).
        workout.status = PlannedWorkout.STATUS_PLANNED
        messages.info(request, "Treino reaberto.")
    else:
        workout.status = PlannedWorkout.STATUS_COMPLETED
        messages.success(request, "Treino concluído! 💪 Mais um passo na sua evolução.")
    workout.save(update_fields=["status", "updated_at"])
    return redirect(_back_to(request, workout))


@login_required
@require_POST
def add_workout_to_plan(request, plan_id):
    plan = get_object_or_404(TrainingPlan, pk=plan_id)
    if not _can_manage(request.user, plan.athlete):
        raise Http404("Plano não encontrado.")
    day = _parse_date(request.POST.get("date"))
    if not day:
        messages.error(request, "Informe uma data válida.")
        return redirect("plan_detail", plan_id=plan_id)
    wtype = request.POST.get("workout_type", "easy")
    if wtype not in dict(WORKOUT_TYPES):
        wtype = "easy"
    pace_s = _parse_pace(request.POST.get("pace", ""))
    dist_km = _to_float(request.POST.get("distance_km"))
    PlannedWorkout.objects.create(
        athlete=plan.athlete,
        created_by=request.user,
        plan=plan,
        source=PlannedWorkout.SOURCE_COACH if request.user != plan.athlete else PlannedWorkout.SOURCE_SELF,
        date=day,
        workout_type=wtype,
        title=request.POST.get("title", "").strip() or dict(WORKOUT_TYPES)[wtype],
        description=request.POST.get("description", "").strip(),
        target_distance_m=dist_km * 1000 if dist_km else None,
        target_pace_low_s=pace_s,
        target_pace_high_s=pace_s,
    )
    messages.success(request, "Treino adicionado ao plano.")
    return redirect("plan_detail", plan_id=plan_id)


# --------------------------------------------------------------------------
# Anamnese: intake de treinador, pré-preenchido pela leitura da Strava
# --------------------------------------------------------------------------
WEEKDAY_OPTIONS = [(0, "Seg"), (1, "Ter"), (2, "Qua"), (3, "Qui"), (4, "Sex"), (5, "Sáb"), (6, "Dom")]


@login_required
def edit_anamnese(request, athlete_id=None):
    athlete = request.user if athlete_id is None else get_object_or_404(User, pk=athlete_id)
    if not _can_manage(request.user, athlete):
        raise Http404("Atleta não encontrado.")
    anamnese = _get_anamnese(athlete)
    back_url = (
        reverse("my_plan") if request.user == athlete
        else reverse("coach_athlete", args=[athlete.id])
    )

    if request.method == "POST":
        days = sorted({int(d) for d in request.POST.getlist("available_days") if d.isdigit()})
        long_day = _to_int(request.POST.get("preferred_long_day"))
        Anamnese.objects.update_or_create(athlete=athlete, defaults={
            "sessions_per_week": _to_int(request.POST.get("sessions_per_week")) or (len(days) or 4),
            "available_days": days,
            "preferred_long_day": long_day if long_day is not None else 6,
            "preferred_time": request.POST.get("preferred_time", "vary"),
            "does_strength": bool(request.POST.get("does_strength")),
            "surface": request.POST.get("surface", "road"),
            "age": _to_int(request.POST.get("age")),
            "goal_note": request.POST.get("goal_note", "").strip(),
            "injury_history": request.POST.get("injury_history", "").strip(),
            "has_pain_now": bool(request.POST.get("has_pain_now")),
            "pain_where": request.POST.get("pain_where", "").strip(),
            "health_notes": request.POST.get("health_notes", "").strip(),
            "medical_clearance": bool(request.POST.get("medical_clearance")),
        })
        messages.success(request, "Anamnese salva — o plano agora respeita seus dias e seu histórico.")
        return redirect(back_url)

    # GET: pré-preenche da anamnese existente OU da leitura do histórico
    activities = list(athlete.activities.all())
    profile = fitness.compute_profile(activities)
    inferred_days = fitness.infer_training_days(activities)
    selected_days = anamnese.available_days if anamnese else (inferred_days or [1, 3, 5, 6])
    prefill = {
        "sessions": anamnese.sessions_per_week if anamnese else (profile["runs_per_week"] if profile else 4),
        "long_day": anamnese.preferred_long_day if anamnese else fitness.infer_long_day(activities),
        "time": anamnese.preferred_time if anamnese else "vary",
        "surface": anamnese.surface if anamnese else "road",
        "strength": anamnese.does_strength if anamnese else False,
        "age": anamnese.age if anamnese else "",
        "goal": anamnese.goal_note if anamnese else "",
        "injury": anamnese.injury_history if anamnese else "",
        "pain": anamnese.has_pain_now if anamnese else False,
        "pain_where": anamnese.pain_where if anamnese else "",
        "health": anamnese.health_notes if anamnese else "",
        "clearance": anamnese.medical_clearance if anamnese else False,
    }
    return render(request, "runner/anamnese.html", {
        "athlete": athlete,
        "athlete_name": coaching._athlete_name(athlete),
        "anamnese": anamnese,
        "is_coach_view": request.user != athlete,
        "weekday_options": WEEKDAY_OPTIONS,
        "selected_days": selected_days,
        "inferred_days_labels": ", ".join([dict(WEEKDAY_OPTIONS)[d] for d in inferred_days]) if inferred_days else "",
        "prefill": prefill,
        "surface_choices": Anamnese.SURFACE_CHOICES,
        "time_choices": Anamnese.TIME_CHOICES,
        "back_url": back_url,
    })
