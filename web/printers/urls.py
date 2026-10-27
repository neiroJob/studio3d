from django.urls import path

from . import views

urlpatterns = [
    path("printers/tiles/", views.tiles, name="printer_tiles"),
    path("printers/add/", views.add, name="printer_add"),
    path("printers/check/", views.check, name="printer_check"),
    path("printers/<int:pk>/", views.detail, name="printer_detail"),
    path("printers/<int:pk>/live/", views.live, name="printer_live"),
    path("printers/<int:pk>/snapshot.jpg", views.snapshot, name="printer_snapshot"),
    path("printers/<int:pk>/edit/", views.edit, name="printer_edit"),
    path("printers/<int:pk>/delete/", views.delete, name="printer_delete"),
    path("printers/<int:pk>/start/", views.start, name="printer_start"),
    path("printers/<int:pk>/check-file/", views.check_file, name="printer_check_file"),
    path("printers/<int:pk>/upload/", views.upload, name="printer_upload"),
    path("printers/<int:pk>/upload/<str:upload_id>/", views.upload_progress, name="printer_upload_progress"),
    path("printers/<int:pk>/cleared/", views.plate_cleared, name="printer_cleared"),
    path("printers/<int:pk>/light/<str:mode>/", views.light, name="printer_light"),
    path("printers/<int:pk>/<str:action>/", views.command, name="printer_command"),
]
