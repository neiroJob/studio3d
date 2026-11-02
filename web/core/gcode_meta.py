"""Время печати и вес пластика из комментариев G-code (для изделий заказа).

Слайсеры пишут итоги в начале или в конце файла: PrusaSlicer/OrcaSlicer/Bambu Studio — вес и время
в комментариях, Cura и Creality Print — `;TIME:` в секундах. Если слайсер веса не указал, вернётся `None`
и значение вводится вручную.
"""

import re
from decimal import Decimal

from printers.gcode_check import read_head_tail

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
WEIGHT_RES = [
    re.compile(r"^;\s*total filament weight\s*\[g\]\s*[:=](.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^;\s*filament used\s*\[g\]\s*[:=](.+)$", re.IGNORECASE | re.MULTILINE),
]
TIME_RES = [
    re.compile(r"^;.*total estimated time\s*[:=]\s*([^;\n]+)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^;\s*estimated printing time[^=\n]*=\s*(.+)$", re.IGNORECASE | re.MULTILINE),
]
CURA_TIME_RE = re.compile(r"^;\s*TIME\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE | re.MULTILINE)
DURATION_RE = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _weight(text):
    for regex in WEIGHT_RES:
        match = regex.search(text)
        if match:
            numbers = NUMBER_RE.findall(match.group(1))
            if numbers:  # несколько экструдеров — суммируем
                return sum((Decimal(n) for n in numbers), Decimal("0")).quantize(Decimal("0.01"))
    return None


def _minutes(text):
    seconds = None
    for regex in TIME_RES:
        match = regex.search(text)
        if match:
            parts = DURATION_RE.findall(match.group(1))
            if parts:
                seconds = sum(int(n) * SECONDS[u.lower()] for n, u in parts)
                break
    if seconds is None:
        match = CURA_TIME_RE.search(text)
        if match:
            seconds = float(match.group(1))
    if seconds is None:
        return None
    return max(1, round(seconds / 60))


def parse_gcode_meta(head, tail):
    """`(вес одной печати в граммах, время в минутах)`; чего слайсер не написал — `None`."""
    text = f"{tail or ''}\n{head or ''}"
    return _weight(text), _minutes(text)


def read_gcode_meta(fileobj):
    """То же для файла: Django UploadedFile, FieldFile или любой файловый объект."""
    head, tail = read_head_tail(fileobj)
    return parse_gcode_meta(head, tail)
