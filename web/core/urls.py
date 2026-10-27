from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("healthz", views.healthz, name="healthz"),
    path("setup/", views.setup, name="setup"),
    path("settings/", views.studio_settings, name="settings"),
    path("journal/", views.journal, name="journal"),
    path("backups/", views.backups, name="backups"),
    path("backups/<str:name>", views.backup_download, name="backup_download"),
]
