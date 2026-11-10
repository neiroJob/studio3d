"""Транзакционный журнал склада. Повторные события не создают повторных списаний."""

import math
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import OrderItem, StockItem
from .stock_models import PrinterStockSetup, PrintStockRun, Spool, StockDocument, StockMovement

ZERO = Decimal("0")


@transaction.atomic
def post_document(document_id, user):
    doc = StockDocument.objects.select_for_update().get(pk=document_id)
    if doc.movement_id:
        return doc.movement
    item = StockItem.objects.select_for_update().get(pk=doc.item_id)
    if item.is_archived:
        raise ValidationError("Позиция в архиве.")
    if doc.unit and doc.unit != item.unit:
        raise ValidationError("Единица позиции изменилась после создания черновика. Создайте новый документ.")
    spool = Spool.objects.select_for_update().get(pk=doc.spool_id) if doc.spool_id else None
    if spool and (spool.item_id != item.pk or not item.grams_per_unit):
        raise ValidationError("Катушка не соответствует весовой позиции.")
    quantity = doc.quantity / item.grams_per_unit if spool else doc.quantity
    if doc.kind == "count":
        quantity -= spool.remaining_g / item.grams_per_unit if spool else item.quantity
    elif doc.kind == "writeoff":
        quantity = -quantity
    if quantity < 0 and item.grams_per_unit and -quantity * item.grams_per_unit > item.available_grams():
        raise ValidationError("Операция затронет резерв заказа или печати. Сначала разберите резерв.")
    doc.movement = move(item.pk, quantity, doc.kind, doc.reason, user, doc.spool_id)
    doc.save(update_fields=["movement"])
    return doc.movement


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def grams(value):
    value = number(value)
    if value is not None and value > 99999999.99:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01")) if value is not None else None


@transaction.atomic
def move(item_id, quantity, kind, reason, user=None, spool_id=None, run=None):
    item = StockItem.objects.select_for_update().get(pk=item_id)
    quantity = Decimal(quantity).quantize(Decimal("0.01"))
    if not reason.strip():
        raise ValidationError("Укажите основание операции.")
    if item.quantity + quantity < 0:
        raise ValidationError("Недостаточно остатка. Уточните вес, приход или расход.")
    spool = Spool.objects.select_for_update().get(pk=spool_id) if spool_id else None
    if item.spools.exists() and not spool:
        raise ValidationError("Выберите конкретную катушку для изменения веса.")
    if spool:
        if spool.item_id != item.pk or not item.grams_per_unit:
            raise ValidationError("Катушка не соответствует весовой позиции.")
        weight = quantity * item.grams_per_unit
        if run is None and spool.print_runs.filter(state="active").exists():
            raise ValidationError("Катушка участвует в печати: дождитесь завершения.")
        if not ZERO <= spool.remaining_g + weight <= spool.nominal_g:
            raise ValidationError("Остаток катушки должен быть между 0 и номинальным весом.")
        spool.remaining_g += weight
        spool.save(update_fields=["remaining_g"])
    item.quantity += quantity
    item.save(update_fields=["quantity"])
    return StockMovement.objects.create(
        item=item,
        spool=spool,
        quantity=quantity,
        unit=item.unit,
        kind=kind,
        reason=reason[:255],
        created_by=user,
        run=run,
    )


@transaction.atomic
def receive_spool(spool, user, opening=False):
    """Привязка старого остатка не создаёт второй приход."""
    item = StockItem.objects.select_for_update().get(pk=spool.item_id)
    if not item.grams_per_unit or item.is_archived:
        raise ValidationError("Катушке нужна активная позиция в граммах или килограммах.")
    if item.grams_per_unit == 1000:
        # Катушки учитываем точно до 0,01 г; иначе Decimal(кг,2) округлит 1 г до нуля.
        item.quantity *= 1000
        item.purchase_quantity *= 1000
        if item.min_quantity is not None:
            item.min_quantity *= 1000
        item.unit = "г"
        item.save(update_fields=["quantity", "unit", "purchase_quantity", "min_quantity"])
    if spool.remaining_g > spool.nominal_g:
        raise ValidationError("Вес пластика больше номинала катушки.")
    represented = sum(item.spools.values_list("remaining_g", flat=True), ZERO)
    if opening and represented + spool.remaining_g > item.grams:
        raise ValidationError(
            "Вес катушек превышает старый остаток. Сначала уточните единицы и остаток позиции."
        )
    spool.save()
    if not opening:
        # Остаток катушки уже задан, поэтому приход меняет только общий итог.
        delta = (spool.remaining_g / item.grams_per_unit).quantize(Decimal("0.01"))
        item.quantity += delta
        item.save(update_fields=["quantity"])
        StockMovement.objects.create(
            item=item,
            spool=spool,
            quantity=delta,
            unit=item.unit,
            kind="income",
            reason=f"Приход катушки {spool.code}",
            created_by=user,
        )
    else:
        StockMovement.objects.create(
            item=item,
            spool=spool,
            quantity=0,
            unit=item.unit,
            kind="count",
            reason=f"Катушка {spool.code}: распределение существующего остатка",
            created_by=user,
        )
    return spool


@transaction.atomic
def observe(printer_id, state):
    """Снимок установки и резерв при начале, в том числе при запуске с панели принтера."""
    from printers.models import Printer

    Printer.objects.select_for_update().get(pk=printer_id)
    job = state.get("job") or {}
    if state.get("status") not in ("printing", "paused") or not job.get("filename"):
        return
    run = PrintStockRun.objects.select_for_update().filter(printer_id=printer_id, state="active").first()
    elapsed_now = number(job.get("elapsed"))
    reset = (
        run
        and run.last_elapsed is not None
        and elapsed_now is not None
        and elapsed_now + 2 < run.last_elapsed
    )
    if run and (run.filename != job["filename"] or reset):
        run.state, run.issue = (
            "pending",
            "Пришло другое задание без достоверного завершения предыдущего. Проверьте расход.",
        )
        run.save(update_fields=["state", "issue"])
        run = None
    if not run:
        recent = PrintStockRun.objects.filter(
            printer_id=printer_id,
            filename=job["filename"],
            job__ended_at__gte=timezone.now() - timezone.timedelta(seconds=10),
        ).first()
        if recent and elapsed_now is not None and recent.last_elapsed is not None:
            if abs(elapsed_now - recent.last_elapsed) <= 2:
                return  # запоздавший кадр уже завершённого задания
        setup = PrinterStockSetup.objects.select_related("spool__item").filter(printer_id=printer_id).first()
        if not setup or not setup.spool_id:
            return
        spool = setup.spool
        # Нельзя запускать учёт на катушке, которую оператор отправил в архив.
        if spool.item.is_archived:
            return
        run = PrintStockRun.objects.create(
            printer_id=printer_id,
            spool=spool,
            filename=job["filename"],
            started_at=timezone.now(),
            order_item=setup.order_item,
            output_item=setup.output_item,
            output_quantity=setup.output_quantity,
            price_per_g=spool.purchase_price / spool.nominal_g,
        )
    estimated_g = grams(job.get("filament_g_total"))
    estimated_seconds = number(job.get("estimated_seconds"))
    if run.estimated_g is None and estimated_g is not None:
        run.estimated_g = estimated_g
    if run.estimated_seconds is None and estimated_seconds:
        run.estimated_seconds = estimated_seconds
    elapsed = number(job.get("elapsed"))
    # print_duration — счётчик печати драйвера. Паузы, подготовка/нагрев до
    # первой подачи пластика не учитываются. Не приписываем время разрыва связи.
    printing = state.get("status") == "printing" and float(job.get("filament_mm") or 0) > 0
    if run.last_observed_at and (timezone.now() - run.last_observed_at).total_seconds() > 60:
        run.timing_complete = False
    if elapsed is not None and run.last_elapsed is not None and run.last_printing and printing:
        if run.last_observed_at and (timezone.now() - run.last_observed_at).total_seconds() <= 60:
            run.active_seconds += max(0, elapsed - run.last_elapsed)
    run.last_elapsed, run.last_printing = elapsed, printing
    run.last_observed_at = timezone.now()
    run.issue = (
        ""
        if run.estimated_g is not None
        else "В G-code нет веса слайсера: автоматический расход не определён."
    )
    if run.estimated_g is not None and run.estimated_g > run.spool.remaining_g:
        run.issue = "Расчётный расход больше остатка катушки. Проверьте подачу и вес."
    run.save()
    return run


@transaction.atomic
def finish(job, *, timing_uncertain=False):
    """Связываем только наблюдавшуюся печать, а не старую историю принтера."""
    run = PrintStockRun.objects.select_for_update().filter(job=job).first()
    if not run:
        run = (
            PrintStockRun.objects.select_for_update()
            .filter(
                printer=job.printer,
                state="active",
                filename=job.filename,
                started_at__lte=job.ended_at,
            )
            .first()
        )
        if not run or job.started_at < run.started_at - timezone.timedelta(minutes=2):
            # При подключении посреди печати started_at раньше начала наблюдения.
            # Нельзя выдавать накопленное неполное время за достоверный расход.
            if not run:
                return
            run.issue = "Учёт подключён посреди печати: время до подключения не подтверждено."
    if run.corrected or (run.state == "settled" and run.settled_status == job.status):
        return
    if timing_uncertain or (run.last_elapsed is not None and job.print_duration - run.last_elapsed > 60):
        run.timing_complete = False
    run.job = job
    run.state = "pending"
    weight = run.estimated_g
    if weight is None:
        run.issue = "В G-code нет веса слайсера. Укажите расход в админке склада."
    elif job.status == "completed":
        _consume(run, weight)
    elif not run.estimated_seconds:
        run.issue = "В G-code нет времени слайсера. Укажите расход в админке склада."
    elif not run.timing_complete:
        run.issue = "Есть разрыв наблюдения времени печати. Укажите уточнённый расход."
    elif "посреди" not in run.issue:
        if run.last_printing and run.last_elapsed is not None:
            run.active_seconds += max(0, min(job.print_duration - run.last_elapsed, 60))
        amount = weight * Decimal(str(min(run.active_seconds / run.estimated_seconds, 1)))
        _consume(run, amount.quantize(Decimal("0.01")))
    run.save()
    return run


def _consume(run, amount, user=None, reason=None):
    delta = amount - run.consumed_g
    item = run.spool.item
    try:
        move(
            item.pk,
            -delta / item.grams_per_unit,
            "correction" if reason else "print",
            reason or f"Печать {run.filename} · {run.spool.code}",
            user,
            run.spool_id,
            run,
        )
    except ValidationError as exc:
        run.issue = "; ".join(exc.messages)
        return False
    if run.order_item_id:
        order_item = OrderItem.objects.select_for_update().get(pk=run.order_item_id)
        order_item.stock_accounted_g += delta
        order_item.save(update_fields=["stock_accounted_g"])
    run.consumed_g, run.state, run.issue = amount, "settled", ""
    run.settled_status = run.job.status if run.job_id else ""
    return True


@transaction.atomic
def correct(run_id, amount, reason, user):
    run = PrintStockRun.objects.select_for_update().select_related("spool__item").get(pk=run_id)
    if run.state == "active":
        raise ValidationError("Дождитесь окончания печати.")
    amount = grams(amount)
    if amount is None or not reason.strip():
        raise ValidationError("Укажите неотрицательный вес и причину корректировки.")
    if not _consume(run, amount, user, f"Коррекция {run.consumed_g} → {amount} г: {reason}"):
        raise ValidationError(run.issue)
    run.corrected = True
    run.save()


@transaction.atomic
def receive_output(job, user):
    if job.status != "completed":
        return
    run = PrintStockRun.objects.select_for_update().filter(job=job).first()
    if not run or run.removed_at:
        return
    if job.quality == "defect":
        run.removed_at = timezone.now()
        run.save(update_fields=["removed_at"])
        return
    if run.output_item_id:
        move(
            run.output_item_id, run.output_quantity, "output", f"Снято изделие: {job.filename}", user, run=run
        )
    if run.order_item_id:
        item = OrderItem.objects.select_for_update().get(pk=run.order_item_id)
        item.printed = min(item.quantity, item.printed + run.output_quantity)
        item.save(update_fields=["printed"])
    run.removed_at = timezone.now()
    run.save(update_fields=["removed_at"])
