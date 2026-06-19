"""Views do ZayaRun (lado do corredor)."""
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from runner.models import Activity, StravaToken
from runner.services import insights, metrics, strava

logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    """Painel principal do corredor: metricas, evolucao e projecoes."""
    has_strava = StravaToken.objects.filter(user=request.user).exists()
    all_activities = list(request.user.activities.all())

    period = metrics.normalize_period(request.GET.get("period", "month"))
    activities = metrics.filter_by_period(all_activities, period)

    evolution = metrics.evolution_series(activities)
    volume = metrics.volume_series(activities, period)

    context = {
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


@login_required
def strava_callback(request):
    """Recebe o retorno do OAuth e troca o code por tokens."""
    error = request.GET.get("error")
    code = request.GET.get("code")
    if error or not code:
        messages.error(request, "Autorizacao da Strava cancelada ou invalida.")
        return redirect("dashboard")
    try:
        strava.exchange_code_for_token(request.user, code)
        messages.success(request, "Strava conectada! Sincronizando seus treinos...")
        created, updated = strava.sync_activities(request.user)
        messages.success(
            request, f"{created} treinos novos, {updated} atualizados."
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
    StravaToken.objects.filter(user=request.user).delete()
    messages.info(request, "Conta Strava desconectada.")
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
    return render(request, "runner/activity_detail.html", {"activity": activity})
