"""Rotas do app runner."""
from django.urls import path

from runner import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("strava/connect/", views.strava_connect, name="strava_connect"),
    path("strava/callback/", views.strava_callback, name="strava_callback"),
    path("strava/sync/", views.strava_sync, name="strava_sync"),
    path("strava/disconnect/", views.strava_disconnect, name="strava_disconnect"),
    path("atividade/<int:pk>/", views.activity_detail, name="activity_detail"),
]
