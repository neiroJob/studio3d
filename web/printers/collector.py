"""Коллектор: держит соединение с каждым принтером, пишет состояние в кэш и задания в БД.

Запуск: python manage.py run_collector (контейнер collector).
Обрыв связи с принтером не роняет сервис: каждое соединение переподключается само.
"""

import asyncio
import datetime as dt
import logging
import time

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.db.models import F
from django.utils import timezone

from .drivers import base, monitor_class
from .models import Printer, PrintJob
from .state import write_state

log = logging.getLogger("collector")

RELOAD_EVERY = 10  # сек — как часто перечитывать список принтеров
HEARTBEAT = 5  # сек — обновление состояния в кэше и учёт времени включения
FLUSH_ON_EVERY = 60  # сек — запись времени включения в БД


def _ts(value):
    return dt.datetime.fromtimestamp(value, tz=dt.UTC) if value else None


@sync_to_async
def load_printers():
    close_old_connections()
    return list(Printer.objects.filter(enabled=True))


@sync_to_async
def save_job(printer_id, job, live):
    """Записать завершённое задание. Возвращает True, если запись новая."""
    close_old_connections()
    printer = Printer.objects.get(pk=printer_id)
    if not job["end_time"] or job["end_time"] <= printer.history_since:
        return False  # уже учтено в базе наработки на момент добавления принтера
    status = job["status"] if job["status"] in PrintJob.Status.values else PrintJob.Status.OTHER
    existing = PrintJob.objects.filter(printer=printer, external_id=job["external_id"]).first()
    if existing and existing.status == PrintJob.Status.COMPLETED and status != PrintJob.Status.COMPLETED:
        # Запоздавшая резервная оценка не должна понизить подтверждённое завершение.
        return False
    _, created = PrintJob.objects.update_or_create(
        printer=printer,
        external_id=job["external_id"],
        defaults={
            "filename": job["filename"],
            "status": status,
            "started_at": _ts(job["start_time"]) or timezone.now(),
            "ended_at": _ts(job["end_time"]),
            "print_duration": job["print_duration"],
            "total_duration": job["total_duration"],
            "filament_mm": job["filament_mm"],
            "filament_g": job["filament_g"],
        },
    )
    if live and status == PrintJob.Status.COMPLETED:
        Printer.objects.filter(pk=printer_id).update(awaiting_removal=True)
    return created


@sync_to_async
def last_job_end(printer_id, default):
    close_old_connections()
    job = PrintJob.objects.filter(printer_id=printer_id).exclude(ended_at=None).order_by("-ended_at").first()
    return max(job.ended_at.timestamp(), default) if job else default


@sync_to_async
def add_on_seconds(printer_id, seconds):
    close_old_connections()
    Printer.objects.filter(pk=printer_id).update(
        on_seconds=F("on_seconds") + int(seconds), last_seen_at=timezone.now()
    )


@sync_to_async
def clear_awaiting(printer_id):
    close_old_connections()
    Printer.objects.filter(pk=printer_id, awaiting_removal=True).update(awaiting_removal=False)


#  Модели без истории заданий на самом принтере (см. drivers/creality_native.py) — если соединение
# обрывается прямо во время печати, монитор пересоздаётся с нуля и теряет отслеживание задания
# в памяти. Иначе такое задание не попадёт ни в историю, ни в «ждёт снятия» никогда.
NO_SERVER_HISTORY_DRIVERS = {"creality_native"}


class PrinterWorker:
    def __init__(self, printer, session):
        self.printer = printer
        self.session = session
        self.signature = self.make_signature(printer)
        self.connected = False
        self.last_state = base.offline_state()
        self.pending_on = 0.0
        self.pending_job_check = None  # состояние перед обрывом связи, если задание было активно
        self.task = None

    @staticmethod
    def make_signature(p):
        return (p.host, p.port, p.api_key_encrypted, p.profile_id)

    def start(self):
        self.task = asyncio.create_task(self.loop(), name=f"printer-{self.printer.pk}")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await sync_to_async(write_state)(self.printer.pk, base.offline_state("Опрос выключен", time.time()))

    async def on_state(self, state):
        self.connected = True
        if state["status"] == base.PRINTING:
            await clear_awaiting(self.printer.pk)
        previous_job = self.last_state.get("job") or {}
        if (
            self.printer.profile_id == "creality_k1"
            and self.last_state.get("status") in (base.PRINTING, base.PAUSED)
            and state.get("status") == base.READY
            and previous_job.get("filename")
        ):
            # K1 иногда пропускает кадр завершения. Монитор обычно сохраняет
            # задание первым; повторный upsert страхует переход состояния.
            elapsed = float(previous_job.get("elapsed") or 0)
            progress = float(previous_job.get("progress") or 0)
            started = (self.last_state.get("updated") or time.time()) - elapsed
            await self.on_job(
                {
                    "external_id": previous_job.get("external_id")
                    or f"{previous_job['filename']}@{round(started)}",
                    "filename": previous_job["filename"],
                    "status": "completed" if progress >= 0.999 else "cancelled",
                    "start_time": started,
                    "end_time": time.time(),
                    "print_duration": elapsed,
                    "total_duration": elapsed,
                    "filament_mm": previous_job.get("filament_mm") or 0.0,
                    "filament_g": previous_job.get("filament_g_total"),
                },
                live=True,
            )
        if self.pending_job_check is not None:
            pending = self.pending_job_check
            self.pending_job_check = None
            pending_job = pending.get("job") or {}
            current_job = state.get("job") or {}
            if current_job.get("filename") != pending_job.get("filename"):
                await self._finish_job_after_gap(pending)
        self.last_state = state
        await sync_to_async(write_state)(self.printer.pk, state)

    async def on_job(self, job, live):
        created = await save_job(self.printer.pk, job, live)
        if created:
            log.info("%s: задание %s — %s", self.printer.name, job["filename"], job["status"])

    async def _finish_job_after_gap(self, snapshot):
        """Соединение обрывалось во время печати, а при восстановлении задания уже нет: у этой
        модели нет истории на самом принтере (NO_SERVER_HISTORY_DRIVERS), поэтому факт завершения
        иначе был бы потерян навсегда. Закрываем задание по последним показаниям перед обрывом —
        точный момент окончания неизвестен, только то, что оно было активно и перестало быть."""
        job = snapshot.get("job") or {}
        if not job.get("filename"):
            return
        elapsed = job.get("elapsed") or 0.0
        progress = job.get("progress") or 0.0
        start_time = (snapshot.get("updated") or time.time()) - elapsed
        await self.on_job(
            {
                "external_id": f"gap:{job['filename']}@{round(start_time)}",
                "filename": job["filename"],
                "status": "completed" if progress >= 0.999 else "interrupted",
                "start_time": start_time,
                "end_time": time.time(),
                "print_duration": elapsed,
                "total_duration": elapsed,
                "filament_mm": job.get("filament_mm") or 0.0,
                "filament_g": None,
            },
            live=True,
        )

    async def heartbeat(self):
        last_flush = time.monotonic()
        while True:
            await asyncio.sleep(HEARTBEAT)
            if self.connected:
                self.pending_on += HEARTBEAT
                state = dict(self.last_state, updated=time.time())
                await sync_to_async(write_state)(self.printer.pk, state)
            if time.monotonic() - last_flush >= FLUSH_ON_EVERY and self.pending_on:
                await add_on_seconds(self.printer.pk, self.pending_on)
                self.pending_on = 0
                last_flush = time.monotonic()

    async def loop(self):
        profile = self.printer.profile
        if profile is None:
            await sync_to_async(write_state)(self.printer.pk, base.offline_state("Профиль модели не найден"))
            return
        beat = asyncio.create_task(self.heartbeat())
        delay = 5
        try:
            while True:
                monitor = monitor_class(profile)(
                    self.printer.host, self.printer.port, self.printer.api_key, profile, session=self.session
                )
                since = await last_job_end(self.printer.pk, self.printer.history_since)
                try:
                    await monitor.run(self.on_state, self.on_job, history_since=since)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — любая ошибка связи ведёт к переподключению
                    log.info("%s: нет связи (%s), повтор через %s с", self.printer.name, e, delay)
                    self.connected = False
                    if (
                        profile.driver in NO_SERVER_HISTORY_DRIVERS
                        and self.last_state.get("job")
                        and self.last_state.get("status") in (base.PRINTING, base.PAUSED)
                    ):
                        self.pending_job_check = self.last_state
                    self.last_state = base.offline_state(updated=time.time())
                    await sync_to_async(write_state)(self.printer.pk, self.last_state)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                delay = 5
        finally:
            beat.cancel()
            if self.pending_on:
                await add_on_seconds(self.printer.pk, self.pending_on)


async def run_forever():
    import aiohttp

    workers = {}
    async with aiohttp.ClientSession() as session:
        log.info("Коллектор запущен")
        while True:
            try:
                printers = {p.pk: p for p in await load_printers()}
            except Exception as e:  # noqa: BLE001 — БД может быть недоступна при старте
                log.warning("Не удалось прочитать список принтеров: %s", e)
                await asyncio.sleep(RELOAD_EVERY)
                continue
            for pk in list(workers):
                p = printers.get(pk)
                if p is None or PrinterWorker.make_signature(p) != workers[pk].signature:
                    await workers.pop(pk).stop()
            for pk, p in printers.items():
                if pk not in workers:
                    workers[pk] = PrinterWorker(p, session)
                    workers[pk].start()
                    log.info("Подключаю %s (%s:%s)", p.name, p.host, p.port)
                elif workers[pk].task.done():
                    workers.pop(pk)  # пересоздадим на следующем круге
            await asyncio.sleep(RELOAD_EVERY)
