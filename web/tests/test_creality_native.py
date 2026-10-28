"""Этап 3: драйвер creality_native, профиль Creality K1 (WebSocket 9999, без Moonraker)."""

import asyncio

import pytest
from django.urls import reverse

from accounts.roles import ROLE_OPERATOR
from printers.drivers import base
from printers.drivers.creality_native import (
    CrealityNativeClient,
    CrealityNativeMonitor,
    derive_status,
    normalize,
    parse_firmware,
)
from printers.models import Printer
from printers.profiles import get_profile

from .fake_creality import FakeCreality, load_fixture
from .test_printers import login, make_user

K1 = get_profile("creality_k1")
IDLE = load_fixture("creality_k1_idle.json")


@pytest.fixture
def fake():
    server = FakeCreality().start_thread()
    yield server
    server.stop_thread()


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


# --- профиль ------------------------------------------------------------------------------


def test_k1_profile():
    assert K1.driver == "creality_native" and K1.port == 9999 and K1.verified
    assert K1.capabilities == {"pause", "resume", "cancel", "light", "start_file", "upload"}


# --- разбор версии прошивки --------------------------------------------------------------


def test_parse_firmware():
    assert parse_firmware(IDLE["modelVersion"]) == "1.3.5.22"
    assert parse_firmware(None) == ""


# --- карта состояний и нормализация -------------------------------------------------------


def test_derive_status_idle():
    assert derive_status(IDLE) == (base.READY, "")


def test_derive_status_printing_and_paused():
    printing = dict(IDLE, printFileName="a.gcode", printProgress=35, state=1)
    assert derive_status(printing) == (base.PRINTING, "")
    paused = dict(printing, state=5)
    assert derive_status(paused) == (base.PAUSED, "")
    homing = dict(printing, state=7)
    assert derive_status(homing) == (base.PRINTING, "")


def test_derive_status_error_and_self_test():
    err = dict(IDLE, err={"errcode": 5, "key": 1, "value": "термистор сопла"})
    assert derive_status(err) == (base.ERROR, "термистор сопла")
    testing = dict(IDLE, withSelfTest=50)
    status, _ = derive_status(testing)
    assert status == base.PRINTING


def test_normalize_offline():
    assert normalize({}, connected=False)["status"] == base.OFFLINE


def test_normalize_idle():
    s = normalize(IDLE, K1)
    assert s["status"] == base.READY and s["job"] is None
    assert s["light"] is False
    assert s["temps"]["nozzle"] == [25.1, 0.0]
    assert s["temps"]["chamber"] == [23.0, 0.0]


def test_normalize_printing_job():
    data = dict(
        IDLE,
        printFileName="a.gcode",
        printProgress=35,
        state=1,
        layer=42,
        TotalLayer=120,
        printJobTime=1800,
        printLeftTime=3600,
        usedMaterialLength=1250.5,
        lightSw=1,
        _gcode_meta={"filamentWeight": 42.7},
    )
    s = normalize(data, K1)
    assert s["status"] == base.PRINTING and s["light"] is True
    job = s["job"]
    assert job["progress"] == 0.35 and job["layer"] == 42 and job["total_layers"] == 120
    assert job["elapsed"] == 1800 and job["remaining"] == 3600
    assert job["filament_mm"] == 1250.5 and job["filament_g_total"] == 42.7


def test_normalize_remaining_falls_back_to_slicer_estimate():
    """Сразу после запуска printLeftTime у принтера ещё 0 (не успел посчитать) — ПО-27418,

    пока нет данных «вживую», берём оценку слайсера (timeCost) минус прошедшее время,
    а не показываем «осталось 0» (эта ошибка показывалась как «осталось 0 мин — закончится
    сейчас» сразу после нажатия «Печать»).
    """
    data = dict(
        IDLE,
        printFileName="a.gcode",
        printProgress=1,
        state=1,
        printJobTime=30,
        printLeftTime=0,
        _gcode_meta={"timeCost": 14644},
    )
    s = normalize(data, K1)
    assert s["job"]["remaining"] == 14644 - 30

    # Как только принтер сам посчитал остаток, он точнее оценки слайсера — используем его.
    live = dict(data, printLeftTime=9000)
    s_live = normalize(live, K1)
    assert s_live["job"]["remaining"] == 9000


# --- клиент (короткие WS-подключения) -----------------------------------------------------


def test_client_check(fake):
    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    info = c.check()
    assert info["klipper"] == "1.3.5.22" and info["hostname"] == "K1-3301"
    assert info["printer_state"] == "Готов"


def test_client_no_connection():
    with pytest.raises(base.DriverError, match="Нет связи"):
        CrealityNativeClient("127.0.0.1", 1, profile=K1, timeout=1).check()


def test_client_history_totals_unsupported(fake):
    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    with pytest.raises(base.DriverError, match="истори"):
        c.history_totals()


def test_client_commands(fake):
    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    c.pause()
    c.resume()
    c.cancel()
    c.set_light(True)
    c.set_light(False)
    assert ("set", {"pause": 1}) in fake.received
    assert ("set", {"pause": 0}) in fake.received
    assert ("set", {"stop": 1}) in fake.received
    assert ("set", {"lightSw": 1}) in fake.received
    assert ("set", {"lightSw": 0}) in fake.received


def test_client_list_files(fake):
    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    files = c.list_files()
    assert [f["path"].rsplit("/", 1)[-1] for f in files] == [
        "project_2_PETG_4h4m.gcode",
        "project_crealityK1_1_PETG_4h10m.gcode",
        "stackable_PETG_8h27m.gcode",
    ]
    assert files[0]["size"] == 10364876 and files[0]["modified"] == 1792916389


def test_client_read_head_tail_unsupported(fake):
    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    with pytest.raises(base.DriverError, match="не поддерживается"):
        c.read_head_tail("a.gcode")


def test_client_start(fake):
    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    c.start("/usr/data/printer_data/gcodes/a.gcode")
    assert ("set", {"opGcodeFile": "printprt:/usr/data/printer_data/gcodes/a.gcode"}) in fake.received


def test_client_upload(fake):
    import io

    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    c.upload_port = fake.port
    sent = []
    c.upload(io.BytesIO(b"G28\n"), "test.gcode", progress=lambda s, t: sent.append((s, t)))
    assert fake.uploads == [{"filename": "test.gcode", "size": 4}]
    assert sent and sent[-1][0] == sent[-1][1]  # последний вызов — «отправлено всё»


def test_client_upload_and_start(fake):
    import io

    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    c.upload_port = fake.port
    c.upload(io.BytesIO(b"G28\n"), "test.gcode", start=True)
    assert fake.uploads == [{"filename": "test.gcode", "size": 4}]
    started = [p for m, p in fake.received if m == "set" and "opGcodeFile" in p]
    assert started == [{"opGcodeFile": "printprt:/usr/data/printer_data/gcodes/test.gcode"}]


def test_client_thumbnail(fake):
    fake.thumbnails["a.png"] = b"\x89PNG\r\n\x1a\nfake"
    c = CrealityNativeClient("127.0.0.1", fake.port, profile=K1, timeout=5)
    c.upload_port = fake.port
    assert c.thumbnail("a.gcode") == b"\x89PNG\r\n\x1a\nfake"
    assert c.thumbnail("missing.gcode") is None


# --- монитор (постоянное WebSocket-соединение) ----------------------------------------------


def test_monitor_tracks_job_completion():
    async def scenario():
        import aiohttp

        fake = FakeCreality()
        await fake.start_async()
        states, jobs = [], []

        async def on_state(s):
            states.append(s)

        async def on_job(j, live):
            jobs.append((j["status"], live, j["filament_mm"]))

        async with aiohttp.ClientSession() as session:
            mon = CrealityNativeMonitor("127.0.0.1", fake.port, profile=K1, session=session)
            task = asyncio.create_task(mon.run(on_state, on_job))
            for _ in range(50):
                if states:
                    break
                await asyncio.sleep(0.05)
            await fake.push(
                {
                    "printFileName": "a.gcode",
                    "printStartTime": 1000,
                    "state": 1,
                    "printProgress": 35,
                    "usedMaterialLength": 500.0,
                }
            )
            await asyncio.sleep(0.1)
            # прогресс 100 сам по себе уже переводит статус в «готов» (см. derive_status) —
            # задание считается завершённым сразу на этом кадре, печать не ждёт.
            await fake.push({"printProgress": 100, "usedMaterialLength": 1400.0, "printJobTime": 7200})
            await asyncio.sleep(0.2)
            await fake.close_clients()
            with pytest.raises(Exception):  # noqa: B017 — обрыв соединения приводит к исключению
                await asyncio.wait_for(task, 3)
        await fake.stop_async()
        return states, jobs

    states, jobs = asyncio.run(scenario())
    assert states[0]["status"] == base.READY  # начальный кадр — исходный fixture (idle)
    printing = [s for s in states if s["status"] == base.PRINTING]
    assert printing and printing[-1]["job"]["progress"] == 0.35
    assert jobs == [("completed", True, 1400.0)]


def test_monitor_job_completed_despite_telemetry_reset_on_finish():
    """Принтер может обнулить printJobTime/usedMaterialLength в том же кадре, где прогресс

    доходит до 100 (готовится к следующему заданию) — ещё до того, как пропадёт
    printFileName. Раньше это писало в историю время печати и расход «0», из-за чего
    наработка не росла. Берём максимум по ходу печати, а не значения из кадра обнуления.
    """

    async def scenario():
        import aiohttp

        fake = FakeCreality()
        await fake.start_async()
        states, jobs = [], []

        async def on_state(s):
            states.append(s)

        async def on_job(j, live):
            jobs.append((j["status"], live, j["print_duration"], j["filament_mm"]))

        async with aiohttp.ClientSession() as session:
            mon = CrealityNativeMonitor("127.0.0.1", fake.port, profile=K1, session=session)
            task = asyncio.create_task(mon.run(on_state, on_job))
            for _ in range(50):
                if states:
                    break
                await asyncio.sleep(0.05)
            await fake.push(
                {
                    "printFileName": "a.gcode",
                    "printStartTime": 1000,
                    "state": 1,
                    "printProgress": 90,
                    "printJobTime": 6500,
                    "usedMaterialLength": 1300.0,
                }
            )
            await asyncio.sleep(0.1)
            # Прогресс доходит до 100 (printFileName пока не пропал), но принтер в этом же
            # кадре уже обнулил счётчики времени и расхода — готовится к следующему заданию.
            await fake.push({"printProgress": 100, "printJobTime": 0, "usedMaterialLength": 0.0})
            await asyncio.sleep(0.1)
            # Следом принтер уходит в простой — новых заданий это уже не должно создавать.
            await fake.push({"printFileName": "", "printProgress": 0, "state": 0})
            await asyncio.sleep(0.2)
            await fake.close_clients()
            with pytest.raises(Exception):  # noqa: B017 — обрыв соединения приводит к исключению
                await asyncio.wait_for(task, 3)
        await fake.stop_async()
        return jobs

    jobs = asyncio.run(scenario())
    assert jobs == [("completed", True, 6500.0, 1300.0)]


# --- веб: вкладка «Файлы» и превью -----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_files_tab_shows_thumbnails_and_print_button(client, fake):
    import time

    from printers.drivers import base
    from printers.state import write_state

    make_user("sergey", ROLE_OPERATOR)
    p = Printer.objects.create(name="K1-01", profile_id="creality_k1", host="127.0.0.1", port=fake.port)
    write_state(p.pk, {"status": base.READY, "temps": {}, "job": None, "updated": time.time()})
    login(client, "sergey")
    html = client.get(reverse("printer_detail", args=[p.pk]) + "?tab=files").content.decode()
    assert "project_2_PETG_4h4m.gcode" in html and "Печать" in html
    full_path = "/usr/data/printer_data/gcodes/project_2_PETG_4h4m.gcode"
    assert reverse("printer_file_thumbnail", args=[p.pk, full_path]) in html


@pytest.mark.django_db(transaction=True)
def test_file_thumbnail_view(client, fake):
    fake.thumbnails["project_2_PETG_4h4m.png"] = b"\x89PNG\r\n\x1a\nfake"
    make_user("sergey", ROLE_OPERATOR)
    p = Printer.objects.create(name="K1-01", profile_id="creality_k1", host="127.0.0.1", port=fake.port)
    login(client, "sergey")
    # client_for() использует порт из Printer (fake.port) для WS, но HTTP у этого драйвера
    # всегда на upload_port (в проде — 80); на поддельном сервере оба на одном порту, поэтому
    # подменяем upload_port там, где его читает код запроса, — через monkeypatch класса.
    from printers.drivers.creality_native import CrealityNativeClient

    CrealityNativeClient.upload_port = fake.port
    try:
        resp = client.get(reverse("printer_file_thumbnail", args=[p.pk, "project_2_PETG_4h4m.gcode"]))
        assert resp.status_code == 200 and resp["Content-Type"] == "image/png"
        assert resp.content == b"\x89PNG\r\n\x1a\nfake"
        missing = client.get(reverse("printer_file_thumbnail", args=[p.pk, "missing.gcode"]))
        assert missing.status_code == 404
    finally:
        CrealityNativeClient.upload_port = 80
