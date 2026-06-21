"""Views do ZayaRun (lado do corredor)."""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from runner import views_coach
from runner.models import (
    Activity,
    CoachAthlete,
    FitnessProfile,
    PlannedWorkout,
    StravaToken,
    WorkoutFeedback,
)
from runner.services import insights, metrics, strava

logger = logging.getLogger(__name__)


def service_worker(request):
    """Serve o service worker em /sw.js (escopo raiz) — necessário p/ instalar o PWA."""
    resp = render(request, "runner/sw.js", content_type="application/javascript")
    resp["Service-Worker-Allowed"] = "/"
    resp["Cache-Control"] = "no-cache"  # sempre revalida → atualizações propagam rápido
    return resp


@login_required
def ai_diag(request):
    """Diagnóstico da IA (só admin) — mostra provider, prefixo da chave e o motivo
    EXATO de uma falha do Gemini (ex.: corpo do 429). Não vaza a chave."""
    from django.http import Http404, HttpResponse

    from runner.services import ai

    if not request.user.is_staff:
        raise Http404()
    gk = ai.gemini_key()
    fmt = (f"({gk[:6]}… {len(gk)} chars)"
           + ("  ✅ AIza" if gk.startswith("AIza") else "  ⚠️ não-AIza")) if gk else "AUSENTE"
    lines = [
        f"AI_PROVIDER     : {getattr(settings, 'AI_PROVIDER', 'auto')}",
        f"GEMINI_MODEL    : {getattr(settings, 'GEMINI_MODEL', '-')}",
        f"provider()      : {ai.provider()}",
        f"GEMINI_API_KEY  : {fmt}",
        f"ANTHROPIC_KEY   : {'presente' if ai.anthropic_key() else 'ausente'}",
        "-" * 64,
    ]
    if ai.provider() == "gemini":
        text, debug = ai.gemini_request("Você é um teste.", "Responda apenas: pong.", max_tokens=50)
        lines.append(f"gemini_request  : {debug}")
        if text:
            lines.append(f"resposta        : {text}")
    else:
        lines.append("(provider != gemini — nada a testar)")
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


@login_required
def dashboard(request):
    """Painel principal do corredor: metricas, evolucao e projecoes."""
    has_strava = StravaToken.objects.filter(user=request.user).exists()
    all_activities = list(request.user.activities.all())

    period = metrics.normalize_period(request.GET.get("period", "month"))
    activities = metrics.filter_by_period(all_activities, period)

    evolution = metrics.evolution_series(activities)
    volume = metrics.volume_series(activities, period)

    # --- Lado-treinador para o atleta: vínculo, semana prescrita, pendências ---
    today = timezone.localdate()
    coach_link = (
        CoachAthlete.objects.filter(
            athlete=request.user, status=CoachAthlete.STATUS_ACTIVE
        ).select_related("coach").first()
    )
    week_plan = views_coach.planned_week(request.user)
    today_workouts = [w for w in week_plan["workouts"] if w.date == today]
    # Treinos realizados nos últimos dias ainda sem feedback (pede PSE).
    done_recent = [
        w for w in request.user.planned_workouts.filter(
            date__gte=today - timedelta(days=4),
            status=PlannedWorkout.STATUS_COMPLETED,
        )
    ]
    with_feedback = set(
        WorkoutFeedback.objects.filter(
            planned_workout__in=done_recent
        ).values_list("planned_workout_id", flat=True)
    )
    needs_feedback = [w for w in done_recent if w.pk not in with_feedback]

    context = {
        "coach_link": coach_link,
        "is_coach": getattr(getattr(request.user, "profile", None), "is_coach", False),
        "week_plan": week_plan,
        "today_workouts": today_workouts,
        "needs_feedback": needs_feedback,
        "feeling_choices": WorkoutFeedback.FEELING_CHOICES,
        "has_strava": has_strava,
        "strava_configured": strava.is_configured(),
        "period": period,
        "period_choices": metrics.period_choices(),
        "activities": activities[:15],
        "summary": metrics.summary(activities),
        "trend": metrics.evolution_trend(activities),
        # PRs e insight olham o histórico completo, não o período filtrado.
        "prs": metrics.personal_records(all_activities),
        "insight": insights.daily_insight(all_activities, user=request.user),
        # ACWR sempre usa janelas fixas (7/28 dias), independente do filtro.
        "acwr": metrics.acwr(all_activities),
        "race": metrics.race_projections(activities),
        "volume_labels": json.dumps([v["label"] for v in volume]),
        "volume_km": json.dumps([v["km"] for v in volume]),
        "evo_labels": json.dumps(evolution["labels"]),
        "evo_paces": json.dumps(evolution["paces_seconds"]),
        "evo_paces_str": json.dumps(evolution["paces_str"]),
        "evo_distances": json.dumps(evolution["distances_km"]),
        "evo_cadences": json.dumps(evolution["cadences_spm"]),
        "evo_has_cadence": evolution["has_cadence"],
    }
    return render(request, "runner/dashboard.html", context)


@login_required
def strava_connect(request):
    """Inicia o fluxo OAuth da Strava."""
    if not strava.is_configured():
        messages.error(
            request,
            "Strava nao configurada. Defina STRAVA_CLIENT_ID e "
            "STRAVA_CLIENT_SECRET nas variaveis de ambiente.",
        )
        return redirect("dashboard")
    # URL de callback deduzida do proprio host (funciona em qualquer dominio
    # sem configuracao extra). Forca https fora do modo DEBUG (Heroku).
    redirect_uri = request.build_absolute_uri(reverse("strava_callback"))
    if not settings.DEBUG:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    return redirect(
        strava.build_authorize_url(redirect_uri=redirect_uri, state=str(request.user.pk))
    )


def strava_login(request):
    """Inicia o login COM Strava (usuario anonimo): autoriza e cria a conta."""
    if not strava.is_configured():
        messages.error(request, "Login com Strava indisponivel (nao configurado).")
        return redirect("login")
    redirect_uri = request.build_absolute_uri(reverse("strava_callback"))
    if not settings.DEBUG:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    return redirect(
        strava.build_authorize_url(redirect_uri=redirect_uri, state="login")
    )


def strava_callback(request):
    """
    Retorno do OAuth da Strava. Serve para os dois fluxos:
      - usuario logado  -> conecta a Strava a conta atual;
      - usuario anonimo -> cria/loga a conta a partir do atleta (login Strava).
    """
    error = request.GET.get("error")
    code = request.GET.get("code")
    if error or not code:
        messages.error(request, "Autorizacao da Strava cancelada ou invalida.")
        return redirect("dashboard")

    try:
        data = strava.exchange_code_raw(code)
    except strava.StravaError as exc:
        messages.error(request, str(exc))
        return redirect("login")

    if request.user.is_authenticated:
        user = request.user
    else:
        try:
            user = strava.get_or_create_user_from_athlete(data)
        except strava.StravaError as exc:
            messages.error(request, str(exc))
            return redirect("login")
        auth_login(request, user)

    try:
        strava.save_token(user, data)
        # Backfill profundo na conexão: precisamos do histórico para o plano.
        created, updated = strava.sync_activities(user, deep=True)
        messages.success(
            request, f"Strava conectada! {created} treinos novos, {updated} atualizados."
        )
    except strava.StravaError as exc:
        messages.error(request, str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("Erro inesperado no callback da Strava")
        messages.error(request, "Erro inesperado ao conectar a Strava.")
    return redirect("dashboard")


@login_required
@require_POST
def strava_sync(request):
    """Sincroniza atividades sob demanda."""
    if not StravaToken.objects.filter(user=request.user).exists():
        messages.error(request, "Conecte sua conta Strava primeiro.")
        return redirect("dashboard")
    try:
        created, updated = strava.sync_activities(request.user)
        messages.success(
            request, f"Sincronizado: {created} novos, {updated} atualizados."
        )
    except strava.StravaError as exc:
        messages.error(request, str(exc))
    return redirect("dashboard")


@login_required
@require_POST
def strava_disconnect(request):
    # Termos da Strava: ao desconectar, removemos os dados sincronizados.
    StravaToken.objects.filter(user=request.user).delete()
    Activity.objects.filter(user=request.user, source=Activity.SOURCE_STRAVA).delete()
    FitnessProfile.objects.filter(athlete=request.user).delete()
    messages.info(request, "Conta Strava desconectada e seus dados da Strava removidos.")
    return redirect("dashboard")


@login_required
def activity_detail(request, pk):
    """Detalhe de uma atividade com splits por km."""
    activity = get_object_or_404(Activity, pk=pk, user=request.user)
    # Busca splits sob demanda (uma vez) se ainda nao temos.
    if not activity.splits and activity.source == Activity.SOURCE_STRAVA:
        try:
            strava.fetch_activity_detail(request.user, activity)
        except strava.StravaError as exc:
            messages.warning(request, f"Nao foi possivel carregar os splits: {exc}")
    # Se esta atividade cumpriu um treino prescrito, mostra planejado x realizado.
    planned = activity.planned_workouts.first()
    adherence = metrics.adherence(planned, activity) if planned else None
    return render(request, "runner/activity_detail.html", {
        "activity": activity,
        "planned": planned,
        "adherence": adherence,
    })
