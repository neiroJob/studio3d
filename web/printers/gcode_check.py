"""Проверка G-code: подходит ли файл этой модели принтера.

Пример, ради которого это сделано (2026-10-27, QIDI Q1 Pro): файл нарезан общим профилем Klipper,
в начале стоит START_PRINT вместо PRINT_START. Klipper пропускает незнакомую команду, оси не паркуются,
и первое же движение падает с «Must home axis first».

Проверяются начало файла (первые команды) и конец (там слайсеры пишут «; printer_model = …»).
"""

import re

HEAD_BYTES = 512 * 1024
TAIL_BYTES = 128 * 1024
MAX_COMMANDS = 600

# Распространённые имена стартовых макросов в профилях разных принтеров.
KNOWN_START_MACROS = {"PRINT_START", "START_PRINT", "START", "PRINTSTART", "STARTPRINT", "START_GCODE"}
TOOL_RE = re.compile(r"^T\d+$")
MODEL_RE = re.compile(r"^;\s*printer_model\s*=\s*(.+?)\s*$", re.MULTILINE)


def _commands(head):
    result = []
    for raw in head.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        result.append(line.split()[0].upper())
        if len(result) >= MAX_COMMANDS:
            break
    return result


def check_gcode(head, tail, profile):
    """Список предупреждений по-русски. Пустой список — файл выглядит подходящим."""
    if profile is None:
        return []
    warnings = []
    commands = _commands(head or "")
    title = profile.title

    if profile.start_macro and commands:
        start = profile.start_macro.upper()
        if start not in commands:
            foreign = [c for c in dict.fromkeys(commands) if c in KNOWN_START_MACROS and c != start]
            text = (
                f"В начале файла нет {profile.start_macro} — стартового макроса {title}. "
                "Без него принтер не припаркует оси и не прогреется, печать остановится с ошибкой."
            )
            if foreign:
                text += f" Вместо него в файле: {', '.join(foreign)} — файл нарезан под другой принтер."
            warnings.append(text)

    tools = [c for c in dict.fromkeys(commands) if TOOL_RE.match(c)]
    if tools and profile.tools <= 1:
        warnings.append(f"В файле команда смены экструдера {', '.join(tools)}: у {title} один экструдер.")

    found = MODEL_RE.findall((tail or "") + "\n" + (head or ""))
    if found and profile.model_names:
        model = found[-1].strip().strip('"')
        known = {n.lower() for n in profile.model_names}
        if model.lower() not in known:
            warnings.append(f"Файл нарезан для принтера «{model}», а это {title}.")
    return warnings


def read_head_tail(fileobj):
    """Начало и конец загруженного файла (Django UploadedFile или любой файловый объект)."""
    fileobj.seek(0)
    head = fileobj.read(HEAD_BYTES)
    try:
        fileobj.seek(0, 2)
        size = fileobj.tell()
        fileobj.seek(max(size - TAIL_BYTES, 0))
        tail = fileobj.read(TAIL_BYTES)
    except (OSError, ValueError):
        tail = b""
    fileobj.seek(0)
    return _text(head), _text(tail)


def _text(data):
    if isinstance(data, str):
        return data
    return (data or b"").decode("utf-8", errors="replace")
