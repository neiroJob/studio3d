import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.models import Group
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.roles import ROLE_ADMIN, has_right, right_required, user_rights

from . import audit
from .forms import SetupForm, StudioSettingsForm, TariffForm
from .middleware import setup_done
from .models import AuditLog, ElectricityTariff, StudioSettings

User = get_user_model()
BACKUP_NAME_RE = re.compile(r"^studio3d_[0-9_-]+\.dump$")


@login_not_required
def healthz(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


@login_not_required
def setup(request):
    if setup_done():
        return redirect("login")

    form = SetupForm(request.POST or None, initial={"timezone": settings.TIME_ZONE})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            if User.objects.exists():  # кто-то успел раньше
                return redirect("login")
            user = User.objects.create_user(
                username=data["username"], password=data["password1"], first_name=data["first_name"]
            )
            user.groups.add(Group.objects.get(name=ROLE_ADMIN))
            studio = StudioSettings.load()
            studio.name = data["studio_name"]
            studio.timezone = data["timezone"]
            studio.setup_completed_at = timezone.now()
            studio.save()
            ElectricityTariff.objects.create(
                price_kwh=data["price_kwh"], valid_from=timezone.localdate(), created_by=user
            )
        login(request, user)
        audit.log(
            request,
            audit.Action.SETUP,
            target=studio.name,
            user=user,
            timezone=studio.timezone,
            price_kwh=str(data["price_kwh"]),
        )
        messages.success(request, "Студия настроена. Добро пожаловать!")
        return redirect("dashboard")

    return render(request, "core/setup.html", {"form": form})


def dashboard(request):
    from printers.views import dashboard_context

    context = {"can_view": has_right(request.user, "view_dashboard")}
    if context["can_view"]:
        context.update(dashboard_context(request))
    return render(request, "core/dashboard.html", context)


@right_required("studio_settings", "tariffs_view", "tariffs_edit")
def studio_settings(request):
    rights = user_rights(request.user)
    studio = StudioSettings.load()
    settings_form = StudioSettingsForm(instance=studio) if "studio_settings" in rights else None
    tariff_form = TariffForm() if "tariffs_edit" in rights else None

    if request.method == "POST":
        what = request.POST.get("form")
        if what == "studio" and settings_form is not None:
            settings_form = StudioSettingsForm(request.POST, instance=studio)
            if settings_form.is_valid():
                settings_form.save()
                audit.log(
                    request,
                    audit.Action.SETTINGS,
                    target=studio.name,
                    name=studio.name,
                    timezone=studio.timezone,
                )
                messages.success(request, "Настройки сохранены.")
                return redirect("settings")
        elif what == "tariff" and tariff_form is not None:
            tariff_form = TariffForm(request.POST)
            if tariff_form.is_valid():
                tariff = tariff_form.save(commit=False)
                tariff.created_by = request.user
                tariff.save()
                audit.log(
                    request,
                    audit.Action.TARIFF,
                    target=str(tariff),
                    price_kwh=str(tariff.price_kwh),
                    valid_from=str(tariff.valid_from),
                )
                messages.success(request, "Тариф добавлен.")
                return redirect("settings")

    return render(
        request,
        "core/settings.html",
        {
            "studio": studio,
            "settings_form": settings_form,
            "tariff_form": tariff_form,
            "tariffs": ElectricityTariff.objects.select_related("created_by")[:50],
            "current_tariff": ElectricityTariff.on_date(),
        },
    )


@right_required("users_manage")
def journal(request):
    qs = AuditLog.objects.all()
    action = request.GET.get("action", "")
    if action:
        qs = qs.filter(action=action)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "core/journal.html",
        {
            "page": page,
            "actions": AuditLog.Action.choices,
            "action": action,
        },
    )


def _backup_files():
    folder = settings.BACKUP_DIR
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and BACKUP_NAME_RE.match(p.name)]
    return sorted(files, key=lambda p: p.name, reverse=True)


@right_required("studio_settings")
def backups(request):
    items = [
        {
            "name": p.name,
            "size_mb": p.stat().st_size / 1024 / 1024,
            "mtime": timezone.datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.get_current_timezone()),
        }
        for p in _backup_files()
    ]
    return render(request, "core/backups.html", {"items": items, "backup_dir": settings.BACKUP_DIR})


@right_required("studio_settings")
@require_POST
def backup_download(request, name):
    if not BACKUP_NAME_RE.match(name):
        raise Http404
    path = settings.BACKUP_DIR / name
    if not path.is_file():
        raise Http404
    audit.log(request, audit.Action.BACKUP_DOWNLOAD, target=name)
    return FileResponse(path.open("rb"), as_attachment=True, filename=name)
