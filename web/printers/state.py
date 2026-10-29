"""Живое состояние принтеров: коллектор пишет его в кэш (Redis), веб читает."""

import logging
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .drivers import base

STATE_TTL = 30  # сек: если коллектор молчит дольше, принтер считается без связи
log = logging.getLogger(__name__)

STATUS_COLORS = {
    base.OFFLINE: "--st-off",
    base.READY: "--st-idle",
    base.PRINTING: "--st-print",
    base.PAUSED: "--st-pause",
    base.ERROR: "--st-err",
    base.FINISHED: "--st-done",
    base.MAINTENANCE: "--st-maint",
}


def _key(printer_id):
    return f"printer:state:{printer_id}"


def write_state(printer_id, state):
    _persist_faults(printer_id, state)
    cache.set(_key(printer_id), state, timeout=STATE_TTL)


def _persist_faults(printer_id, state):
    """Latch reported printer faults in the database until a user acknowledges them."""
    from .models import PrinterFault

    messages = list(
        dict.fromkeys(
            str(item).strip()
            for item in state.get("faults") or []
            if str(item).strip() and base.show_fault_on_dashboard(item)
        )
    )
    message = str(state.get("message") or "").strip()
    if state.get("status") == base.ERROR and base.show_fault_on_dashboard(message) and message not in messages:
        messages.append(message)

    try:
        now = timezone.now()
        with transaction.atomic():
            active_rows = list(
                PrinterFault.objects.select_for_update().filter(printer_id=printer_id, active=True)
            )
            active_by_message = {fault.message: fault for fault in active_rows}
            for fault in active_rows:
                if fault.message not in messages:
                    fault.active = False
                    fault.save(update_fields=["active"])
            for text in messages:
                fault = active_by_message.get(text)
                if fault is None:
                    PrinterFault.objects.create(printer_id=printer_id, message=text)
                elif fault.last_seen < now - timedelta(seconds=20):
                    fault.last_seen = now
                    fault.save(update_fields=["last_seen"])
    except Exception:  # noqa: BLE001 — Redis state must still update if DB fault logging fails
        log.exception("Could not persist printer faults for printer %s", printer_id)


def read_state(printer_id):
    return cache.get(_key(printer_id))


def read_states(printer_ids):
    keys = {_key(pk): pk for pk in printer_ids}
    found = cache.get_many(list(keys))
    return {keys[k]: v for k, v in found.items()}


def view_state(printer, state=None):
    """Состояние для шаблонов: единая модель + признаки из БД («ждёт снятия»)."""
    state = dict(state or base.offline_state())
    pending = list(printer.fault_events.filter(acknowledged_at__isnull=True).values_list("message", flat=True))
    acknowledged_active = set(
        printer.fault_events.filter(acknowledged_at__isnull=False, active=True).values_list("message", flat=True)
    )
    current = [
        str(item)
        for item in state.get("faults") or []
        if str(item) not in acknowledged_active and base.show_fault_on_dashboard(item)
    ]
    state["faults"] = list(dict.fromkeys(pending + current))
    if state.get("message") in acknowledged_active or not base.show_fault_on_dashboard(state.get("message")):
        state["message"] = ""
    filament = state.get("filament")
    if not isinstance(filament, dict):
        filament = {"present": None, "enabled": None}
    state["filament"] = filament
    status = state.get("status", base.OFFLINE)
    if status == base.READY and printer.awaiting_removal:
        status = base.FINISHED
    state["status"] = status
    state["label"] = base.STATUS_LABELS.get(status, status)
    state["color"] = STATUS_COLORS.get(status, "--st-off")
    state["age"] = time.time() - float(state.get("updated") or 0)
    # Нагрев без печати: например, макрос отмены на принтере оборвался и не выключил нагреватели.
    names = {"nozzle": "сопло", "bed": "стол", "chamber": "камера"}
    temps = state.get("temps") or {}
    state["heating_idle"] = []
    if status not in (base.PRINTING, base.PAUSED, base.OFFLINE):
        state["heating_idle"] = [
            f"{names[k]} {round(v[1])} °C"
            for k, v in temps.items()
            if k in names and v and float(v[1] or 0) > 0
        ]
    job = state.get("job")
    if job:
        job["percent"] = round(float(job.get("progress") or 0) * 100)
        total_g = job.get("filament_g_total")
        job["filament_g_now"] = round(total_g * job["progress"]) if total_g else None
    # Модели без живого статуса света (K1C: свет только отдельной командой, не в телеметрии) —
    # последняя отправленная команда, пока принтер не скажет точнее.
    if state.get("light") is None and printer.light_hint is not None:
        state["light"] = printer.light_hint
    return state


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"
