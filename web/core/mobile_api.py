"""Versioned operator API. No print/motion/heating commands are exposed."""

import hashlib
import json
import secrets
from datetime import timedelta
from functools import wraps

from asgiref.sync import async_to_sync
from django.contrib.auth import authenticate
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters

from accounts import lockout
from accounts.roles import has_right
from printers.drivers import base, client_for
from printers.drivers.base import DriverError
from printers.models import Printer, PrinterFault
from printers.state import read_state, view_state, write_state

from . import audit
from .mobile_models import MobileSession
from .models import StudioSettings
from .telegram_photos import PhotoProvider, PhotoUnavailable


def error(message, status):
    return JsonResponse({"error": message}, status=status)


def payload(request):
    if len(request.body) > 8192:
        raise ValueError
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError
    return data


def endpoint(method="GET", authenticated=True):
    def decorate(view):
        @csrf_exempt  # Native bearer tokens, never cookie/session authentication.
        @login_not_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method != method:
                return error("Метод недоступен", 405)
            if authenticated:
                raw = request.headers.get("Authorization", "")
                token = raw.removeprefix("Bearer ") if raw.startswith("Bearer ") else ""
                session = (
                    MobileSession.objects.select_related("user")
                    .filter(
                        token_hash=hashlib.sha256(token.encode()).hexdigest(),
                        expires_at__gt=timezone.now(),
                        user__is_active=True,
                    )
                    .first()
                    if token
                    else None
                )
                if session is None:
                    return error("Войдите в аккаунт заново", 401)
                if not secrets.compare_digest(session.auth_hash, session.user.get_session_auth_hash()):
                    session.delete()
                    return error("Пароль изменён. Войдите заново", 401)
                request.user = session.user
                request.mobile_session = session
                if not has_right(request.user, "view_dashboard"):
                    return error("Нет права просмотра принтеров", 403)
                if session.last_used_at < timezone.now() - timedelta(hours=1):
                    MobileSession.objects.filter(pk=session.pk).update(last_used_at=timezone.now())
            response = view(request, *args, **kwargs)
            response["Cache-Control"] = "no-store"
            return response

        return wrapped

    return decorate


@endpoint("POST", authenticated=False)
@sensitive_post_parameters("password")
def login(request):
    ip = lockout.client_ip(request)
    if lockout.is_locked(ip):
        return error("Вход временно заблокирован. Повторите позже", 429)
    try:
        data = payload(request)
        username, password = data.get("username", ""), data.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError
        user = authenticate(request, username=username, password=password)
    except (ValueError, UnicodeError):
        return error("Некорректный запрос", 400)
    if user is None or not has_right(user, "view_dashboard"):
        lockout.register_failure(ip)
        audit.log(request, audit.Action.LOGIN_FAILED, username=username, via="android")
        return error("Неверный логин, пароль или нет доступа", 401)
    lockout.reset(ip)
    token = secrets.token_urlsafe(48)
    now = timezone.now()
    MobileSession.objects.filter(expires_at__lt=now).delete()
    MobileSession.objects.create(
        user=user,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        auth_hash=user.get_session_auth_hash(),
        device=str(data.get("device") or "Android")[:100],
        expires_at=now + timedelta(days=30),
        last_used_at=now,
    )
    audit.log(request, audit.Action.LOGIN, user=user, via="android")
    return JsonResponse(
        {
            "token": token,
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "name": user.first_name or user.username,
            "api_version": 1,
        }
    )


@endpoint("POST")
def logout(request):
    request.mobile_session.delete()
    return JsonResponse({"ok": True})


def printer_data(printer, user):
    state = view_state(printer, read_state(printer.pk))
    job = dict(state.get("job") or {})
    if job.get("filename"):
        job["filename"] = job["filename"].replace("\\", "/").rsplit("/", 1)[-1]
    return {
        "id": printer.pk,
        "name": printer.name,
        "model": printer.profile_id,
        "enabled": printer.enabled,
        "status": state["status"],
        "label": state["label"],
        "updated": state.get("updated") or 0,
        "job": job or None,
        "temps": state.get("temps") or {},
        "filament": state["filament"],
        "faults": state.get("faults") or [],
        "heating_idle": state.get("heating_idle") or [],
        "light": state.get("light"),
        "can_light": printer.can("light") and has_right(user, "print_control"),
        "camera": bool(printer.profile and printer.profile.camera_type != "none"),
        "print_hours": printer.print_hours,
        "on_hours": printer.on_hours,
    }


@endpoint()
def fleet(request):
    return JsonResponse(
        {
            "studio": StudioSettings.load().name,
            "name": request.user.first_name or request.user.username,
            "timezone": StudioSettings.load().timezone,
            "server_time": timezone.now().isoformat(),
            "printers": [printer_data(p, request.user) for p in Printer.objects.all()],
        }
    )


@endpoint()
def history(request, pk):
    printer = Printer.objects.filter(pk=pk).first()
    if not printer:
        return error("Принтер не найден", 404)
    return JsonResponse(
        {
            "jobs": [
                {
                    "id": job.pk,
                    "filename": job.filename.replace("\\", "/").rsplit("/", 1)[-1],
                    "status": job.get_status_display(),
                    "started_at": job.started_at.isoformat(),
                    "ended_at": job.ended_at.isoformat() if job.ended_at else None,
                    "duration": job.print_duration,
                    "filament_g": job.filament_g,
                    "quality": job.get_quality_display() if job.quality else "Без оценки",
                }
                for job in printer.jobs.order_by("-ended_at")[:30]
            ]
        }
    )


@endpoint()
def events(request):
    items = [
        {
            "printer": fault.printer.name,
            "title": fault.message,
            "at": fault.first_seen.isoformat(),
            "kind": "fault",
        }
        for fault in PrinterFault.objects.select_related("printer")
        .filter(acknowledged_at=None)
        .order_by("-last_seen")[:30]
    ]
    for printer in Printer.objects.all():
        state = view_state(printer, read_state(printer.pk))
        if printer.enabled and state["status"] in (base.FINISHED, base.PAUSED, base.OFFLINE):
            items.append(
                {
                    "printer": printer.name,
                    "title": state["label"],
                    # A live status is not an event timestamp. Do not invent one.
                    "at": "",
                    "kind": state["status"],
                }
            )
    return JsonResponse({"events": items})


@endpoint()
def photo(request, pk):
    printer = Printer.objects.filter(pk=pk).first()
    if not printer:
        return error("Принтер не найден", 404)
    key = f"mobile:photo:{pk}"
    frame = cache.get(key)
    if frame is None:
        try:
            frame = async_to_sync(PhotoProvider().get)(printer)
        except PhotoUnavailable as exc:
            return error(str(exc), 503)
        cache.set(key, frame, timeout=10)
    response = HttpResponse(frame.data, content_type="image/jpeg")
    response["X-Captured-At"] = frame.captured_at.isoformat()
    return response


@endpoint("POST")
def light(request, pk):
    if not has_right(request.user, "print_control"):
        return error("Нет права управления светом", 403)
    printer = Printer.objects.filter(pk=pk).first()
    if not printer or not printer.can("light"):
        return error("Управление светом недоступно", 404)
    try:
        data = payload(request)
        on = data.get("on")
        if type(on) is not bool:
            raise ValueError
        client_for(printer).set_light(on)
    except (ValueError, UnicodeError):
        return error("Укажите on: true или false", 400)
    except DriverError:
        return error("Принтер не принял команду света", 503)
    printer.light_hint = on
    printer.save(update_fields=["light_hint"])
    state = read_state(pk)
    if state:
        write_state(pk, dict(state, light=on))
    audit.log(request, audit.Action.PRINTER_LIGHT, target=printer.name, on=on, via="android")
    return JsonResponse({"ok": True, "on": on})
