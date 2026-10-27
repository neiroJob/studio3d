import zoneinfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.shortcuts import redirect
from django.utils import timezone

from .models import TZ_CACHE_KEY, StudioSettings

SETUP_DONE_KEY = "studio:setup_done"
OPEN_PREFIXES = ("/setup/", "/static/", "/media/", "/healthz")


def setup_done():
    """Мастер первого запуска пройден, если в системе есть хотя бы один пользователь."""
    if cache.get(SETUP_DONE_KEY):
        return True
    done = get_user_model().objects.exists()
    if done:
        cache.set(SETUP_DONE_KEY, True, timeout=None)
    return done


class FirstRunMiddleware:
    """Пока нет ни одного пользователя, все страницы ведут в мастер первого запуска."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(OPEN_PREFIXES) and not setup_done():
            return redirect("setup")
        return self.get_response(request)


class StudioTimezoneMiddleware:
    """Время хранится в UTC, показывается в часовом поясе студии из настроек."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tz_name = cache.get(TZ_CACHE_KEY)
        if tz_name is None:
            settings_obj = StudioSettings.objects.filter(pk=1).only("timezone").first()
            tz_name = settings_obj.timezone if settings_obj else ""
            cache.set(TZ_CACHE_KEY, tz_name, timeout=300)
        if tz_name:
            try:
                timezone.activate(zoneinfo.ZoneInfo(tz_name))
            except zoneinfo.ZoneInfoNotFoundError:
                timezone.deactivate()
        else:
            timezone.deactivate()
        return self.get_response(request)
