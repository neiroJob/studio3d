from accounts.roles import user_rights
from studio3d import __version__

from .models import StudioSettings


def studio(request):
    if request.path.startswith(("/static/", "/healthz")):
        return {}
    settings_obj = StudioSettings.objects.filter(pk=1).first()
    return {
        "studio_name": settings_obj.name if settings_obj else "Студия 3D",
        "rights": user_rights(request.user) if hasattr(request, "user") else set(),
        "app_version": __version__,
    }
