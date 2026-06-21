"""Rotas do app runner."""
from django.urls import path

from runner import views, views_coach

urlpatterns = [
    # Lado-corredor (existente)
    path("", views.dashboard, name="dashboard"),
    path("strava/connect/", views.strava_connect, name="strava_connect"),
    path("strava/login/", views.strava_login, name="strava_login"),
    path("strava/callback/", views.strava_callback, name="strava_callback"),
    path("strava/sync/", views.strava_sync, name="strava_sync"),
    path("strava/disconnect/", views.strava_disconnect, name="strava_disconnect"),
    path("atividade/<int:pk>/", views.activity_detail, name="activity_detail"),

    # Lado-treinador (assessoria)
    path("treinador/", views_coach.coach_dashboard, name="coach_dashboard"),
    path("treinador/ativar/", views_coach.become_coach, name="become_coach"),
    path("treinador/convidar/", views_coach.coach_invite, name="coach_invite"),
    path("treinador/atleta/<int:athlete_id>/", views_coach.coach_athlete, name="coach_athlete"),
    path("treinador/atleta/<int:athlete_id>/prescrever/", views_coach.prescribe_workout, name="prescribe_workout"),
    path("treinador/atleta/<int:athlete_id>/auto/", views_coach.auto_prescribe_week, name="auto_prescribe_week"),
    path("treinador/atleta/<int:athlete_id>/plano/", views_coach.create_plan, name="create_plan"),

    # Atleta: vínculo, plano, feedback, check-in, copiloto
    path("aceitar-convite/", views_coach.accept_invite, name="accept_invite"),
    path("plano/", views_coach.my_plan, name="my_plan"),
    path("plano/criar/", views_coach.create_my_plan, name="create_my_plan"),
    path("plano/<int:plan_id>/", views_coach.plan_detail, name="plan_detail"),
    path("plano/<int:plan_id>/excluir/", views_coach.delete_plan, name="delete_plan"),
    path("plano/<int:plan_id>/treino/novo/", views_coach.add_workout_to_plan, name="add_workout_to_plan"),
    path("treino/<int:planned_id>/editar/", views_coach.edit_workout, name="edit_workout"),
    path("treino/<int:planned_id>/excluir/", views_coach.delete_workout, name="delete_workout"),
    path("treino/<int:planned_id>/feedback/", views_coach.workout_feedback, name="workout_feedback"),
    path("checkin/", views_coach.daily_checkin, name="daily_checkin"),
    path("copiloto/", views_coach.copilot, name="copilot"),
    path("anamnese/", views_coach.edit_anamnese, name="edit_anamnese"),
    path("treinador/atleta/<int:athlete_id>/anamnese/", views_coach.edit_anamnese, name="edit_anamnese_athlete"),
]
