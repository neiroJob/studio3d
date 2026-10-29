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
from django.utils import timezone

from accounts.roles import ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR
from core.models import AuditLog
from printers import collector
from printers.drivers import base
from printers.drivers.moonraker import MoonrakerClient, MoonrakerMonitor, job_from_history, normalize
from printers.models import Printer, PrinterFault, PrintJob
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


def test_k1c_profile():
    """Проверено на двух реальных принтерах K1C 2025 (nexusp Moonraker v0.1.1), 2026-10-28."""
    k1c = get_profile("creality_k1c")
    assert k1c.driver == "moonraker" and k1c.port == 7125 and k1c.verified
    assert k1c.objects["chamber"] == "temperature_sensor chassis"
    assert {"pause", "resume", "cancel", "start_file", "upload", "light"} <= k1c.capabilities
    # Нет объекта/макроса света через Moonraker на этой прошивке — свет идёт отдельной
    # командой нативного WS-протокола Creality (как у K1), доступного на этом принтере
    # параллельно с Moonraker (порт 9999). Проверено на реальном принтере (включили/выключили).
    assert k1c.light_object == "" and k1c.light_native_port == 9999
    # Камера — только страница WebRTC (нет отдельного JPEG-снимка), показывается во фрейме.
    assert k1c.camera_type == "webrtc_iframe"
    assert k1c.snapshot_url_for("10.0.0.7") == ""
    assert k1c.page_url_for("10.0.0.7") == "http://10.0.0.7:8000"


def test_k1c_set_light_uses_native_protocol(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "printers.drivers.creality_native.set_light_native",
        lambda host, port, on, timeout=8: calls.append((host, port, on)),
    )
    k1c = get_profile("creality_k1c")
    c = MoonrakerClient("10.0.0.5", k1c.port, profile=k1c, timeout=3)
    c.set_light(True)
    assert calls == [("10.0.0.5", 9999, True)]


def test_view_state_uses_light_hint_when_live_status_unknown():
    p = Printer(name="x", profile_id="creality_k1c", host="h", port=1, light_hint=True)
    s = view_state(p, {"status": base.READY, "temps": {}, "job": None, "light": None, "updated": time.time()})
    assert s["light"] is True
    # Если принтер вживую отдаёт свет — он важнее подсказки.
    s2 = view_state(p, {"status": base.READY, "temps": {}, "job": None, "light": False, "updated": time.time()})
    assert s2["light"] is False


def test_k1c_check_reports_camera_page_reachable(fake):
    import dataclasses

    k1c = dataclasses.replace(get_profile("creality_k1c"), page_url="http://{host}:" + str(fake.port) + "/webrtc_page")
    c = MoonrakerClient("127.0.0.1", fake.port, profile=k1c, timeout=5)
    info = c.check()
    assert info["camera"] == f"http://127.0.0.1:{fake.port}/webrtc_page" and info["camera_ok"] is True


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


def test_normalize_warns_on_filament_sensor_runout():
    """Датчик обрыва нити (profile.objects.filament_sensor, Q1 Pro — filament_switch_sensor fila):
    принтер может считать, что печатает, хотя пластик на самом деле не подаётся."""
    st = {
        "webhooks": {"state": "ready"},
        "print_stats": {"state": "printing", "filename": "a.gcode"},
        "filament_switch_sensor fila": {"enabled": True, "filament_detected": False},
    }
    printing = normalize(st, profile=Q1)
    assert printing["status"] == base.PRINTING and "датчик нити" in printing["message"].lower()
    assert printing["filament"] == {"present": False, "enabled": True}

    # датчик выключен или нить есть — предупреждения нет
    st["filament_switch_sensor fila"] = {"enabled": False, "filament_detected": False}
    assert normalize(st, profile=Q1)["message"] == ""
    st["filament_switch_sensor fila"] = {"enabled": True, "filament_detected": True}
    assert normalize(st, profile=Q1)["message"] == ""
    assert normalize(st, profile=Q1)["filament"] == {"present": True, "enabled": True}

    plus4 = get_profile("qidi_plus_4")
    st["print_stats"] = {"state": "standby"}
    st["filament_switch_sensor fila"] = {"enabled": False, "filament_detected": True}
    assert normalize(st, profile=plus4)["filament"] == {"present": True, "enabled": False}

    # не печатает — датчик не проверяется
    st["print_stats"] = {"state": "standby"}
    st["filament_switch_sensor fila"] = {"enabled": True, "filament_detected": False}
    assert normalize(st, profile=Q1)["message"] == ""


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


def test_client_delete_and_rename_file(fake):
    c = MoonrakerClient("127.0.0.1", fake.port, profile=Q1)
    c.delete_file("cube.gcode")
    assert ("DELETE", "cube.gcode") in fake.calls
    c.rename_file("sub/cube.gcode", "cube2.gcode")
    assert ("move", {"source": "gcodes/sub/cube.gcode", "dest": "gcodes/sub/cube2.gcode"}) in fake.calls
    c.rename_file("cube.gcode", "cube3.gcode")  # без подпапки — переименование в корне
    assert ("move", {"source": "gcodes/cube.gcode", "dest": "gcodes/cube3.gcode"}) in fake.calls


def test_client_file_exists(fake):
    c = MoonrakerClient("127.0.0.1", fake.port, profile=Q1)
    assert c.file_exists("gear_z24.gcode") is True
    fake.missing_files.add(".plr/plr.gcode")
    assert c.file_exists(".plr/plr.gcode") is False


@pytest.mark.django_db
def test_files_tab_shows_power_loss_recovery_when_file_present(client, fake):
    """QIDI Q1 Pro: если принтер сам создал .plr/plr.gcode (потеря питания во время печати),
    на вкладке «Файлы» должна появиться возможность его запустить (lessonslearned/ТЗ про
    восстановление после аварийных ситуаций — ❓ сам файл не проверен на реальной потере питания)."""
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)
    write_state(p.pk, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    login(client, "sergey")
    html = client.get(reverse("printer_detail", args=[p.pk]) + "?tab=files").content.decode()
    assert "Восстановить печать" in html

    fake.missing_files.add(".plr/plr.gcode")
    html = client.get(reverse("printer_detail", args=[p.pk]) + "?tab=files").content.decode()
    assert "Восстановить печать" not in html


def test_client_thumbnail(fake):
    """Превью модели — из thumbnails[] в метаданных G-code, самое маленькое (список файлов)."""
    fake.data["metadata"]["thumbnails"] = [
        {"width": 300, "height": 300, "relative_path": ".thumbs/gear_z24-300x300.png"},
        {"width": 48, "height": 48, "relative_path": ".thumbs/gear_z24-48x48.png"},
    ]
    fake.gcode_files[".thumbs/gear_z24-48x48.png"] = b"\x89PNG\r\n\x1a\nsmall"
    fake.gcode_files[".thumbs/gear_z24-300x300.png"] = b"\x89PNG\r\n\x1a\nbig"
    c = MoonrakerClient("127.0.0.1", fake.port, profile=Q1)
    assert c.thumbnail("gear_z24.gcode") == b"\x89PNG\r\n\x1a\nsmall"


def test_client_thumbnail_subdirectory_and_missing(fake):
    fake.data["metadata"]["thumbnails"] = [{"width": 48, "height": 48, "relative_path": ".thumbs/part-48x48.png"}]
    fake.gcode_files["sub/.thumbs/part-48x48.png"] = b"\x89PNG\r\n\x1a\nnested"
    c = MoonrakerClient("127.0.0.1", fake.port, profile=Q1)
    assert c.thumbnail("sub/part.gcode") == b"\x89PNG\r\n\x1a\nnested"
    fake.data["metadata"]["thumbnails"] = []
    assert c.thumbnail("sub/part.gcode") is None


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


def test_monitor_catches_up_history_without_notification():
    """Некоторые сборки Moonraker (встречалось на изменённой прошивке QIDI) не шлют
    notify_history_changed — задание не должно теряться, если печать просто перестала быть активной."""

    async def scenario():
        import aiohttp

        fake = FakeMoonraker()
        await fake.start_async()
        fake.data["history"].append(
            {
                "job_id": "LIVE1",
                "filename": "gear.gcode",
                "status": "completed",
                "start_time": time.time() - 100,
                "end_time": time.time() - 1,
                "print_duration": 99.0,
            }
        )
        jobs = []

        async def on_state(s):
            pass

        async def on_job(j, live):
            jobs.append((j["external_id"], live))

        async with aiohttp.ClientSession() as session:
            mon = MoonrakerMonitor("127.0.0.1", fake.port, profile=Q1, session=session)
            task = asyncio.create_task(mon.run(on_state, on_job, history_since=1e12))
            for _ in range(50):
                if mon.status:
                    break
                await asyncio.sleep(0.05)
            # печать закончилась, но notify_history_changed принтер не прислал
            await fake.notify("notify_status_update", [{"print_stats": {"state": "complete"}}, 2.0])
            await asyncio.sleep(1.5)
            await fake.close_clients()
            with pytest.raises(Exception):  # noqa: B017 — обрыв соединения приводит к исключению
                await asyncio.wait_for(task, 3)
        await fake.stop_async()
        return jobs

    jobs = asyncio.run(scenario())
    assert ("LIVE1", True) in jobs


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
    printer.awaiting_removal = False
    printer.save(update_fields=["awaiting_removal"])
    assert async_to_sync(collector.save_job)(printer.pk, job, True) is False  # повтор не дублирует
    printer.refresh_from_db()
    assert printer.awaiting_removal is True  # повторное завершение тоже восстанавливает состояние
    old = dict(job, external_id="J0", end_time=1500.0)
    assert async_to_sync(collector.save_job)(printer.pk, old, False) is False  # уже в базе наработки
    printer.refresh_from_db()
    assert printer.awaiting_removal is True
    assert printer.print_seconds == 7200 and PrintJob.objects.count() == 1
    async_to_sync(collector.add_on_seconds)(printer.pk, 65)
    printer.refresh_from_db()
    assert printer.on_seconds == 65 and printer.last_seen_at is not None


@pytest.mark.django_db(transaction=True)
def test_collector_recovers_job_lost_in_connection_gap():
    """У K1 нет истории на самом принтере: если соединение обрывается прямо во время печати,
    монитор пересоздаётся с нуля и теряет отслеживание задания в памяти (lessonslearned № 15/
    текущий баг — «Ждёт снятия» и история не работали после реального обрыва). Коллектор должен
    закрыть такое задание сам, по последним показаниям перед обрывом."""
    printer = make_printer(name="K1-gap", profile_id="creality_k1")
    worker = collector.PrinterWorker(printer, session=None)
    gap_state = {
        "status": base.PRINTING,
        "job": {"filename": "part.gcode", "progress": 0.87, "elapsed": 1200.0, "filament_mm": 500.0},
        "updated": time.time() - 1200,
    }
    worker.pending_job_check = gap_state  # имитирует ветку except в loop() после обрыва связи

    async_to_sync(worker.on_state)({"status": base.READY, "job": None, "updated": time.time()})
    job = PrintJob.objects.get(printer=printer)
    assert job.status == "interrupted" and round(job.print_duration) == 1200
    assert printer.awaiting_removal is False  # прогресс не дотянул до 100% — не «успех»
    assert worker.pending_job_check is None

    # полный прогресс в последнем известном кадре — считаем успешным завершением
    printer2 = make_printer(name="K1-gap-2", profile_id="creality_k1")
    worker2 = collector.PrinterWorker(printer2, session=None)
    worker2.pending_job_check = {
        "status": base.PRINTING,
        "job": {"filename": "done.gcode", "progress": 1.0, "elapsed": 600.0, "filament_mm": 20.0},
        "updated": time.time() - 5,
    }
    async_to_sync(worker2.on_state)({"status": base.READY, "job": None, "updated": time.time()})
    printer2.refresh_from_db()
    job2 = PrintJob.objects.get(printer=printer2)
    assert job2.status == "completed" and printer2.awaiting_removal is True

    # если после переподключения то же задание продолжается — ничего не закрываем
    printer3 = make_printer(name="K1-gap-3", profile_id="creality_k1")
    worker3 = collector.PrinterWorker(printer3, session=None)
    worker3.pending_job_check = {
        "status": base.PRINTING,
        "job": {"filename": "continuing.gcode", "progress": 0.5, "elapsed": 300.0},
        "updated": time.time() - 2,
    }
    async_to_sync(worker3.on_state)(
        {"status": base.PRINTING, "job": {"filename": "continuing.gcode", "progress": 0.52}, "updated": time.time()}
    )
    assert PrintJob.objects.filter(printer=printer3).count() == 0


@pytest.mark.django_db(transaction=True)
def test_k1_ready_transition_recovers_completed_job():
    printer = make_printer(name="K1-ready-transition", profile_id="creality_k1")
    worker = collector.PrinterWorker(printer, session=None)
    started = time.time() - 900
    worker.last_state = {
        "status": base.PRINTING,
        "updated": time.time(),
        "job": {
            "filename": "finished.gcode",
            "external_id": f"finished.gcode@{round(started)}",
            "progress": 1.0,
            "elapsed": 900,
            "filament_mm": 850,
        },
    }

    async_to_sync(worker.on_state)({"status": base.READY, "job": None, "updated": time.time()})

    job = PrintJob.objects.get(printer=printer)
    printer.refresh_from_db()
    assert job.status == "completed"
    assert job.external_id == f"finished.gcode@{round(started)}"
    assert printer.awaiting_removal is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("profile_id", ["qidi_q1_pro", "creality_k1c"])
def test_full_print_cycle_sets_awaiting_removal_and_history_moonraker(profile_id):
    """Полный цикл «печать идёт → завершилась» через настоящий PrinterWorker (не только через
    on_job напрямую) должен писать в историю и выставлять «ждёт снятия» — для обоих профилей
    на драйвере moonraker, не только для того, на котором баг не проявлялся (K1C)."""

    async def scenario(worker):
        import aiohttp

        fake = FakeMoonraker()
        await fake.start_async()
        try:
            async with aiohttp.ClientSession() as session:
                mon = MoonrakerMonitor(
                    "127.0.0.1", fake.port, profile=get_profile(profile_id), session=session
                )
                task = asyncio.create_task(mon.run(worker.on_state, worker.on_job, history_since=1e12))
                for _ in range(50):
                    if worker.connected:
                        break
                    await asyncio.sleep(0.05)
                await fake.notify("notify_status_update", [{"print_stats": {"state": "printing"}}, 2.0])
                await asyncio.sleep(0.1)
                await fake.notify(
                    "notify_history_changed",
                    [
                        {
                            "action": "finished",
                            "job": {
                                "job_id": "FULLCYCLE1",
                                "filename": "part.gcode",
                                "status": "completed",
                                "start_time": time.time() - 600,
                                "end_time": time.time(),
                                "print_duration": 600.0,
                            },
                        }
                    ],
                )
                await asyncio.sleep(0.2)
                await fake.close_clients()
                with pytest.raises(Exception):  # noqa: B017 — обрыв соединения приводит к исключению
                    await asyncio.wait_for(task, 3)
        finally:
            await fake.stop_async()

    printer = make_printer(name=f"full-{profile_id}", profile_id=profile_id, port=1)
    worker = collector.PrinterWorker(printer, session=None)
    asyncio.run(scenario(worker))
    printer.refresh_from_db()
    assert printer.awaiting_removal is True
    job = PrintJob.objects.get(printer=printer, external_id="FULLCYCLE1")
    assert job.status == "completed"


# --- веб: права, добавление, команды ----------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("profile_id", ["qidi_q1_pro", "qidi_plus_4", "creality_k1", "creality_k1c"])
def test_gcode_command_is_available_for_all_printers(client, monkeypatch, profile_id):
    sent = []

    class Client:
        def gcode(self, script):
            sent.append(script)

    make_user("gcode_operator", ROLE_OPERATOR)
    printer = Printer.objects.create(name=profile_id, profile_id=profile_id, host="127.0.0.1", port=7125)
    monkeypatch.setattr("printers.views.client_for", lambda p: Client())
    login(client, "gcode_operator")

    html = client.get(reverse("printer_detail", args=[printer.pk])).content.decode()
    assert "G-code команды принтера" in html
    response = client.post(reverse("printer_gcode", args=[printer.pk]), {"script": "M105"}, follow=True)

    assert response.status_code == 200
    assert sent == ["M105"]
    assert AuditLog.objects.filter(action="gcode_send", target=profile_id).exists()


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
def test_dashboard_attention_shows_printing_with_warning_message(client):
    """Печать идёт, но принтер предупредил (например, датчик нити) — попадает в «Требует
    внимания», хотя статус остаётся «printing» (саму печать мы не останавливаем)."""
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer()
    write_state(
        p.pk,
        {
            "status": base.PRINTING,
            "message": "Датчик нити: пластик не подаётся.",
            "temps": {},
            "job": {"filename": "a.gcode", "progress": 0.5},
            "updated": time.time(),
        },
    )
    login(client, "sergey")
    html = client.get(reverse("printer_tiles")).content.decode()
    assert "Датчик нити" in html


@pytest.mark.django_db
def test_k1_self_test_is_visible_without_operator_attention(client):
    make_user("k1_self_test_operator", ROLE_OPERATOR)
    printer = Printer.objects.create(name="K1-self-test", profile_id="creality_k1", host="127.0.0.1", port=9999)
    write_state(
        printer.pk,
        {"status": base.SELF_TEST, "message": "Самопроверка принтера", "temps": {}, "job": None, "updated": time.time()},
    )
    login(client, "k1_self_test_operator")

    response = client.get("/")

    assert "Самопроверка принтера" in response.content.decode()
    assert response.context["attention"] == []


@pytest.mark.django_db
def test_dashboard_shows_all_fault_messages_for_moonraker_and_creality(client):
    make_user("sergey", ROLE_OPERATOR)
    moonraker = make_printer()
    creality = Printer.objects.create(name="K1-01", profile_id="creality_k1", host="127.0.0.1", port=9999)
    ignored_notice = '// Unknown command:"HARMONIC_NO_ALIGN"'
    moonraker_faults = ["Датчик нити: пластик не подаётся.", "Moonraker second warning is retained", ignored_notice]
    creality_fault = "Creality fault: " + "detailed message " * 20
    write_state(
        moonraker.pk,
        {
            "status": base.PRINTING,
            "message": moonraker_faults[0],
            "faults": moonraker_faults,
            "console": [ignored_notice],
            "filament": {"present": True, "enabled": False},
            "temps": {},
            "job": {"filename": "a.gcode", "progress": 0.5},
            "updated": time.time(),
        },
    )
    write_state(
        creality.pk,
        {
            "status": base.ERROR,
            "message": creality_fault,
            "faults": [creality_fault, ignored_notice],
            "temps": {},
            "job": None,
            "updated": time.time(),
        },
    )
    login(client, "sergey")
    html = client.get(reverse("printer_tiles")).content.decode()
    assert all(fault in html for fault in moonraker_faults[:2])
    assert creality_fault in html
    assert ignored_notice not in html
    assert "Пластик: есть" in html
    assert "нет данных от датчика" in html
    error_filter = client.get(reverse("printer_tiles") + "?f=error").content.decode()
    assert moonraker_faults[0] in error_filter


@pytest.mark.django_db
def test_fault_messages_stay_until_user_acknowledges_after_restart(client):
    user = make_user("sergey", ROLE_OPERATOR)
    printer = make_printer()
    fault_messages = ["! Communication timeout during homing probe", "!! SDCARD_RESET_FILE cannot be run from the sdcard"]

    write_state(
        printer.pk,
        {"status": base.ERROR, "message": fault_messages[0], "faults": fault_messages, "temps": {}, "job": None},
    )
    write_state(printer.pk, {"status": base.READY, "message": "", "faults": [], "temps": {}, "job": None})
    assert view_state(printer, read_state(printer.pk))["faults"] == fault_messages

    login(client, "sergey")
    response = client.post(reverse("printer_faults_acknowledge", args=[printer.pk]))
    assert response.status_code == 302
    assert list(printer.fault_events.filter(acknowledged_by=user).values_list("message", flat=True)) == fault_messages
    assert view_state(printer, read_state(printer.pk))["faults"] == []

    # A continued active report stays acknowledged; a later recurrence after it clears is new.
    write_state(printer.pk, {"status": base.ERROR, "faults": [fault_messages[0]], "temps": {}, "job": None})
    assert view_state(printer, read_state(printer.pk))["faults"] == [fault_messages[0]]


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


@pytest.mark.django_db(transaction=True)
def test_plate_cleared_records_quality_and_defect_reason(client, fake):
    """«Снял изделие» теперь — не просто снятие флага: всплывающее окно может отметить брак
    с причиной, записывается на последнее завершённое задание без оценки."""
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port, awaiting_removal=True)
    job = PrintJob.objects.create(
        printer=p,
        external_id="J1",
        filename="part.gcode",
        status=PrintJob.Status.COMPLETED,
        started_at=timezone.now(),
        ended_at=timezone.now(),
    )
    login(client, "sergey")
    client.post(
        reverse("printer_cleared", args=[p.pk]),
        {"quality": "defect", "defect_reason": "filament_tangle", "defect_note": "запуталась нить на катушке"},
    )
    p.refresh_from_db()
    job.refresh_from_db()
    assert p.awaiting_removal is False
    assert job.quality == "defect"
    assert job.defect_reason == "filament_tangle"
    assert job.defect_note == "запуталась нить на катушке"

    # вторая печать без брака — отмечается отдельно, прошлая запись не трогается
    p.awaiting_removal = True
    p.save(update_fields=["awaiting_removal"])
    job2 = PrintJob.objects.create(
        printer=p,
        external_id="J2",
        filename="part2.gcode",
        status=PrintJob.Status.COMPLETED,
        started_at=timezone.now(),
        ended_at=timezone.now(),
    )
    client.post(reverse("printer_cleared", args=[p.pk]), {"quality": "ok"})
    job.refresh_from_db()
    job2.refresh_from_db()
    assert job2.quality == "ok" and job2.defect_reason == ""
    assert job.quality == "defect"  # предыдущая оценка не перезаписана


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
def test_files_tab_delete_and_rename_buttons_and_views(client, fake):
    make_user("sergey", ROLE_OPERATOR)
    p = make_printer(port=fake.port)
    write_state(p.pk, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    login(client, "sergey")
    html = client.get(reverse("printer_detail", args=[p.pk]) + "?tab=files").content.decode()
    assert "Удалить" in html and "Переименовать" in html

    client.post(reverse("printer_file_delete", args=[p.pk]), {"path": "cube.gcode"})
    assert ("DELETE", "cube.gcode") in fake.calls
    assert AuditLog.objects.filter(action="file_delete", username="sergey").exists()

    client.post(reverse("printer_file_rename", args=[p.pk]), {"path": "cube.gcode", "new_name": "cube2.gcode"})
    assert ("move", {"source": "gcodes/cube.gcode", "dest": "gcodes/cube2.gcode"}) in fake.calls
    assert AuditLog.objects.filter(action="file_rename", username="sergey").exists()

    bad = client.post(
        reverse("printer_file_rename", args=[p.pk]), {"path": "cube.gcode", "new_name": "../cube.gcode"}, follow=True
    )
    assert "Укажите новое имя файла" in bad.content.decode()


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


@pytest.mark.django_db
def test_camera_frame_placeholder_when_page_unreachable(client):
    make_user("anna", ROLE_MANAGER)
    p = make_printer(profile_id="creality_k1c", host="127.0.0.1", port=7125)
    login(client, "anna")
    resp = client.get(reverse("printer_camera_frame", args=[p.pk]))
    assert resp.status_code == 200 and resp["Content-Type"] == "image/svg+xml"


@pytest.mark.django_db
def test_camera_frame_redirects_when_page_reachable(client, monkeypatch):
    make_user("anna", ROLE_MANAGER)
    p = make_printer(profile_id="creality_k1c", host="10.0.0.9", port=7125)
    login(client, "anna")
    monkeypatch.setattr("printers.views.page_reachable", lambda url, timeout=5: True)
    resp = client.get(reverse("printer_camera_frame", args=[p.pk]))
    assert resp.status_code == 302 and resp["Location"] == "http://10.0.0.9:8000"


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
    from printers.templatetags.printers_tags import basename

    assert basename(job["filename"]) == "part-A - 3_9h24m.gcode"
    assert basename("folder/a.gcode") == "a.gcode"
    assert basename("/usr/data/printer_data/gcodes/project_2_PETG.gcode") == "project_2_PETG.gcode"


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
    assert "START_PRINT" not in s["message"] and s["console"][-1].startswith("!!")
    assert any("Unknown command" in line for line in s["console"])
    assert not any("Unknown command" in fault for fault in s["faults"])
    assert any(fault.startswith("!!") for fault in s["faults"])


def test_klipper_timeouts_are_faults_but_unknown_macro_notices_stay_in_console():
    from printers.drivers.moonraker import decorate, keep_response

    lines = [
        "! Communication timeout during homing probe",
        "!! SDCARD_RESET_FILE cannot be run from the sdcard",
        '// Unknown command:"HARMONIC_NO_ALIGN"',
    ]
    assert all(keep_response(line) for line in lines)
    state = decorate(normalize({"webhooks": {"state": "ready"}}, profile=Q1), lines)
    assert state["faults"] == lines[:2]
    assert state["console"] == lines


def test_all_recent_klipper_fault_messages_are_retained():
    from printers.drivers.moonraker import decorate

    lines = [f"!! printer fault {n}" for n in range(25)]
    state = decorate(normalize({"webhooks": {"state": "ready"}}, profile=Q1), lines)
    assert len(state["console"]) == 20
    assert state["console"][0] == "!! printer fault 5"
    assert len(state["faults"]) == 25
    assert state["faults"][0] == "!! printer fault 0"


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
            await fake.notify("notify_gcode_response", ["!! old fault"])
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
    assert last["status"] == base.ERROR and "START_PRINT" not in last["message"]
    assert "!! old fault" not in last["console"]  # очищено на новом задании
    assert "!! old fault" not in last["faults"]
    assert any("Unknown command" in line for line in last["console"])
    assert not any("Unknown command" in fault for fault in last["faults"])


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
    assert "Свет в принтере: <b>выключен</b>" in html and "Включить свет" in html
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
