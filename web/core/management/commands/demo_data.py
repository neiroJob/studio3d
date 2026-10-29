"""Синтетические демо-данные для скриншотов и знакомства с интерфейсом.

    python manage.py demo_data            # создать демо-принтеры и задания
    python manage.py demo_data --hold 600 # плюс держать живое состояние и снимки камер N секунд

Никаких реальных адресов, клиентов и файлов: имена и цифры придуманы.
Не запускайте на рабочей установке — данные смешаются с настоящими.
"""

import io
import math
import time
from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from printers.drivers import base
from printers.models import Printer, PrintJob
from printers.state import write_state

DEMO = [
    # имя, зона, статус, прогресс, файл, слой, всего слоёв, осталось (с), сопло, стол, камера
    ("Q1Pro-01", "Зал печати", base.PRINTING, 0.64, "gear_housing_PA-CF.gcode", 131, 203, 7420, 295, 80, 60),
    ("Q1Pro-02", "Зал печати", base.PRINTING, 0.18, "bracket_x12_PETG.gcode", 22, 118, 14800, 240, 80, 0),
    ("Q1Pro-03", "Зал печати", base.READY, 0, "", 0, 0, 0, 0, 0, 0),
    ("Q1Pro-04", "Зал печати", base.PAUSED, 0.41, "cable_clip_x40_PLA.gcode", 57, 140, 3900, 210, 60, 0),
    ("Q1Pro-05", "Склад", base.ERROR, 0.0, "enclosure_lid_ABS.gcode", 0, 160, None, 0, 0, 0),
    ("Q1Pro-06", "Склад", base.OFFLINE, 0, "", 0, 0, 0, 0, 0, 0),
]


def demo_state(row, now):
    name, zone, status, progress, filename, layer, total, remaining, noz, bed, ch = row
    if status == base.OFFLINE:
        return base.offline_state(updated=now)
    temps = {
        "nozzle": [noz - 0.4 if noz else 26.5, noz],
        "bed": [bed - 0.3 if bed else 25.8, bed],
        "chamber": [ch - 1.2 if ch else 27.1, ch],
    }
    job = None
    message = ""
    console = []
    if status in (base.PRINTING, base.PAUSED, base.ERROR):
        elapsed = int((remaining or 600) * progress / max(1 - progress, 0.01))
        job = {
            "filename": filename,
            "progress": progress,
            "layer": layer,
            "total_layers": total,
            "elapsed": elapsed,
            "remaining": remaining,
            "filament_mm": 12000 * progress,
            "filament_g_total": 96.0,
        }
    if status == base.ERROR:
        message = (
            "Оси не в исходном положении: G-code начал движение без парковки (G28). "
            "Принтер не знает команд из файла: START_PRINT — скорее всего, файл нарезан под другой принтер."
        )
        console = [
            "File opened:enclosure_lid_ABS.gcode Size:18342211",
            '// Unknown command:"START_PRINT"',
            "!! Must home axis first: 0.000 0.000 3.000 [0.000]",
        ]
    return {
        "status": status,
        "message": message,
        "temps": temps,
        "job": job,
        "light": status != base.READY,
        "console": console,
        "updated": now,
    }


def demo_snapshot(seed, t):
    """Синтетический «кадр камеры»: стол, деталь, сопло. Без реальных изображений."""
    from PIL import Image, ImageDraw, ImageFilter

    w, h = 640, 480
    img = Image.new("RGB", (w, h), (24, 28, 34))
    d = ImageDraw.Draw(img)
    # стенки и стол в перспективе
    d.polygon([(40, 470), (600, 470), (520, 300), (120, 300)], fill=(46, 52, 60))
    d.polygon([(70, 460), (570, 460), (505, 315), (135, 315)], fill=(58, 64, 72))
    for i in range(9):
        x0 = 135 + i * 46
        d.line([(x0, 315), (70 + i * 62.5, 460)], fill=(66, 72, 80), width=1)
    # деталь, растущая по слоям
    height = 30 + int(90 * ((seed * 0.37 + t * 0.002) % 1.0))
    colors = [(156, 116, 38), (70, 120, 180), (190, 70, 60), (60, 150, 120)]
    c = colors[seed % len(colors)]
    d.rectangle([250, 400 - height, 390, 400], fill=c)
    d.polygon(
        [(250, 400 - height), (390, 400 - height), (410, 385 - height), (270, 385 - height)],
        fill=tuple(min(255, v + 35) for v in c),
    )
    d.polygon(
        [(390, 400), (410, 385), (410, 385 - height), (390, 400 - height)],
        fill=tuple(max(0, v - 30) for v in c),
    )
    # портал и сопло
    nx = 320 + int(60 * math.sin(t / 3 + seed))
    d.rectangle([0, 120, w, 140], fill=(36, 40, 46))
    d.rectangle([nx - 38, 140, nx + 38, 220], fill=(205, 205, 210))
    d.polygon([(nx - 10, 220), (nx + 10, 220), (nx + 3, 240), (nx - 3, 240)], fill=(170, 140, 70))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


class Command(BaseCommand):
    help = "Создать синтетические демо-принтеры (для скриншотов и знакомства с интерфейсом)."

    def add_arguments(self, parser):
        parser.add_argument("--hold", type=int, default=0, help="Держать живое состояние N секунд")

    def handle(self, *args, hold, **options):
        printers = []
        for i, row in enumerate(DEMO):
            name, zone = row[0], row[1]
            p, _ = Printer.objects.update_or_create(
                name=name,
                defaults={
                    "profile_id": "qidi_q1_pro",
                    "host": f"192.168.10.{21 + i}",
                    "port": 7125,
                    "zone": zone,
                    "power_w": 350,
                    "purchase_cost": 65000,
                    "enabled": False,
                    "print_seconds_base": int(3600 * (1200 + 310 * i)),
                    "on_seconds": int(3600 * (1800 + 400 * i)),
                    "awaiting_removal": row[2] == base.READY,
                    "last_seen_at": timezone.now(),
                },
            )
            printers.append((p, row))
            if not p.jobs.exists():
                for j in range(6):
                    ended = timezone.now() - timedelta(hours=7 * j + 3)
                    PrintJob.objects.create(
                        printer=p,
                        external_id=f"DEMO{i}{j}",
                        filename=f"part_{j + 1:02d}.gcode",
                        status=PrintJob.Status.CANCELLED if j == 2 else PrintJob.Status.COMPLETED,
                        started_at=ended - timedelta(hours=4, minutes=12 * j),
                        ended_at=ended,
                        print_duration=4 * 3600 + 720 * j,
                        total_duration=4 * 3600 + 900 * j,
                        filament_mm=9000 + 800 * j,
                        filament_g=27.5 + 2.4 * j,
                    )
        self.stdout.write(f"Демо-принтеров: {len(printers)}")

        deadline = time.time() + hold
        while True:
            now = time.time()
            for p, row in printers:
                write_state(p.pk, demo_state(row, now))
                if row[2] != base.OFFLINE:
                    cache.set(f"printer:snapshot:{p.pk}", demo_snapshot(p.pk, now), timeout=60)
            if now >= deadline:
                break
            time.sleep(5)
