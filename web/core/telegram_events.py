"""Транзакционная очередь событий. Telegram не вызывается из коллектора."""

import hashlib
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from printers.drivers import base
from printers.models import Printer
from printers.state import read_state, view_state

from .telegram_access import can_receive
from .telegram_models import (
    TelegramDelivery,
    TelegramEvent,
    TelegramPrinterState,
    TelegramRecipient,
    TelegramSettings,
)

ACTIVE = (base.PRINTING, base.PAUSED)


def enqueue(config, printer, kind, title, snapshot, key, priority=10):
    if not config.enabled:
        return
    event, created = TelegramEvent.objects.get_or_create(
        key=hashlib.sha256(key.encode()).hexdigest(),
        defaults={
            "printer": printer,
            "kind": kind,
            "title": title,
            "payload": snapshot,
            "priority": priority,
        },
    )
    if not created:
        return
    recipients = TelegramRecipient.objects.filter(enabled=True, subscribed=True, chat_id__isnull=False)
    if config.destination != "both":
        recipients = recipients.filter(kind=config.destination)
    for recipient in recipients.select_related("user").prefetch_related("printers"):
        if not can_receive(recipient):
            continue
        selected = list(recipient.printers.all())
        if selected and printer.pk not in [item.pk for item in selected]:
            continue
        TelegramDelivery.objects.get_or_create(
            event=event,
            recipient=recipient,
            defaults={"next_attempt_at": timezone.now()},
        )


@transaction.atomic
def record_state(printer_id, raw):
    config = TelegramSettings.load()
    printer = Printer.objects.get(pk=printer_id)
    snapshot = view_state(printer, raw)
    row, _ = TelegramPrinterState.objects.select_for_update().get_or_create(printer=printer)
    previous = row.snapshot
    old_status = previous.get("status")
    status = snapshot["status"]
    now = timezone.now()
    job = snapshot.get("job") or {}
    old_job = previous.get("job") or {}

    if status in ACTIVE:
        # OFFLINE не завершает задание: переподключение не должно создавать новый старт.
        different_file = (
            old_job.get("filename") and job.get("filename") and job["filename"] != old_job["filename"]
        )
        if not row.active or different_file:
            row.active = True
            row.run_key = uuid.uuid4().hex
            row.active_since = now
            row.last_periodic = now
            if config.notify_start:
                enqueue(
                    config, printer, "start", "Начало печати", snapshot, f"start:{printer.pk}:{row.run_key}"
                )
        elif status != old_status and old_status in ACTIVE and config.notify_pause:
            title = "Печать на паузе" if status == base.PAUSED else "Печать продолжена"
            enqueue(config, printer, "pause", title, snapshot, uuid.uuid4().hex, 3)
    elif status not in (base.OFFLINE, base.SELF_TEST):
        row.active = False

    faults = snapshot.get("faults") or []
    new_faults = [item for item in faults if item not in (previous.get("faults") or [])]
    if new_faults and config.notify_errors:
        enqueue(config, printer, "fault", "Требует внимания", snapshot, uuid.uuid4().hex, 0)

    if status == base.OFFLINE:
        if row.offline_since is None:
            row.offline_since = now
        if (
            config.enabled
            and not row.offline_notified
            and config.notify_connection
            and now - row.offline_since >= timedelta(minutes=config.offline_minutes)
        ):
            enqueue(config, printer, "offline", "Нет связи с принтером", snapshot, uuid.uuid4().hex, 5)
            row.offline_notified = True
        # Сохраняем имя активного файла на время обрыва.
        if row.active and old_job and not job:
            snapshot["job"] = old_job
    else:
        if row.offline_notified and config.notify_connection:
            enqueue(config, printer, "online", "Связь восстановлена", snapshot, uuid.uuid4().hex, 5)
        row.offline_since = None
        row.offline_notified = False

    if status == base.PRINTING and config.interval_minutes and row.last_periodic:
        if now - row.last_periodic >= timedelta(minutes=config.interval_minutes):
            enqueue(config, printer, "periodic", "Ход печати", snapshot, uuid.uuid4().hex, 20)
            row.last_periodic = now
    if status == base.FINISHED:
        if row.last_reminder is None:
            row.last_reminder = now
        elif config.reminder_minutes and now - row.last_reminder >= timedelta(
            minutes=config.reminder_minutes
        ):
            enqueue(config, printer, "reminder", "Изделие ждёт снятия", snapshot, uuid.uuid4().hex, 15)
            row.last_reminder = now
    else:
        row.last_reminder = None
    row.snapshot = snapshot
    row.save()


@transaction.atomic
def record_finished(printer, job, live):
    config = TelegramSettings.load()
    row, _ = TelegramPrinterState.objects.select_for_update().get_or_create(printer=printer)
    end = float(job.get("end_time") or 0)
    observed = row.active_since.timestamp() if row.active_since else 0
    # READY may arrive before history sync after reconnect. The observed start
    # still bounds catch-up, even if the current snapshot is already idle.
    caught_up = observed and end >= observed
    if not live and not caught_up:
        return  # Не отправлять старую историю при первом подключении.
    completed = job["status"] == "completed"
    snapshot = {
        **row.snapshot,
        "status": base.FINISHED if completed else base.READY,
        "job": {
            "filename": job["filename"],
            "elapsed": job.get("print_duration"),
            "progress": 1 if completed else (row.snapshot.get("job") or {}).get("progress", 0),
            "filament_g_total": job.get("filament_g"),
            "remaining": 0,
        },
        "result": job["status"],
        "updated": end,
    }
    if config.notify_finish:
        title = "Печать завершена — снимите изделие" if completed else "Печать остановлена / прервана"
        enqueue(
            config,
            printer,
            "finish",
            title,
            snapshot,
            f"finish:{printer.pk}:{job['external_id']}:{job['status']}",
            1,
        )
    if not row.active or caught_up:
        row.active = False
        row.snapshot = snapshot
        row.last_reminder = timezone.now() if completed else None
        row.save()


def refresh_notifications():
    """Учитывает истечение кэша, таймеры и снятие изделия даже без новых пакетов принтера."""
    for printer in Printer.objects.filter(enabled=True).only("pk"):
        record_state(printer.pk, read_state(printer.pk))
