from datetime import datetime
from zoneinfo import ZoneInfo

from printers.drivers import base

RESULTS = {"completed": "Успех", "cancelled": "Отменено", "error": "Ошибка", "interrupted": "Прервано"}


def duration(seconds):
    if seconds is None:
        return "нет данных"
    minutes = max(0, int(seconds)) // 60
    return f"{minutes // 60} ч {minutes % 60:02d} мин" if minutes >= 60 else f"{minutes} мин"


def clock(value, zone):
    if not value:
        return "нет данных"
    if isinstance(value, (float, int)):
        value = datetime.fromtimestamp(value, tz=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo(zone)).strftime("%d.%m %H:%M:%S")


def status_text(printer, state, zone, title="", event_at=None):
    status = state.get("status", base.OFFLINE)
    lines = [f"{title + ': ' if title else ''}{printer.name}", base.STATUS_LABELS.get(status, status)]
    if event_at:
        lines.append(f"Событие: {clock(event_at, zone)}")
    job = state.get("job") or {}
    if job:
        name = (job.get("filename") or "—").replace("\\", "/").rsplit("/", 1)[-1]
        lines += [
            f"Файл: {name}",
            f"Прогресс: {round(float(job.get('progress') or 0) * 100)} %",
            f"Прошло: {duration(job.get('elapsed'))}",
        ]
        if status in (base.PRINTING, base.PAUSED):
            lines.append(f"Осталось: {duration(job.get('remaining'))}")
            if status == base.PRINTING and job.get("remaining") is not None and state.get("updated"):
                lines.append(f"Окончание ≈ {clock(state['updated'] + job['remaining'], zone)}")
        if job.get("layer") is not None:
            lines.append(f"Слой: {job['layer']} / {job.get('total_layers') or '—'}")
        if job.get("filament_g_total"):
            lines.append(f"Пластик по G-code: {job['filament_g_total']:g} г")
    if status == base.PAUSED:
        lines.append("Печать на паузе; оценка окончания изменится после продолжения.")
    if status == base.FINISHED:
        lines.append("Снимите изделие и подтвердите снятие в веб-интерфейсе.")
    if state.get("result"):
        lines.append(f"Результат: {RESULTS.get(state['result'], state['result'])}")
    filament = state.get("filament") or {}
    present = filament.get("present")
    lines.append("Нить: " + ("есть" if present is True else "нет" if present is False else "нет данных"))
    temps = []
    for key, label in [("nozzle", "сопло"), ("bed", "стол"), ("chamber", "камера")]:
        pair = (state.get("temps") or {}).get(key)
        if pair:
            temps.append(f"{label} {pair[0]:g}/{pair[1]:g} °C")
    if temps:
        lines.append("Температуры: " + "; ".join(temps))
    for fault in (state.get("faults") or [])[:3]:
        lines.append(f"Внимание: {fault}")
    if state.get("message") and not state.get("faults"):
        lines.append(state["message"])
    if state.get("heating_idle"):
        lines.append("Нагрев без печати: " + ", ".join(state["heating_idle"]))
    lines.append(f"Данные: {clock(state.get('updated'), zone)}")
    return "\n".join(lines)
