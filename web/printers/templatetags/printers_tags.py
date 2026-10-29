from django import template

from printers.state import fmt_duration

register = template.Library()


@register.filter
def duration(seconds):
    """3725 → «1 ч 02 мин»."""
    return fmt_duration(seconds)


@register.filter
def hours(seconds):
    """Секунды → часы с одним знаком: 5400 → «1,5»."""
    if seconds is None:
        return "—"
    return f"{seconds / 3600:.1f}".replace(".", ",")


@register.filter
def filesize(num):
    num = float(num or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024 or unit == "ГБ":
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num:.1f} {unit}".replace(".", ",")
        num /= 1024
    return ""


@register.filter
def unix_date(ts):
    """Unix time → «27.10.2026 14:05» в часовом поясе студии."""
    import datetime as dt

    from django.utils import timezone

    if not ts:
        return "—"
    return timezone.localtime(dt.datetime.fromtimestamp(float(ts), tz=dt.UTC)).strftime("%d.%m.%Y %H:%M")


@register.filter
def eta(remaining_seconds):
    """Секунды до конца печати → часы в часовом поясе студии: 5400 → «16:30»."""
    if remaining_seconds is None:
        return "—"
    import datetime as dt

    from django.utils import timezone

    finish = timezone.now() + dt.timedelta(seconds=float(remaining_seconds))
    return timezone.localtime(finish).strftime("%H:%M")


@register.filter
def can(printer, capability):
    return printer.can(capability)


@register.filter
def basename(path):
    """Только имя файла, без пути: «.cache/деталь.gcode» и «/usr/data/.../деталь.gcode» → «деталь.gcode»."""
    return str(path or "").rsplit("/", 1)[-1]
