from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.roles import ROLE_MANAGER, ROLE_OPERATOR
from core.models import StockGroup, StockItem
from core.stock_models import PrinterStockSetup, Spool, StockDocument, StockMovement
from printers.drivers import DriverError
from printers.maintenance_models import MaintenanceAction, MaintenanceEvent

from .test_printers import make_printer, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(client):
    client.force_login(make_user("maintenance-test", ROLE_OPERATOR))


def test_service_submission_is_atomic_and_retry_safe(client, operator):
    printer = make_printer(print_seconds_base=7200)
    group = StockGroup.objects.create(name="Расходники ТО")
    nozzle = StockItem.objects.create(group=group, name="Сопло 0,6 мм", quantity=1)
    sock = StockItem.objects.create(group=group, name="Силиконовый носок", quantity=1)
    action = MaintenanceAction.objects.create(name="Замена запчасти", requires_stock=True, stock_group=group)
    url = reverse("printer_maintenance", args=[printer.pk])
    data = {
        "request_id": str(uuid4()),
        "note": "Проверена подача",
        "work-TOTAL_FORMS": 2,
        "work-INITIAL_FORMS": 0,
        "work-0-action": action.pk,
        "work-0-item": nozzle.pk,
        "work-0-quantity": 1,
        "work-1-action": action.pk,
        "work-1-item": sock.pk,
        "work-1-quantity": 2,
    }
    assert client.post(url, data).status_code == 200
    nozzle.refresh_from_db()
    assert nozzle.quantity == 1
    assert not MaintenanceEvent.objects.exists()
    assert not StockDocument.objects.exists()
    assert not StockMovement.objects.exists()

    data["work-1-quantity"] = 1
    assert client.post(url, data).status_code == 302
    assert client.post(url, data).status_code == 302
    event = MaintenanceEvent.objects.get()
    assert event.print_seconds == 7200
    assert event.operator == "maintenance-test"
    assert list(event.lines.order_by("pk").values_list("item_name", flat=True)) == [nozzle.name, sock.name]
    assert StockMovement.objects.count() == 2
    nozzle.refresh_from_db()
    sock.refresh_from_db()
    assert nozzle.quantity == sock.quantity == 0
    assert nozzle.name in client.get(url).content.decode()


def test_operator_cannot_edit_catalog(client, operator):
    assert client.post(reverse("maintenance_catalog"), {"name": "Подмена справочника"}).status_code == 403


def test_viewer_cannot_record_maintenance(client):
    client.force_login(make_user("maintenance-viewer", ROLE_MANAGER))
    printer = make_printer()
    url = reverse("printer_maintenance", args=[printer.pk])
    assert client.get(url).status_code == 200
    assert client.post(url, {}).status_code == 403


@pytest.mark.parametrize("reason", ["", "printer_failure", "other"])
def test_invalid_defect_does_not_clear_plate(client, operator, reason):
    printer = make_printer(awaiting_removal=True)
    response = client.post(
        reverse("printer_cleared", args=[printer.pk]),
        {
            "quality": "defect",
            "defect_reason": reason,
            "defect_note": " ",
        },
    )
    assert response.status_code == 302
    printer.refresh_from_db()
    assert printer.awaiting_removal


@pytest.mark.parametrize("entry", ["printer_start", "printer_upload"])
def test_launch_uses_selected_spool_and_rolls_back_on_driver_error(client, operator, monkeypatch, entry):
    printer = make_printer()
    group = StockGroup.objects.create(name="Пластик")
    material = StockItem.objects.create(group=group, name="PETG", unit="г", quantity=1000)
    spool = Spool.objects.create(item=material, code="PETG-1", nominal_g=1000, remaining_g=1000)
    driver = Mock()
    driver.start.side_effect = DriverError("Тестовый отказ")
    driver.upload.side_effect = DriverError("Тестовый отказ")
    monkeypatch.setattr("printers.views.client_for", lambda printer: driver)
    monkeypatch.setattr("printers.views._state_blocks_start", lambda printer: "")
    url = reverse(entry, args=[printer.pk])

    def launch():
        data = {
            "spool": spool.pk,
            "filename": "test.gcode",
            "confirm": "1",
            "confirm_warnings": "1",
            "start": "on",
        }
        if entry == "printer_upload":
            data["file"] = SimpleUploadedFile("test.gcode", b"G28\n")
        return client.post(url, data)

    assert launch().status_code == 302
    assert not PrinterStockSetup.objects.filter(printer=printer).exists()
    driver.start.side_effect = driver.upload.side_effect = None
    assert launch().status_code == 302
    assert PrinterStockSetup.objects.get(printer=printer).spool_id == spool.pk
