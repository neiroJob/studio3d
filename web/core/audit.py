from accounts.lockout import client_ip

from .models import AuditLog

Action = AuditLog.Action


def log(request, action, target="", user=None, username=None, **details):
    """Записать действие в журнал. user/username — если действие выполнил не текущий пользователь."""
    if user is None and request is not None and request.user.is_authenticated:
        user = request.user
    ip = client_ip(request) if request is not None else None
    return AuditLog.objects.create(
        user=user,
        username=username if username is not None else (user.username if user else ""),
        action=action,
        target=str(target)[:255],
        ip=ip or None,
        details=details,
    )
