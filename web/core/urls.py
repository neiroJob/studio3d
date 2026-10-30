from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("healthz", views.healthz, name="healthz"),
    path("setup/", views.setup, name="setup"),
    path("settings/", views.studio_settings, name="settings"),
    path("journal/", views.journal, name="journal"),
    path("stock/", views.stock, name="stock"),
    path("stock/groups/add/", views.stock_group_edit, name="stock_group_create"),
    path("stock/groups/<int:pk>/", views.stock_group_edit, name="stock_group_edit"),
    path("stock/groups/<int:pk>/delete/", views.stock_group_delete, name="stock_group_delete"),
    path("stock/items/add/", views.stock_item_edit, name="stock_item_create"),
    path("stock/items/<int:pk>/", views.stock_item_edit, name="stock_item_edit"),
    path("stock/items/<int:pk>/archive/", views.stock_item_archive, name="stock_item_archive"),
    path("backups/", views.backups, name="backups"),
    path("backups/<str:name>", views.backup_download, name="backup_download"),
]
