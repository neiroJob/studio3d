"""Блокировка входа по IP: после N неудачных попыток вход с IP закрыт на M минут (ТЗ, раздел 3)."""

from django.conf import settings
from django.core.cache import cache


def client_ip(request):
    # Caddy перезаписывает X-Forwarded-For реальным адресом клиента; берём последний адрес цепочки.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def _fail_key(ip):
    return f"login:fail:{ip}"


def _lock_key(ip):
    return f"login:lock:{ip}"


def lock_seconds():
    return settings.LOGIN_LOCK_MINUTES * 60


def is_locked(ip):
    return cache.get(_lock_key(ip)) is not None


def register_failure(ip):
    """Учесть неудачную попытку. Возвращает True, если IP теперь заблокирован."""
    key = _fail_key(ip)
    cache.add(key, 0, timeout=lock_seconds())
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=lock_seconds())
        count = 1
    if count >= settings.LOGIN_MAX_ATTEMPTS:
        cache.set(_lock_key(ip), 1, timeout=lock_seconds())
        cache.delete(key)
        return True
    return False


def reset(ip):
    cache.delete(_fail_key(ip))
    cache.delete(_lock_key(ip))
