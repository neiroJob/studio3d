import asyncio
import time
from email.utils import formatdate

import aiohttp
import pytest
from asgiref.sync import async_to_sync
from django import forms
from django.urls import reverse

from accounts.roles import ROLE_OPERATOR
from core.forms import StockItemForm
from core.models import StockGroup, StockItem
from printers import collector
from printers.drivers import base
from printers.drivers.moonraker import MoonrakerMonitor, job_from_history, normalize
from printers.models import PrintJob
from printers.state import view_state, write_state

from .fake_moonraker import FakeMoonraker
from .test_printers import Q1, login, make_printer, make_user


def test_long_job_uses_end_time_and_printer_clock():
    async def scenario():
        fake = FakeMoonraker()
        now = time.time()
        skew = 2 * 365 * 86400
        fake.http_date = formatdate(now - skew, usegmt=True)
        fake.data["history"] = [
            {
                "job_id": "000083",
                "filename": "long.gcode",
                "status": "completed",
                "start_time": now - skew - 33000,
                "end_time": now - skew - 2,
                "print_duration": 32073.39,
                "total_duration": 33000,
            },
            {
                "job_id": "000082",
                "filename": "old.gcode",
                "status": "completed",
                "start_time": now - skew - 90000,
                "end_time": now - skew - 60000,
            },
        ]
        await fake.start_async()
        try:
            async with aiohttp.ClientSession() as session:
                monitor = MoonrakerMonitor("127.0.0.1", fake.port, profile=Q1, session=session)
                jobs = await monitor.history_since(now - 60)
                assert len(jobs) == 1
                job = job_from_history(jobs[0], monitor.clock_offset)
                assert abs(job["end_time"] - (now - 2)) < 2
                assert job["print_duration"] == 32073.39
                assert abs(job["clock_offset_seconds"] - skew) < 2
                states = []

                async def on_state(state):
                    states.append(state)

                await monitor._emit_state(on_state)
                assert states[0]["clock_warning"]
        finally:
            await fake.stop_async()

    asyncio.run(scenario())


def test_history_pagination_keeps_long_print_crossing_cutoff():
    async def scenario():
        fake = FakeMoonraker()
        now = time.time()
        fake.data["history"] = [
            {
                "job_id": f"{i:06X}",
                "filename": f"{i}.gcode",
                "status": "completed",
                "start_time": now - 100000 + i,
                "end_time": now - 100 + i,
            }
            for i in range(120)
        ]
        await fake.start_async()
        try:
            async with aiohttp.ClientSession() as session:
                monitor = MoonrakerMonitor("127.0.0.1", fake.port, profile=Q1, session=session)
                jobs = await monitor.history_since(now - 90)
                assert len(jobs) == 109
                assert jobs[0]["job_id"] == "00000B"
        finally:
            await fake.stop_async()

    asyncio.run(scenario())


def test_periodic_history_recovers_without_any_ws_notification(monkeypatch):
    async def scenario():
        monitor = MoonrakerMonitor("localhost", 1, profile=Q1)
        now = time.time()
        calls = []
        sleeps = 0

        async def tick(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        async def history(since):
            assert since < now
            return [{"job_id": "late", "filename": "part.gcode", "status": "completed",
                     "start_time": now - 40000, "end_time": now - 2, "print_duration": 39000}]

        async def on_job(job, live):
            calls.append((job, live))

        monkeypatch.setattr("printers.drivers.moonraker.asyncio.sleep", tick)
        monkeypatch.setattr(monitor, "history_since", history)
        with pytest.raises(asyncio.CancelledError):
            await monitor._poll_history(on_job)
        assert calls[0][0]["print_duration"] == 39000
        assert calls[0][1] is True

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_completed_then_fault_keeps_history_and_removal(client):
    printer = make_printer(history_since=time.time() - 2000)
    user = make_user("operator", ROLE_OPERATOR)
    job = job_from_history(
        {
            "job_id": "J-complete",
            "filename": "part.gcode",
            "status": "completed",
            "start_time": time.time() - 1000,
            "end_time": time.time() - 1,
            "print_duration": 900,
        }
    )
    async_to_sync(collector.save_job)(printer.pk, job, True)
    async_to_sync(collector.save_job)(printer.pk, dict(job, status="error"), True)
    printer.refresh_from_db()
    write_state(printer.pk, {"status": base.ERROR, "message": "Ошибка после печати", "temps": {}})
    login(client, user.username)
    html = client.get(reverse("printer_detail", args=[printer.pk])).content.decode()
    assert 'data-open-dialog="plate-dialog-' in html
    assert PrintJob.objects.get(printer=printer).status == "completed"
    assert printer.print_seconds == 900
    client.post(reverse("printer_cleared", args=[printer.pk]), {"quality": "ok"})
    async_to_sync(collector.save_job)(printer.pk, job, True)
    printer.refresh_from_db()
    assert not printer.awaiting_removal


@pytest.mark.django_db(transaction=True)
def test_reconnect_restores_only_latest_unassessed_completion():
    printer = make_printer()
    job = job_from_history(
        {
            "job_id": "last",
            "filename": "part.gcode",
            "status": "completed",
            "start_time": time.time() - 600,
            "end_time": time.time() - 1,
            "print_duration": 500,
        }
    )
    async_to_sync(collector.save_job)(printer.pk, job, False)
    async_to_sync(collector.restore_awaiting)(printer.pk, "other.gcode")
    printer.refresh_from_db()
    assert not printer.awaiting_removal
    async_to_sync(collector.restore_awaiting)(printer.pk, "part.gcode")
    printer.refresh_from_db()
    assert printer.awaiting_removal
    printer.jobs.update(quality="ok")
    printer.awaiting_removal = False
    printer.save()
    async_to_sync(collector.restore_awaiting)(printer.pk, "part.gcode")
    printer.refresh_from_db()
    assert not printer.awaiting_removal


@pytest.mark.django_db
def test_attention_includes_stock_clock_and_disabled_reaction(client):
    printer = make_printer()
    user = make_user("operator", ROLE_OPERATOR)
    state = {
        "status": base.PRINTING,
        "temps": {},
        "job": None,
        "clock_warning": "Проверьте часы принтера",
        "filament": {"present": True, "enabled": False},
    }
    write_state(printer.pk, state)
    login(client, user.username)
    response = client.get(reverse("dashboard"))
    assert any(it["printer"].pk == printer.pk for it in response.context["attention"])
    html = response.content.decode().split("<h2>Требует внимания</h2>")[1]
    assert "Автосписание не настроено" in html
    assert "Проверьте часы принтера" in html
    assert "Реакция на отсутствие нити отключена" in html
    assert view_state(printer, state)["status"] == base.PRINTING


def test_hall_presence_does_not_mean_runout_protection_enabled():
    state = normalize(
        {
            "print_stats": {"state": "printing"},
            "hall_filament_width_sensor": {"Diameter": 1.91, "is_active": False},
        },
        profile=Q1,
    )
    assert state["filament"] == {"present": True, "enabled": False}


@pytest.mark.django_db
def test_stock_units_dropdown_and_legacy_value():
    group = StockGroup.objects.create(name="Материалы")
    form = StockItemForm()
    assert isinstance(form.fields["unit"].widget, forms.Select)
    assert set(dict(form.fields["unit"].choices)) == {"шт", "г", "кг"}
    data = {"group": group.pk, "name": "Сопло", "unit": "5шт", "quantity": 0}
    assert not StockItemForm(data).is_valid()
    item = StockItem.objects.create(group=group, name="Старое сопло", unit="5шт", quantity=2)
    legacy = StockItemForm(dict(data, name=item.name, quantity=2), instance=item)
    assert legacy.is_valid(), legacy.errors
    legacy.save()
    item.refresh_from_db()
    assert item.unit == "5шт" and item.quantity == 2
