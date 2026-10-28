"""Этап 3: драйвер creality_native, профиль Creality K1 (WebSocket 9999, без Moonraker)."""

import asyncio

import pytest

from printers.drivers import base
from printers.drivers.creality_native import (
    CrealityNativeClient,
    CrealityNativeMonitor,
    derive_status,
    normalize,
    parse_firmware,
)
from printers.profiles import get_profile

from .fake_creality import FakeCreality, load_fixture

K1 = get_profile("creality_k1")
IDLE = load_fixture("creality_k1_idle.json")


@pytest.fixture
def fake():
    server = FakeCreality().start_thread()
    yield server
    server.stop_thread()


# --- профиль ------------------------------------------------------------------------------


def test_k1_profile():
    assert K1.driver == "creality_native" and K1.port == 9999 and K1.verified
    assert K1.capabilities == {"pause", "resume", "cancel", "light"}
    assert not K1.can("start_file") and not K1.can("upload")


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
