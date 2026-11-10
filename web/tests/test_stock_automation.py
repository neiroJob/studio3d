from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR
from core.models import OrderItem, StockGroup, StockItem
from core.stock import correct, finish, observe, post_document, receive_output, receive_spool
from core.stock_models import PrinterStockSetup, PrintStockRun, Spool, StockDocument, StockMovement
from printers.collector import _save_job
from printers.models import Printer, PrintJob
from printers.state import view_state
from tests.test_crm import make_order
from tests.test_stock import login, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    group = StockGroup.objects.create(name="Пластик")
    material = StockItem.objects.create(group=group, name="PETG", unit="г", quantity=1000)
    spool = Spool.objects.create(
        item=material, code="КТ-001", nominal_g=1000, remaining_g=1000, purchase_price=1320
    )
    output = StockItem.objects.create(group=group, name="Корпус", unit="шт", finished_product=True)
    printer = Printer.objects.create(
        name="Принтер", profile_id="qidi_q1_pro", host="example.invalid", port=7125
    )
    config = PrinterStockSetup.objects.create(
        printer=printer, spool=spool, output_item=output, output_quantity=2
    )
    return config


def state(elapsed=0, status="printing", extruded=0, weight=100, estimate=7200):
    return {
        "status": status,
        "job": {
            "filename": "part.gcode",
            "elapsed": elapsed,
            "filament_mm": extruded,
            "filament_g_total": weight,
            "estimated_seconds": estimate,
        },
    }


def end_job(config, status="completed", seconds=1800):
    run = PrintStockRun.objects.get(printer=config.printer, state="active")
    return PrintJob.objects.create(
        printer=config.printer,
        external_id="one",
        filename="part.gcode",
        status=status,
        started_at=run.started_at,
        ended_at=timezone.now(),
        print_duration=seconds,
        total_duration=seconds + 900,
    )


def test_success_reserves_then_consumes_once_and_receives_on_removal(setup):
    run = observe(setup.printer_id, state())
    assert setup.spool.item.reserved_grams() == 100
    job = end_job(setup)
    finish(job)
    finish(job)
    setup.spool.refresh_from_db()
    setup.output_item.refresh_from_db()
    assert setup.spool.remaining_g == 900
    assert setup.spool.item.reserved_grams() == 0
    assert setup.output_item.quantity == 0
    run.refresh_from_db()
    assert run.material_cost == 132
    assert StockMovement.objects.filter(kind="print").count() == 1
    receive_output(job, None)
    receive_output(job, None)
    setup.output_item.refresh_from_db()
    assert setup.output_item.quantity == 2


@pytest.mark.parametrize("status", ["cancelled", "error", "interrupted", "klippy_shutdown"])
def test_failure_uses_only_active_print_seconds(setup, status):
    # 10 минут подготовки, 30 минут подачи, 10 минут паузы, ещё 30 минут печати.
    observe(setup.printer_id, state(0))
    observe(setup.printer_id, state(600, extruded=10))
    observe(setup.printer_id, state(2400, extruded=20))
    observe(setup.printer_id, state(2400, status="paused", extruded=20))
    observe(setup.printer_id, state(3000, status="paused", extruded=20))
    observe(setup.printer_id, state(3000, extruded=20))
    observe(setup.printer_id, state(4800, extruded=30))
    finish(end_job(setup, status, 4800))
    setup.spool.refresh_from_db()
    assert setup.spool.remaining_g == 950  # 100 г × 3600 / 7200, не 4800 / 7200


def test_correction_is_delta_with_reason_and_preserves_history(setup):
    observe(setup.printer_id, state())
    job = end_job(setup)
    run = finish(job)
    correct(run.pk, 75, "Контрольное взвешивание", None)
    finish(job)
    setup.spool.refresh_from_db()
    assert setup.spool.remaining_g == 925
    assert list(StockMovement.objects.order_by("pk").values_list("quantity", flat=True)) == [-100, 25]


@pytest.mark.parametrize("weight,estimate,status", [(None, 7200, "completed"), (100, None, "cancelled")])
def test_missing_slicer_data_requests_review_instead_of_inventing_weight(setup, weight, estimate, status):
    observe(setup.printer_id, state(weight=weight, estimate=estimate))
    run = finish(end_job(setup, status))
    assert run.state == "pending" and run.issue
    assert not StockMovement.objects.exists()
    correct(run.pk, 42, "Измерено вручную", None)
    setup.spool.refresh_from_db()
    assert setup.spool.remaining_g == 958


def test_failed_print_does_not_receive_defective_product(setup):
    observe(setup.printer_id, state())
    job = end_job(setup)
    finish(job)
    job.quality = "defect"
    receive_output(job, None)
    setup.output_item.refresh_from_db()
    assert setup.output_item.quantity == 0


def test_uncertain_ending_leaves_stock_for_review_even_when_printer_ready(setup):
    observe(setup.printer_id, state(60, extruded=10))
    run = finish(end_job(setup, "interrupted", 60), timing_uncertain=True)
    assert run.state == "pending" and not StockMovement.objects.exists()
    assert view_state(setup.printer, {"status": "ready"})["stock_warning"] == run.issue


def test_legacy_stock_is_not_received_twice_and_kg_is_normalized(setup):
    item = setup.spool.item
    item.unit, item.quantity = "кг", 1
    item.save()
    # Убираем тестовую катушку: распределяем старый килограмм на 750 + 250 г.
    setup.spool = None
    setup.save()
    Spool.objects.all().delete()
    s = Spool(item=item, code="old", nominal_g=750, remaining_g=750, tare_g=180)
    receive_spool(s, None, opening=True)
    item.refresh_from_db()
    assert item.unit == "г" and item.quantity == 1000
    with pytest.raises(ValidationError):
        receive_spool(Spool(item=item, code="too-much", nominal_g=750, remaining_g=750), None, opening=True)
    assert Spool.objects.count() == 1


def test_order_reservation_and_legacy_counter_do_not_double_consume(client, setup):
    user = make_user("operator", ROLE_OPERATOR)
    client.force_login(user)
    item = OrderItem.objects.create(
        order=make_order(), name="Корпус", quantity=2, plastic=setup.spool.item, unit_weight_g=50
    )
    setup.order_item = item
    setup.save()
    observe(setup.printer_id, state())
    assert setup.spool.item.reserved_grams() == 100
    job = end_job(setup)
    finish(job)
    receive_output(job, user)
    client.post(reverse("item_printed", args=[item.pk]), {"printed": 2})
    setup.spool.item.refresh_from_db()
    assert setup.spool.item.quantity == 900
    assert setup.spool.item.reserved_grams() == 0


def test_adjustment_requires_admin_and_new_pages_render(client, setup):
    observe(setup.printer_id, state())
    run = finish(end_job(setup))
    make_user("operator", ROLE_OPERATOR)
    login(client, "operator")
    assert (
        client.post(reverse("stock_correction", args=[run.pk]), {"weight": 1, "reason": "test"}).status_code
        == 403
    )
    for tab in ["balances", "spools", "documents", "prints", "reorder", "groups"]:
        assert client.get(reverse("stock"), {"tab": tab}).status_code == 200
    assert (
        client.post(
            reverse("stock_operation"),
            {"kind": "income", "item": setup.output_item_id, "quantity": 99, "reason": "forbidden"},
        ).status_code
        == 200
    )
    assert not StockMovement.objects.filter(reason="forbidden").exists()
    client.logout()
    make_user("admin", ROLE_ADMIN)
    login(client, "admin")
    assert (
        client.post(
            reverse("stock_correction", args=[run.pk]), {"weight": 60, "reason": "Точный вес"}
        ).status_code
        == 302
    )


def test_collector_end_to_end_is_idempotent(setup):
    run = observe(setup.printer_id, state())
    payload = {
        "external_id": "one",
        "filename": "part.gcode",
        "status": "completed",
        "start_time": run.started_at.timestamp(),
        "end_time": timezone.now().timestamp(),
        "print_duration": 7200,
        "total_duration": 8000,
        "filament_mm": 1000,
        "filament_g": 100,
    }
    _save_job(setup.printer_id, payload, live=True)
    _save_job(setup.printer_id, payload, live=True)
    assert StockMovement.objects.filter(kind="print").count() == 1
    assert PrintJob.objects.count() == 1


def test_purchase_url_and_price_persist(client, setup):
    make_user("manager", ROLE_MANAGER)
    login(client, "manager")
    item = setup.output_item
    assert (
        client.post(
            reverse("stock_item_edit", args=[item.pk]),
            {
                "group": item.group_id,
                "name": item.name,
                "unit": "шт",
                "quantity": "0",
                "purchase_price": "350",
                "purchase_quantity": "1",
                "supplier": "Поставщик",
                "product_url": "https://example.com/item",
            },
        ).status_code
        == 302
    )
    item.refresh_from_db()
    assert item.purchase_price == Decimal("350")
    assert item.product_url == "https://example.com/item"


def test_draft_post_revalidates_and_is_idempotent(setup):
    user = make_user("manager", ROLE_MANAGER)
    doc = StockDocument.objects.create(
        item=setup.output_item, kind="income", quantity=3, reason="Начальный приход", created_by=user
    )
    assert not StockMovement.objects.exists()
    post_document(doc.pk, user)
    post_document(doc.pk, user)
    setup.output_item.refresh_from_db()
    assert setup.output_item.quantity == 3
    assert StockMovement.objects.count() == 1
    doc = StockDocument.objects.create(
        item=setup.output_item, kind="writeoff", quantity=4, reason="Больше остатка", created_by=user
    )
    with pytest.raises(ValidationError):
        post_document(doc.pk, user)
    doc.refresh_from_db()
    assert doc.movement_id is None


def test_missing_time_after_connection_gap_is_not_silent_zero(setup):
    run = observe(setup.printer_id, state(10, extruded=10))
    run.last_observed_at = timezone.now() - timezone.timedelta(minutes=3)
    run.save()
    observe(setup.printer_id, state(200, extruded=20))
    run = finish(end_job(setup, "cancelled", 200))
    assert run.state == "pending"
    assert not StockMovement.objects.exists()


def test_consumption_is_capped_at_slicer_weight(setup):
    observe(setup.printer_id, state(0, extruded=1, estimate=100))
    observe(setup.printer_id, state(200, extruded=20, estimate=100))
    run = finish(end_job(setup, "error", 200))
    assert run.consumed_g == 100


def test_repeated_terminal_after_removal_does_not_restore_waiting(setup):
    run = observe(setup.printer_id, state())
    payload = {
        "external_id": "one",
        "filename": "part.gcode",
        "status": "completed",
        "start_time": run.started_at.timestamp(),
        "end_time": timezone.now().timestamp(),
        "print_duration": 0,
        "total_duration": 10,
        "filament_mm": 1000,
        "filament_g": 100,
    }
    _save_job(setup.printer_id, payload, True)
    job = PrintJob.objects.get(external_id="one")
    receive_output(job, None)
    setup.printer.awaiting_removal = False
    setup.printer.save()
    _save_job(setup.printer_id, payload, True)
    setup.printer.refresh_from_db()
    assert not setup.printer.awaiting_removal
    observe(setup.printer_id, state())
    assert PrintStockRun.objects.count() == 1


def test_shortage_keeps_explicit_review_without_clamping(setup):
    observe(setup.printer_id, state(weight=1500))
    run = finish(end_job(setup))
    assert run.state == "pending" and run.issue
    setup.spool.refresh_from_db()
    assert setup.spool.remaining_g == 1000


def test_failed_order_still_reserves_material_for_retry(setup):
    item = OrderItem.objects.create(
        order=make_order(), name="Корпус", quantity=2, plastic=setup.spool.item, unit_weight_g=50
    )
    setup.order_item = item
    setup.save()
    observe(setup.printer_id, state(0, extruded=1))
    observe(setup.printer_id, state(1800, extruded=10))
    finish(end_job(setup, "cancelled", 1800))
    assert setup.spool.item.reserved_grams() == 100  # материал по-прежнему нужен для заказа


def test_initial_and_paused_time_are_not_counted_as_print(setup):
    observe(setup.printer_id, state(0))
    observe(setup.printer_id, state(600))
    observe(setup.printer_id, state(600, status="paused"))
    observe(setup.printer_id, state(1200, status="paused"))
    run = finish(end_job(setup, "cancelled", 1200))
    assert run.consumed_g == 0


def test_plate_cleared_view_receives_output_once(client, setup):
    user = make_user("operator", ROLE_OPERATOR)
    client.force_login(user)
    observe(setup.printer_id, state())
    finish(end_job(setup))
    setup.printer.awaiting_removal = True
    setup.printer.save()
    url = reverse("printer_cleared", args=[setup.printer_id])
    assert client.post(url, {"quality": "ok"}).status_code == 302
    assert client.post(url, {"quality": "ok"}).status_code == 302
    setup.output_item.refresh_from_db()
    assert setup.output_item.quantity == 2
