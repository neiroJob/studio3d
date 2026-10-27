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
def gcode_name(path):
    """«.cache/деталь.gcode» → «деталь.gcode»: служебные скрытые папки QIDI не показываем."""
    if not path:
        return ""
    parts = [p for p in str(path).split("/") if p]
    while len(parts) > 1 and parts[0].startswith("."):
        parts.pop(0)
    return "/".join(parts)
