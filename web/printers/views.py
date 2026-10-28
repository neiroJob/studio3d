import json
import re
import time

from django.contrib import messages
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.roles import right_required, user_rights
from core import audit

from .drivers import DriverError, base, client_for
from .drivers.moonraker import fetch_snapshot
from .forms import PrinterForm, UploadForm
from .gcode_check import check_gcode, read_head_tail
from .models import Printer
from .profiles import get_profile, manufacturers
from .state import read_state, read_states, view_state, write_state

FILTERS = [
    ("all", "Все принтеры", "--ink3"),
    (base.PRINTING, "Печатают", "--st-print"),
    (base.FINISHED, "Ждут снятия", "--st-done"),
    (base.PAUSED, "На паузе", "--st-pause"),
    (base.ERROR, "Ошибки", "--st-err"),
    (base.OFFLINE, "Нет связи", "--st-off"),
]

NO_SIGNAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200">'
    '<rect width="320" height="200" fill="#202830"/>'
    '<text x="160" y="105" fill="#9aa4ad" font-size="15" text-anchor="middle" '
    'font-family="system-ui,sans-serif">Камера недоступна</text></svg>'
)


def _items(printers):
    states = read_states([p.pk for p in printers])
    return [{"printer": p, "state": view_state(p, states.get(p.pk))} for p in printers]


def dashboard_context(request):
    """Плитки принтеров и счётчики для дашборда."""
    items = _items(list(Printer.objects.all()))
    counts = {key: 0 for key, _, _ in FILTERS}
    counts["all"] = len(items)
    for it in items:
        counts[it["state"]["status"]] = counts.get(it["state"]["status"], 0) + 1
    current = request.GET.get("f", "all")
    if current != "all":
        items = [it for it in items if it["state"]["status"] == current]
    strip = [
        {"key": k, "label": label, "color": color, "count": counts.get(k, 0)} for k, label, color in FILTERS
    ]
    attention = [
        it
        for it in _items(list(Printer.objects.all()))
        if it["state"]["status"] in (base.ERROR, base.FINISHED, base.PAUSED) or it["state"]["heating_idle"]
    ]
    back = reverse("dashboard") + (f"?f={current}" if current != "all" else "")
    return {"items": items, "strip": strip, "filter": current, "attention": attention, "back": back}


@right_required("view_dashboard")
def tiles(request):
    return render(request, "printers/_tiles.html", dashboard_context(request))


def _redirect_back(request, printer):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
        return redirect(nxt)
    return redirect("printer_detail", pk=printer.pk)


@right_required("view_dashboard")
def detail(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    rights = user_rights(request.user)
    tab = request.GET.get("tab", "now")
    ctx = {
        "printer": printer,
        "profile": printer.profile,
        "state": view_state(printer, read_state(printer.pk)),
        "tab": tab,
        "web_ui": printer.profile.web_ui_for(printer.host) if printer.profile else "",
        "camera_page_url": printer.profile.page_url_for(printer.host) if printer.profile else "",
        "can_control": "print_control" in rights,
    }
    if tab == "files":
        if not ctx["can_control"]:
            return redirect("printer_detail", pk=pk)
        try:
            ctx["files"] = client_for(printer).list_files()[:200]
        except DriverError as e:
            ctx["files_error"] = str(e)
        ctx["upload_form"] = UploadForm()
    elif tab == "history":
        ctx["jobs"] = printer.jobs.all()[:100]
    elif tab == "hours":
        ctx["jobs_count"] = printer.jobs.count()
    return render(request, "printers/detail.html", ctx)


@right_required("view_dashboard")
def live(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    return render(
        request,
        "printers/_live.html",
        {
            "printer": printer,
            "state": view_state(printer, read_state(printer.pk)),
            "can_control": "print_control" in user_rights(request.user),
        },
    )


THUMBNAIL_NAME_RE = re.compile(r"^[^/\\]+\.(gcode|gco|g)$", re.IGNORECASE)


@right_required("view_dashboard")
def file_thumbnail(request, pk, name):
    printer = get_object_or_404(Printer, pk=pk)
    thumbnail = getattr(client_for(printer), "thumbnail", None) if printer.profile else None
    if thumbnail is None or not THUMBNAIL_NAME_RE.match(name):
        raise Http404
    key = f"printer:thumb:{pk}:{name}"
    image = cache.get(key)
    if image is None:
        image = thumbnail(name) or b""
        cache.set(key, image, timeout=300)
    if not image:
        raise Http404
    resp = HttpResponse(image, content_type="image/png")
    resp["Cache-Control"] = "private, max-age=300"
    return resp


@right_required("view_dashboard")
def snapshot(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    key = f"printer:snapshot:{pk}"
    image = cache.get(key)
    if image is None:
        url = printer.profile.snapshot_url_for(printer.host) if printer.profile else ""
        image = (fetch_snapshot(url, timeout=4) if url else None) or b""
        cache.set(key, image, timeout=3)
    if not image:
        resp = HttpResponse(NO_SIGNAL_SVG, content_type="image/svg+xml")
    else:
        resp = HttpResponse(image, content_type="image/jpeg")
    resp["Cache-Control"] = "no-store"
    return resp


# --- команды ----------------------------------------------------------------------------------

COMMANDS = {
    "pause": ("pause", "Пауза отправлена.", audit.Action.PRINT_PAUSE),
    "resume": ("resume", "Печать продолжается.", audit.Action.PRINT_RESUME),
    "cancel": ("cancel", "Печать отменена.", audit.Action.PRINT_CANCEL),
    "heaters_off": ("heaters_off", "Нагреватели выключены.", audit.Action.HEATERS_OFF),
}
ALWAYS_AVAILABLE = {"heaters_off"}  # стандартные команды Klipper, в профиле не описываются


def _klipper_error(printer):
    """Последняя ошибка Klipper из сообщений принтера — она понятнее, чем ответ HTTP."""
    state = read_state(printer.pk) or {}
    errors = [line for line in state.get("console") or [] if line.startswith("!!")]
    return errors[-1].lstrip("! ").strip() if errors else ""


@right_required("print_control")
@require_POST
def command(request, pk, action):
    printer = get_object_or_404(Printer, pk=pk)
    if action not in COMMANDS or not (action in ALWAYS_AVAILABLE or printer.can(action)):
        messages.error(request, "Эта модель не поддерживает команду.")
        return _redirect_back(request, printer)
    method, ok_text, audit_action = COMMANDS[action]
    try:
        getattr(client_for(printer), method)()
    except DriverError as e:
        text = f"{printer.name}: {e}"
        klipper = _klipper_error(printer)
        if klipper:
            text += f". Klipper: {klipper}"
        if action == "cancel":
            text += (
                ". Макрос отмены на принтере не выполнился до конца — проверьте, что печать остановлена "
                "и нагреватели выключены."
            )
        messages.error(request, text)
        audit.log(request, audit_action, target=printer.name, error=str(e), klipper=klipper)
    else:
        audit.log(request, audit_action, target=printer.name)
        messages.success(request, f"{printer.name}: {ok_text}")
    return _redirect_back(request, printer)


@right_required("print_control")
@require_POST
def light(request, pk, mode):
    printer = get_object_or_404(Printer, pk=pk)
    if mode not in ("on", "off") or not printer.can("light"):
        messages.error(request, "У этой модели нет управления светом.")
        return _redirect_back(request, printer)
    try:
        client_for(printer).set_light(mode == "on")
    except DriverError as e:
        messages.error(request, f"{printer.name}: {e}")
    else:
        audit.log(request, audit.Action.PRINTER_LIGHT, target=printer.name, on=mode == "on")
        # Состояние в кэше обновит коллектор; отражаем сразу, чтобы кнопка не «мигала».
        state = read_state(printer.pk)
        if state:
            state["light"] = mode == "on"
            write_state(printer.pk, state)
    return _redirect_back(request, printer)


@right_required("print_plate")
@require_POST
def plate_cleared(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    printer.awaiting_removal = False
    printer.save(update_fields=["awaiting_removal"])
    audit.log(request, audit.Action.PLATE_CLEARED, target=printer.name)
    messages.success(request, f"{printer.name}: изделие снято, принтер готов.")
    return _redirect_back(request, printer)


def _state_blocks_start(printer):
    state = view_state(printer, read_state(printer.pk))
    if state["status"] == base.FINISHED:
        return "Сначала отметьте, что изделие снято со стола."
    if state["status"] in (base.PRINTING, base.PAUSED):
        return "Принтер занят: идёт печать."
    if state["status"] == base.OFFLINE:
        return "Нет связи с принтером."
    return ""


@right_required("print_control")
@require_POST
def start(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    filename = request.POST.get("filename", "")
    confirmed = request.POST.get("confirm") == "1"
    blocked = _state_blocks_start(printer)
    if blocked:
        messages.error(request, f"{printer.name}: {blocked}")
    elif not printer.can("start_file") or not filename:
        messages.error(request, "Запуск файла недоступен.")
    else:
        warnings = []
        if not confirmed:
            try:
                head, tail = client_for(printer).read_head_tail(filename)
                warnings = check_gcode(head, tail, printer.profile)
            except DriverError:
                warnings = []  # не смогли прочитать — не мешаем запуску, Klipper всё равно проверит
        if warnings:
            return render(
                request,
                "printers/confirm_start.html",
                {
                    "printer": printer,
                    "filename": filename,
                    "warnings": warnings,
                },
            )
        try:
            client_for(printer).start(filename)
        except DriverError as e:
            messages.error(request, f"{printer.name}: {e}")
        else:
            audit.log(
                request,
                audit.Action.PRINT_START,
                target=printer.name,
                filename=filename,
                warnings_confirmed=confirmed,
            )
            messages.success(request, f"{printer.name}: печать «{filename}» запущена.")
            return redirect("printer_detail", pk=pk)
    return _redirect_back(request, printer)


@right_required("print_control")
@require_POST
def check_file(request, pk):
    """Проверка G-code до загрузки: браузер присылает начало и конец файла (head, tail)."""
    printer = get_object_or_404(Printer, pk=pk)
    warnings = check_gcode(request.POST.get("head", ""), request.POST.get("tail", ""), printer.profile)
    return JsonResponse({"warnings": warnings})


UPLOAD_ID_RE = re.compile(r"^[a-z0-9]{8,40}$")


def _upload_key(pk, upload_id):
    return f"printer:upload:{pk}:{upload_id}"


@right_required("print_control")
@require_POST
def upload(request, pk):
    """Загрузка G-code на принтер.

    Браузер отправляет файл через XMLHttpRequest и сам показывает прогресс загрузки на сервер.
    Вторую часть — передачу с сервера на принтер — он узнаёт опросом upload_progress по upload_id.
    """
    printer = get_object_or_404(Printer, pk=pk)
    is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    upload_id = request.POST.get("upload_id", "")
    key = _upload_key(pk, upload_id) if UPLOAD_ID_RE.match(upload_id) else None
    form = UploadForm(request.POST, request.FILES)
    target = reverse("printer_detail", args=[pk]) + "?tab=files"
    if not printer.can("upload"):
        messages.error(request, "Эта модель не поддерживает загрузку файлов.")
    elif not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    else:
        f = form.cleaned_data["file"]
        start_now = form.cleaned_data["start"]
        blocked = _state_blocks_start(printer) if start_now else ""
        warnings = check_gcode(*read_head_tail(f), printer.profile)
        confirmed = request.POST.get("confirm_warnings") == "1"
        if warnings and not confirmed:
            messages.error(request, f"{printer.name}: файл не загружен. " + " ".join(warnings))
        elif blocked:
            messages.error(request, f"{printer.name}: {blocked} Файл не загружен.")
        else:

            def progress(sent, total):
                if key:
                    cache.set(key, {"sent": sent, "total": total}, timeout=600)

            try:
                client_for(printer).upload(f, f.name, start=start_now, progress=progress)
            except DriverError as e:
                messages.error(request, f"{printer.name}: {e}")
            else:
                audit.log(
                    request,
                    audit.Action.FILE_UPLOAD,
                    target=printer.name,
                    filename=f.name,
                    start=start_now,
                    warnings_confirmed=bool(warnings),
                )
                text = "загружен и запущен" if start_now else "загружен"
                messages.success(request, f"{printer.name}: файл «{f.name}» {text}.")
                if start_now:
                    target = reverse("printer_detail", args=[pk])
    if key:
        cache.delete(key)
    if is_xhr:
        return JsonResponse({"redirect": target})
    return redirect(target)


@right_required("print_control")
def upload_progress(request, pk, upload_id):
    if not UPLOAD_ID_RE.match(upload_id):
        return JsonResponse({}, status=404)
    return JsonResponse(cache.get(_upload_key(pk, upload_id)) or {})


# --- добавление и настройки -------------------------------------------------------------------


def _profiles_json():
    return json.dumps(
        {
            man: [
                {
                    "id": p.id,
                    "model": p.model,
                    "port": p.port,
                    "power_w": p.power_w,
                    "camera": bool(p.snapshot_url or p.page_url),
                    "verified": p.verified,
                }
                for p in items
            ]
            for man, items in manufacturers().items()
        },
        ensure_ascii=False,
    )


@right_required("printers_manage")
def add(request):
    form = PrinterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        printer = form.save(commit=False)
        if form.cleaned_data.get("print_hours_base") is None:
            try:
                totals = client_for(
                    profile=printer.profile, host=printer.host, port=printer.port, api_key=printer.api_key
                ).history_totals()
                printer.print_seconds_base = int(totals["print_seconds"])
            except DriverError:
                printer.print_seconds_base = 0
        printer.history_since = time.time()
        printer.save()
        audit.log(
            request,
            audit.Action.PRINTER_ADD,
            target=printer.name,
            model=printer.profile_id,
            host=printer.host,
            base_hours=round(printer.print_hours, 1),
        )
        messages.success(request, f"Принтер {printer.name} добавлен. Состояние появится в течение 10 секунд.")
        return redirect("printer_detail", pk=printer.pk)
    return render(
        request,
        "printers/form.html",
        {
            "form": form,
            "title": "Новый принтер",
            "profiles_json": _profiles_json(),
        },
    )


@right_required("printers_manage")
def edit(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    form = PrinterForm(request.POST or None, instance=printer)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.log(
            request,
            audit.Action.PRINTER_UPDATE,
            target=printer.name,
            changed=form.changed_data and [f for f in form.changed_data if f != "api_key"],
        )
        messages.success(request, f"Настройки {printer.name} сохранены.")
        return redirect("printer_detail", pk=pk)
    return render(
        request,
        "printers/form.html",
        {
            "form": form,
            "printer": printer,
            "title": f"Настройки: {printer.name}",
            "profiles_json": _profiles_json(),
        },
    )


@right_required("printers_manage")
@require_POST
def delete(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    name = printer.name
    printer.delete()
    audit.log(request, audit.Action.PRINTER_DELETE, target=name)
    messages.success(request, f"Принтер {name} удалён.")
    return redirect("dashboard")


@right_required("printers_manage")
@require_POST
def check(request):
    """«Проверить связь» в форме: HTMX-фрагмент с результатом."""
    profile = get_profile(request.POST.get("profile_id", ""))
    host = request.POST.get("host", "").strip()
    ctx = {}
    if profile is None:
        ctx["error"] = "Сначала выберите модель."
    elif not host:
        ctx["error"] = "Укажите IP-адрес."
    else:
        form = PrinterForm(data={**request.POST.dict(), "name": "check", "amortization_hours": 1})
        form.is_valid()
        host = form.cleaned_data.get("host", host)
        port = request.POST.get("port") or profile.port
        try:
            client = client_for(
                profile=profile, host=host, port=int(port), api_key=request.POST.get("api_key", "")
            )
            ctx["result"] = client.check()
            try:
                ctx["hours"] = round(client.history_totals()["print_seconds"] / 3600, 1)
            except DriverError:
                ctx["hours"] = None
        except (DriverError, ValueError) as e:
            ctx["error"] = str(e)
        ctx["profile"] = profile
    return render(request, "printers/_check.html", ctx)
