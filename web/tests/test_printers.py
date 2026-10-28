"""Этап 3: драйвер moonraker, профиль QIDI Q1 Pro, добавление принтера, дашборд, команды, наработка."""

import asyncio
import io
import time

import pytest
from aiohttp import web
from asgiref.sync import async_to_sync
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.urls import reverse

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR
from core.models import AuditLog
from printers import collector
from printers.drivers import base
from printers.drivers.moonraker import MoonrakerClient, MoonrakerMonitor, job_from_history, normalize
from printers.models import Printer, PrintJob
from printers.profiles import get_profile
from printers.state import read_state, view_state, write_state

from .fake_moonraker import FakeMoonraker, load_fixture

PASSWORD = "Pech-2026-studio"
Q1 = get_profile("qidi_q1_pro")


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def fake():
    server = FakeMoonraker().start_thread()
    yield server
    server.stop_thread()


def make_user(username, *roles):
    user = User.objects.create_user(username=username, password=PASSWORD, first_name=username)
    user.groups.set(Group.objects.filter(name__in=roles))
    return user


def login(client, username):
    assert client.post(reverse("login"), {"username": username, "password": PASSWORD}).status_code == 302


def make_printer(port=7125, **kw):
    defaults = {"name": "Q1Pro-01", "profile_id": "qidi_q1_pro", "host": "127.0.0.1", "port": port}
    defaults.update(kw)
    return Printer.objects.create(**defaults)


# --- профиль ----------------------------------------------------------------------------------


def test_q1_pro_profile():
    assert Q1.driver == "moonraker" and Q1.port == 7125 and Q1.verified
    assert Q1.objects["chamber"] == "heater_generic chamber"
    assert Q1.snapshot_url_for("10.0.0.7") == "http://10.0.0.7:8080/?action=snapshot"
    assert Q1.web_ui_for("10.0.0.7") == "http://10.0.0.7:10088"
    assert {"pause", "resume", "cancel", "start_file", "upload"} <= Q1.capabilities


# --- нормализация состояния -------------------------------------------------------------------


def test_normalize_printing():
    data = load_fixture("moonraker_q1pro_printing.json")
    s = normalize(data["status"], data["metadata"], Q1, now=100)
    assert s["status"] == base.PRINTING
    assert s["temps"] == {"nozzle": [249.8, 250.0], "bed": [99.6, 100.0], "chamber": [45.2, 50.0]}
    job = s["job"]
    assert job["progress"] == 0.35 and job["layer"] == 42 and job["total_layers"] == 120
    assert job["elapsed"] == 1800 and job["remaining"] == 3600  # estimated_time 5400 − 1800
    assert job["filament_g_total"] == 42.7


def test_normalize_states():
    st = {"webhooks": {"state": "ready"}, "print_stats": {"state": "standby"}}
    assert normalize(st, profile=Q1)["status"] == base.READY
    assert normalize(st, profile=Q1)["job"] is None
    st["print_stats"] = {"state": "paused", "filename": "a.gcode", "print_duration": 100}
    st["virtual_sdcard"] = {"progress": 0.5}
    paused = normalize(st, profile=Q1)
    assert (
        paused["status"] == base.PAUSED and paused["job"]["remaining"] == 100
    )  # без метаданных — по прогрессу
    st["print_stats"] = {"state": "complete"}
    assert normalize(st, profile=Q1)["status"] == base.READY
    shutdown = normalize(
        {"webhooks": {"state": "shutdown", "state_message": "MCU 'mcu' shutdown"}}, profile=Q1
    )
    assert shutdown["status"] == base.ERROR and "MCU" in shutdown["message"]
    assert normalize({"webhooks": {"state": "startup"}}, profile=Q1)["status"] == base.OFFLINE
    assert normalize({}, connected=False)["status"] == base.OFFLINE


def test_job_from_history_grams():
    done = job_from_history(
        {
            "job_id": 1,
            "status": "completed",
            "filament_used": 4000,
            "metadata": {"filament_weight_total": 20.0, "filament_total": 4000},
        }
    )
    assert done["filament_g"] == 20.0 and done["external_id"] == "1"
    half = job_from_history(
        {
            "job_id": 2,
            "status": "cancelled",
            "filament_used": 1000,
            "metadata": {"filament_weight_total": 20.0, "filament_total": 4000},
        }
    )
    assert half["filament_g"] == 5.0
    assert job_from_history({"job_id": 3, "status": "completed"})["filament_g"] is None


def test_view_state_awaiting_removal():
    p = Printer(name="x", profile_id="qidi_q1_pro", host="h", port=1, awaiting_removal=True)
    s = view_state(p, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    assert s["status"] == base.FINISHED and s["label"] == "Ждёт снятия"


# --- HTTP-клиент ------------------------------------------------------------------------------


def test_client_check_and_commands(fake):
    c = MoonrakerClient("127.0.0.1", fake.port, profile=Q1)
    info = c.check()
    assert info["moonraker"] == "v0.7.1-609-dirty" and info["klipper"] == "v0.10.0-530-dirty"
    assert info["klippy_state"] == "ready" and info["printer_state"] == "printing"
    assert c.history_totals() == {"print_seconds": 360000.0}
    assert [f["path"] for f in c.list_files()] == ["gear_z24.gcode", "cube.gcode"]
    c.pause()
    c.start("cube.gcode")
    assert ("/printer/print/pause", {}) in fake.calls
    assert ("/printer/print/start", {"filename": "cube.gcode"}) in fake.calls
    c.upload(io.BytesIO(b"G28\n"), "test.gcode", start=True)
    assert fake.uploads == [{"filename": "test.gcode", "size": 4, "print": "true"}]


def test_query_encodes_object_names(fake):
    """lessonslearned № 7: «heater_generic chamber» в query-строке кодируется."""
    MoonrakerClient("127.0.0.1", fake.port, profile=Q1).query({"heater_generic chamber": ["temperature"]})
    assert ("query", "heater_generic%20chamber=temperature") in fake.calls


def test_client_no_connection():
    with pytest.raises(base.DriverError, match="Нет связи"):
        MoonrakerClient("127.0.0.1", 1, profile=Q1, timeout=1).check()


# --- монитор (WebSocket) ----------------------------------------------------------------------


def test_monitor_subscribe_updates_and_history():
    async def scenario():
        import aiohttp

        fake = FakeMoonraker()
        await fake.start_async()
        states, jobs = [], []

        async def on_state(s):
            states.append(s)

        async def on_job(j, live):
            jobs.append((j["external_id"], live))

        async with aiohttp.ClientSession() as session:
            mon = MoonrakerMonitor("127.0.0.1", fake.port, profile=Q1, session=session)
            task = asyncio.create_task(mon.run(on_state, on_job, history_since=5000))
            for _ in range(50):
                if states:
                    break
                await asyncio.sleep(0.05)
            await fake.notify("notify_status_update", [{"virtual_sdcard": {"progress": 0.5}}, 2.0])
            await fake.notify(
                "notify_history_changed",
                [
                    {
                        "action": "finished",
                        "job": {
                            "job_id": "0000A3",
                            "filename": "gear_z24.gcode",
                            "status": "completed",
                            "start_time": 1761550000.0,
                            "end_time": 1761555400.0,
                            "print_duration": 5300.0,
                        },
                    }
                ],
            )
            await asyncio.sleep(0.3)
            await fake.close_clients()
            with pytest.raises(Exception):  # noqa: B017 — обрыв соединения приводит к исключению
                await asyncio.wait_for(task, 3)
        await fake.stop_async()
        return states, jobs, fake.calls

    states, jobs, calls = asyncio.run(scenario())
    assert states[0]["status"] == base.PRINTING and states[0]["temps"]["chamber"] == [45.2, 50.0]
    assert states[-1]["job"]["progress"] == 0.5
    # из истории — только задания после history_since; затем живое событие
    assert jobs == [("0000A2", False), ("0000A3", True)]
    subscribed = next(c[1] for c in calls if c[0] == "subscribe")
    assert "heater_generic chamber" in subscribed and "print_stats" in subscribed


# --- коллектор: задания и наработка -----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_collector_save_job_and_awaiting_removal():
    printer = make_printer(history_since=2000.0, print_seconds_base=3600)
    job = job_from_history(
        {
            "job_id": "J1",
            "filename": "a.gcode",
            "status": "completed",
            "start_time": 2500.0,
            "end_time": 6100.0,
            "print_duration": 3600.0,
        }
    )
    assert async_to_sync(collector.save_job)(printer.pk, job, True) is True
    assert async_to_sync(collector.save_job)(printer.pk, job, True) is False  # повтор не дублирует
    old = dict(job, external_id="J0", end_time=1500.0)
    assert async_to_sync(collector.save_job)(printer.pk, old, False) is False  # уже в базе наработки
    printer.refresh_from_db()
    assert printer.awaiting_removal is True
    assert printer.print_seconds == 7200 and PrintJob.objects.count() == 1
    async_to_sync(collector.add_on_seconds)(printer.pk, 65)
    printer.refresh_from_db()
    assert printer.on_seconds == 65 and printer.last_seen_at is not None


# --- веб: права, добавление, команды ----------------------------------------------------------


@pytest.mark.django_db
def test_add_printer_takes_hours_from_history(client, fake):
    make_user("denis", ROLE_ADMIN)
    login(client, "denis")
    check = client.post(
        reverse("printer_check"), {"profile_id": "qidi_q1_pro", "host": "127.0.0.1", "port": fake.port}
    )
    body = check.content.decode()
    assert "Связь есть" in body and "v0.7.1-609-dirty" in body and 'data-fill-hours="100.0"' in body
    resp = client.post(
        reverse("printer_add"),
        {
            "profile_id": "qidi_q1_pro",
            "name": "Q1Pro-01",
            "host": "http://127.0.0.1/",
            "port": fake.port,
            "zone": "Зал",
            "power_w": 350,
            "amortization_hours": 5000,
        },
    )
    assert resp.status_code == 302
    p = Printer.objects.get()
    assert p.host == "127.0.0.1" and p.print_seconds_base == 360000 and p.history_since > 0
    assert AuditLog.objects.filter(action="printer_add").exists()


@pytest.mark.django_db
def test_add_printer_default_port_and_api_key_encrypted(client):
    make_user("denis", ROLE_ADMIN)
    login(client, "denis")
    client.post(
        reverse("printer_add"),
        {
            "profile_id": "qidi_q1_pro",
            "name": "Q1",
            "host": "10.255.255.1",
            "api_key": "secret-key",
            "amortization_hours": 5000,
            "print_hours_base": "12.5",
        },
    )
    p = Printer.objects.get()
    assert p.port == 7125 and p.print_seconds_base == 45000
    assert p.api_key == "secret-key" and "secret-key" not in p.api_key_encrypted


@pytest.mark.django_db
def test_dashboard_shows_live_state(client):
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer()
    data = load_fixture("moonraker_q1pro_printing.json")
    write_state(p.pk, normalize(data["status"], data["metadata"], Q1))
    login(client, "sergey")
    html = client.get("/").content.decode()
    assert "Q1Pro-01" in html and "35 %" in html and "Печать" in html and "Пауза" in html
    tiles = client.get(reverse("printer_tiles")).content.decode()
    assert "слой 42 из 120" in tiles


@pytest.mark.django_db
def test_rights_for_commands(client, fake):
    make_user("anna", ROLE_MANAGER)
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)
    login(client, "anna")
    assert client.post(reverse("printer_command", args=[p.pk, "pause"])).status_code == 403
    assert client.get(reverse("printer_add")).status_code == 403
    client.post(reverse("logout"))
    login(client, "sergey")
    assert client.post(reverse("printer_command", args=[p.pk, "pause"])).status_code == 302
    assert ("/printer/print/pause", {}) in fake.calls
    assert AuditLog.objects.filter(action="print_pause", username="sergey").exists()
    assert client.get(reverse("printer_add")).status_code == 403


@pytest.mark.django_db
def test_start_blocked_until_plate_cleared(client, fake):
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port, awaiting_removal=True)
    write_state(p.pk, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    login(client, "sergey")
    client.post(reverse("printer_start", args=[p.pk]), {"filename": "cube.gcode"})
    assert not any(c[0] == "/printer/print/start" for c in fake.calls)
    client.post(reverse("printer_cleared", args=[p.pk]))
    p.refresh_from_db()
    assert p.awaiting_removal is False
    client.post(reverse("printer_start", args=[p.pk]), {"filename": "cube.gcode"})
    assert ("/printer/print/start", {"filename": "cube.gcode"}) in fake.calls


@pytest.mark.django_db
def test_files_tab_and_upload(client, fake):
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)
    write_state(p.pk, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    login(client, "sergey")
    html = client.get(reverse("printer_detail", args=[p.pk]) + "?tab=files").content.decode()
    assert "gear_z24.gcode" in html and "Печать" in html
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.post(
        reverse("printer_upload", args=[p.pk]),
        {"file": SimpleUploadedFile("part.gcode", b"PRINT_START BED=60\nG1 X1\n")},
    )
    assert fake.uploads[-1]["filename"] == "part.gcode" and fake.uploads[-1]["print"] is None
    bad = client.post(
        reverse("printer_upload", args=[p.pk]), {"file": SimpleUploadedFile("x.stl", b"solid")}, follow=True
    )
    assert "Нужен файл G-code" in bad.content.decode()


@pytest.mark.django_db
def test_detail_tabs_and_offline(client):
    make_user("anna", ROLE_MANAGER)
    p = make_printer()
    login(client, "anna")
    html = client.get(reverse("printer_detail", args=[p.pk])).content.decode()
    assert "Нет связи" in html and "Пауза" not in html
    assert client.get(reverse("printer_detail", args=[p.pk]) + "?tab=hours").status_code == 200
    assert client.get(reverse("printer_detail", args=[p.pk]) + "?tab=history").status_code == 200
    # у менеджера нет права управлять печатью — вкладка файлов недоступна
    assert client.get(reverse("printer_detail", args=[p.pk]) + "?tab=files").status_code == 302


@pytest.mark.django_db
def test_snapshot_placeholder_when_camera_unreachable(client):
    make_user("anna", ROLE_MANAGER)
    p = make_printer(host="127.0.0.1")
    login(client, "anna")
    resp = client.get(reverse("printer_snapshot", args=[p.pk]))
    assert resp.status_code == 200 and resp["Content-Type"] == "image/svg+xml"
    assert read_state(p.pk) is None


# --- запись реального Q1 Pro в простое (tests/fixtures/moonraker_q1pro_idle.json) ---------------

REAL = "moonraker_q1pro_idle.json"


def test_real_q1pro_idle_normalize():
    data = load_fixture(REAL)
    s = normalize(data["status"], profile=Q1)
    assert s["status"] == base.READY and s["job"] is None and s["message"] == ""
    assert s["temps"] == {"nozzle": [27.5, 0.0], "bed": [25.5, 0.0], "chamber": [24.1, 0.0]}
    # объект камеры из профиля действительно есть у принтера
    assert Q1.objects["chamber"] in data["objects"]
    # камера в Moonraker не зарегистрирована — адрес берётся из профиля
    assert data["webcams"] == {"webcams": []} and Q1.snapshot_url


def test_real_q1pro_client():
    server = FakeMoonraker(REAL).start_thread()
    try:
        c = MoonrakerClient("127.0.0.1", server.port, profile=Q1)
        info = c.check()
        assert info["moonraker"] == "v0.7.1-609-gbdd0222-dirty"
        assert info["klipper"] == "v0.10.0-530-g3387a9c2-dirty"
        assert info["printer_state"] == "standby"
        assert round(c.history_totals()["print_seconds"] / 3600, 1) == 2506.8
        # служебные .plr и .cache скрыты
        paths = [f["path"] for f in c.list_files()]
        assert paths and not any(p.startswith(".") for p in paths)
    finally:
        server.stop_thread()


def test_real_q1pro_history_job():
    item = load_fixture(REAL)["history"][0]
    job = job_from_history(item)
    assert job["status"] == "completed" and job["external_id"] == "00007C"
    assert job["filament_g"] == 83.45 and round(job["print_duration"]) == 32027
    from printers.templatetags.printers_tags import gcode_name

    assert gcode_name(job["filename"]) == "part-A - 3_9h24m.gcode"
    assert gcode_name("folder/a.gcode") == "folder/a.gcode"


def test_eta_filter():
    from django.utils import timezone

    from printers.templatetags.printers_tags import eta

    assert eta(None) == "—"
    now_hm = timezone.localtime(timezone.now()).strftime("%H:%M")
    assert eta(0) == now_hm  # «осталось 0» — печать заканчивается прямо сейчас
    future = timezone.localtime(timezone.now() + timezone.timedelta(hours=1)).strftime("%H:%M")
    assert eta(3600) == future


def test_klipper_error_explained():
    """Реальная ошибка Q1 Pro 2026-10-27 при запуске печати."""
    st = {
        "webhooks": {"state": "ready"},
        "print_stats": {
            "state": "error",
            "filename": ".cache/project_2_PETG_4h4m.gcode",
            "message": "Must home axis first: 0.000 0.000 3.000 [0.000]",
        },
        "virtual_sdcard": {"progress": 0.0},
    }
    s = normalize(st, profile=Q1)
    assert s["status"] == base.ERROR
    assert s["message"].startswith("Оси не в исходном положении") and "Must home axis first" in s["message"]
    assert s["job"]["filename"] == ".cache/project_2_PETG_4h4m.gcode"


def test_upload_reports_progress(fake):
    c = MoonrakerClient("127.0.0.1", fake.port, profile=Q1)
    calls = []
    payload = b"G1 X1\n" * 200_000  # ~1,2 МБ
    c.upload(io.BytesIO(payload), "big.gcode", progress=lambda sent, total: calls.append((sent, total)))
    assert fake.uploads[-1] == {"filename": "big.gcode", "size": len(payload), "print": None}
    assert calls and calls[-1][0] == calls[-1][1] and calls[-1][1] > len(payload)


@pytest.mark.django_db
def test_upload_xhr_returns_redirect_and_clears_progress(client, fake):
    from django.core.files.uploadedfile import SimpleUploadedFile

    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)
    login(client, "sergey")
    url = reverse("printer_upload", args=[p.pk])
    resp = client.post(
        url,
        {
            "file": SimpleUploadedFile("part.gcode", b"PRINT_START BED=60\nG1 X1\n"),
            "upload_id": "abc12345xyz",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.json() == {"redirect": reverse("printer_detail", args=[p.pk]) + "?tab=files"}
    progress = client.get(reverse("printer_upload_progress", args=[p.pk, "abc12345xyz"]))
    assert progress.json() == {}
    assert client.get(reverse("printer_upload_progress", args=[p.pk, "BAD!"])).status_code == 404
    html = client.get(reverse("printer_detail", args=[p.pk]) + "?tab=files").content.decode()
    assert "загружен" in html and "data-upload" in html


# --- проверка G-code и сообщения Klipper ----------------------------------------------------------

GOOD_HEAD = "; HEADER_BLOCK_START\n; generated by OrcaSlicer\n; HEADER_BLOCK_END\nM191 S50\nPRINT_START BED=115 HOTEND=295 CHAMBER=50\nG0 Z5\n"
GOOD_TAIL = "; CONFIG_BLOCK_START\n; printer_model = Qidi Q1 Pro\n; CONFIG_BLOCK_END\n"
# Как у файла, на котором Q1 Pro остановился 2026-10-27: START_PRINT и T0 вместо PRINT_START.
BAD_HEAD = "; generated by OrcaSlicer 2.4.1\nSTART_PRINT EXTRUDER_TEMP=240 BED_TEMP=80\nT0\nG1 Z3 F600\n"
BAD_TAIL = "; printer_model = Generic Klipper Printer\n"


def test_check_gcode():
    from printers.gcode_check import check_gcode

    assert check_gcode(GOOD_HEAD, GOOD_TAIL, Q1) == []
    warnings = check_gcode(BAD_HEAD, BAD_TAIL, Q1)
    text = " ".join(warnings)
    assert "нет PRINT_START" in text and "START_PRINT" in text
    assert "T0" in text and "Generic Klipper Printer" in text
    assert check_gcode("", "", Q1) == []  # пустое начало — не придираемся


def test_check_real_q1pro_file_head():
    """Начало реального файла под Q1 Pro (OrcaSlicer 2.4.2, профиль Qidi Q1 Pro) — без предупреждений."""
    from printers.gcode_check import check_gcode

    head = (
        "; HEADER_BLOCK_START\n; generated by OrcaSlicer 2.4.1 on 2026-07-02 at 14:40:59\n; HEADER_BLOCK_END\n"
        "EXCLUDE_OBJECT_DEFINE NAME=part CENTER=122.5,122.5 POLYGON=[[25.5,30.5],[27.5,28.5]]\n"
        "M73 P0 R708\nM106 S0\nM106 P2 S0\n;TYPE:Custom\nM106 P2 S255\n"
        "M191 S50 ;set chamber_temperature and wait for it to be reached\nM106 P2 S0\n"
        "PRINT_START BED=115 HOTEND=295 CHAMBER=50\nM83\nM140 S115\nM104 S295\nG4 P3000\n"
        "G0 X16.3779 Y12.6367 Z5 F6000\n"
    )
    assert check_gcode(head, "; printer_model = Qidi Q1 Pro\n", Q1) == []


@pytest.mark.django_db
def test_start_asks_confirmation_for_foreign_file(client, fake):
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)
    write_state(p.pk, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    fake.gcode_files["project_2_PETG_4h4m.gcode"] = (BAD_HEAD + "G1 X1\n" * 1000 + BAD_TAIL).encode()
    fake.gcode_files["good.gcode"] = (GOOD_HEAD + "G1 X1\n" * 1000 + GOOD_TAIL).encode()
    login(client, "sergey")
    url = reverse("printer_start", args=[p.pk])

    resp = client.post(url, {"filename": "project_2_PETG_4h4m.gcode"})
    assert resp.status_code == 200 and "Всё равно запустить" in resp.content.decode()
    assert not any(c[0] == "/printer/print/start" for c in fake.calls)

    client.post(url, {"filename": "project_2_PETG_4h4m.gcode", "confirm": "1"})
    assert ("/printer/print/start", {"filename": "project_2_PETG_4h4m.gcode"}) in fake.calls
    assert AuditLog.objects.get(action="print_start").details["warnings_confirmed"] is True

    write_state(p.pk, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    assert client.post(url, {"filename": "good.gcode"}).status_code == 302
    assert ("/printer/print/start", {"filename": "good.gcode"}) in fake.calls


@pytest.mark.django_db
def test_upload_checks_gcode(client, fake):
    from django.core.files.uploadedfile import SimpleUploadedFile

    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)
    login(client, "sergey")
    check = client.post(reverse("printer_check_file", args=[p.pk]), {"head": BAD_HEAD, "tail": BAD_TAIL})
    assert len(check.json()["warnings"]) == 3
    url = reverse("printer_upload", args=[p.pk])
    bad = (BAD_HEAD + BAD_TAIL).encode()
    client.post(url, {"file": SimpleUploadedFile("bad.gcode", bad)})
    assert fake.uploads == []  # без подтверждения не загружаем
    client.post(url, {"file": SimpleUploadedFile("bad.gcode", bad), "confirm_warnings": "1"})
    assert fake.uploads[-1]["filename"] == "bad.gcode"


def test_klipper_responses_explain_error():
    from printers.drivers.moonraker import decorate, keep_response

    console = [
        line
        for line in [
            "File opened:project_2_PETG_4h4m.gcode Size:21675609",
            '// Unknown command:"START_PRINT"',
            '// Unknown command:"T0"',
            "ok",
            "B:25.0 /0.0 T0:27.0 /0.0",
            "!! Must home axis first: 0.000 0.000 3.000",
        ]
        if keep_response(line)
    ]
    assert len(console) == 4
    st = {
        "webhooks": {"state": "ready"},
        "print_stats": {"state": "error", "filename": "x.gcode", "message": "Must home axis first: 0 0 3"},
    }
    s = decorate(normalize(st, profile=Q1), console)
    assert "START_PRINT, T0" in s["message"] and s["console"][-1].startswith("!!")


def test_monitor_collects_gcode_responses():
    async def scenario():
        import aiohttp

        fake = FakeMoonraker(REAL)
        await fake.start_async()
        states = []

        async def on_state(s):
            states.append(s)

        async def on_job(j, live):
            pass

        async with aiohttp.ClientSession() as session:
            mon = MoonrakerMonitor("127.0.0.1", fake.port, profile=Q1, session=session)
            task = asyncio.create_task(mon.run(on_state, on_job, history_since=time.time()))
            for _ in range(50):
                if states:
                    break
                await asyncio.sleep(0.05)
            await fake.notify("notify_gcode_response", ["// old message"])
            await fake.notify("notify_gcode_response", ["File opened:a.gcode Size:10"])
            await fake.notify("notify_gcode_response", ['// Unknown command:"START_PRINT"'])
            await fake.notify(
                "notify_status_update",
                [
                    {
                        "print_stats": {
                            "state": "error",
                            "filename": "a.gcode",
                            "message": "Must home axis first",
                        }
                    },
                    3.0,
                ],
            )
            await asyncio.sleep(0.3)
            await fake.close_clients()
            with pytest.raises(Exception):  # noqa: B017
                await asyncio.wait_for(task, 3)
        await fake.stop_async()
        return states

    states = asyncio.run(scenario())
    last = states[-1]
    assert last["status"] == base.ERROR and "START_PRINT" in last["message"]
    assert "// old message" not in last["console"]  # очищено на новом задании


# --- свет ---------------------------------------------------------------------------------------


def test_light_state_and_subscription():
    from printers.drivers.moonraker import subscribe_objects

    assert Q1.can("light") and subscribe_objects(Q1)["output_pin caselight"] == ["value"]
    data = load_fixture(REAL)
    assert normalize(data["status"], profile=Q1)["light"] is False  # в записи 2026-10-27 свет выключен
    data["status"]["output_pin caselight"]["value"] = 1.0
    assert normalize(data["status"], profile=Q1)["light"] is True


@pytest.mark.django_db
def test_light_toggle(client, fake):
    make_user("sergey", ROLE_OPERATOR)
    make_user("anna", ROLE_MANAGER)
    p = make_printer(port=fake.port)
    write_state(
        p.pk, {"status": base.READY, "temps": {}, "job": None, "light": False, "updated": time.time()}
    )
    login(client, "sergey")
    html = client.get(reverse("printer_live", args=[p.pk])).content.decode()
    assert "Свет в камере: <b>выключен</b>" in html and "Включить свет" in html
    client.post(reverse("printer_light", args=[p.pk, "on"]))
    assert ("/printer/gcode/script", {"script": "SET_PIN PIN=caselight VALUE=1"}) in fake.calls
    assert read_state(p.pk)["light"] is True
    assert AuditLog.objects.filter(action="printer_light", details__on=True).exists()
    client.post(reverse("printer_light", args=[p.pk, "off"]))
    assert ("/printer/gcode/script", {"script": "SET_PIN PIN=caselight VALUE=0"}) in fake.calls
    client.post(reverse("logout"))
    login(client, "anna")  # у менеджера нет права управлять принтером
    assert client.post(reverse("printer_light", args=[p.pk, "on"])).status_code == 403
    assert "Включить свет" not in client.get(reverse("printer_live", args=[p.pk])).content.decode()


# --- неудачная отмена и нагрев без печати (Q1 Pro, 2026-10-27) ------------------------------------


@pytest.mark.django_db
def test_failed_cancel_shows_klipper_error_and_heating_idle(client, fake):
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)

    async def bad_request(request):
        return web.json_response({"error": {"code": 400, "message": "Bad Request"}}, status=400)

    # Подменяем обработчик отмены: так ответил реальный Q1 Pro, когда CANCEL_PRINT упал на «Move out of range».
    fake.command_override = bad_request
    write_state(
        p.pk,
        {
            "status": base.READY,
            "message": "",
            "job": None,
            "updated": time.time(),
            "temps": {"nozzle": [33.2, 0.0], "bed": [32.4, 0.0], "chamber": [47.3, 50.0]},
            "console": ["File selected", "!! Move out of range: 120.000 120.000 299.786 [0.000]"],
        },
    )
    login(client, "sergey")
    resp = client.post(reverse("printer_command", args=[p.pk, "cancel"]), follow=True)
    html = resp.content.decode()
    assert "принтер отклонил команду" in html and "Klipper: Move out of range" in html
    assert "Макрос отмены на принтере не выполнился до конца" in html
    assert "Нагрев включён без печати" in html and "камера 50 °C" in html
    # кнопка «Выключить нагрев» отправляет TURN_OFF_HEATERS
    client.post(reverse("printer_command", args=[p.pk, "heaters_off"]))
    assert ("/printer/gcode/script", {"script": "TURN_OFF_HEATERS"}) in fake.calls
    assert AuditLog.objects.filter(action="heaters_off").exists()
    # и на дашборде это в «Требует внимания»
    assert "нагрев включён без печати" in client.get("/").content.decode()


def test_heating_idle_only_when_not_printing():
    from printers.state import view_state

    pr = Printer(name="x", profile_id="qidi_q1_pro", host="h", port=1)
    temps = {"nozzle": [200, 250], "bed": [60, 60], "chamber": None}
    assert view_state(pr, {"status": base.PRINTING, "temps": temps})["heating_idle"] == []
    assert view_state(pr, {"status": base.READY, "temps": temps})["heating_idle"] == [
        "сопло 250 °C",
        "стол 60 °C",
    ]
    assert view_state(pr, {"status": base.READY, "temps": {"nozzle": [30, 0]}})["heating_idle"] == []
