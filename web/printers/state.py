"""Живое состояние принтеров: коллектор пишет его в кэш (Redis), веб читает."""

import time

from django.core.cache import cache

from .drivers import base

STATE_TTL = 30  # сек: если коллектор молчит дольше, принтер считается без связи

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
    cache.set(_key(printer_id), state, timeout=STATE_TTL)


def read_state(printer_id):
    return cache.get(_key(printer_id))


def read_states(printer_ids):
    keys = {_key(pk): pk for pk in printer_ids}
    found = cache.get_many(list(keys))
    return {keys[k]: v for k, v in found.items()}


def view_state(printer, state=None):
    """Состояние для шаблонов: единая модель + признаки из БД («ждёт снятия»)."""
    state = dict(state or base.offline_state())
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
    return state


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"
