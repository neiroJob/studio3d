"""Драйвер нативного протокола Creality (K1, K1C ранних ревизий): WebSocket 9999, без Moonraker.

У принтера нет HTTP API для команд — всё идёт через один WebSocket: `{"method":"get"|"set","params":{...}}`.
Принтер сам, без запроса, шлёт частичные обновления состояния сразу после подключения.

Протокол не документирован Creality. Поля и команды собраны по записи реального K1 (память проекта,
printers.md) и по открытому проекту-интеграции Home Assistant для принтеров Creality
(github.com/3dg1luk43/ha_creality_ws, const.py/ws_client.py/coordinator.py/utils.py) — там же разобрана
карта кодов `state`/`deviceState`/`err`. Команды pause/resume/stop/lightSw проверены на живом K1
(2026-10-28, прошивка 1.3.5.22). Нет подтверждённой команды запуска файла с принтера и загрузки файла —
поэтому `start_file` и `upload` в профиле K1 не заявлены.
"""

import asyncio
import json
import re
import time

from . import base

SUBPROTOCOL_HINT = None  # принтер отвечает и без Sec-WebSocket-Protocol: wsslicer

VERSION_RE = re.compile(r"DWIN sw ver:([^;]+)")


def parse_firmware(model_version):
    m = VERSION_RE.search(model_version or "")
    return m.group(1).strip() if m else (model_version or "")


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def derive_status(data):
    """Код состояния единой модели по телеметрии K1. Карта кодов — ha_creality_ws/utils.py."""
    err = data.get("err") or {}
    if _float(err.get("errcode"), 0) != 0:
        return base.ERROR, err.get("value") or "Ошибка принтера"

    self_test = data.get("withSelfTest")
    if self_test is not None and 1 <= _float(self_test, 0) <= 99:
        return base.PRINTING, "Самопроверка принтера"

    filename = data.get("printFileName") or ""
    state = data.get("state")
    progress = _float(data.get("printProgress"), -1)

    # Упрощение по сравнению с ha_creality_ws: та сторона сглаживает дребезг прогресса
    # 99↔100 в последние секунды печати (NOTIFY_END_CONFIRM_SECS). Здесь это не сделано —
    # возможна секундная вспышка «готов» перед завершением задания.
    if filename and progress < 100:
        if state == 5:
            return base.PAUSED, ""
        if state == 1 or state in (0, 7):  # 0/7 — парковка, нагрев, выход после отмены: тоже занят
            return base.PRINTING, ""
    return base.READY, ""


def normalize(data, profile=None, connected=True, now=None):
    now = now or time.time()
    if not connected or not data:
        return base.offline_state(updated=now)

    status, message = derive_status(data)
    temps = {
        "nozzle": [round(_float(data.get("nozzleTemp")), 1), round(_float(data.get("targetNozzleTemp")), 1)],
        "bed": [round(_float(data.get("bedTemp0")), 1), round(_float(data.get("targetBedTemp0")), 1)],
        "chamber": (
            [round(_float(data.get("boxTemp")), 1), round(_float(data.get("targetBoxTemp")), 1)]
            if data.get("boxTemp") is not None
            else None
        ),
    }

    job = None
    if status in (base.PRINTING, base.PAUSED):
        progress = max(_float(data.get("printProgress")), 0) / 100
        elapsed = _float(data.get("printJobTime"))
        remaining = data.get("printLeftTime")
        meta = data.get("_gcode_meta") or {}
        job = {
            "filename": data.get("printFileName") or "",
            "progress": round(progress, 4),
            "layer": data.get("layer") or None,
            "total_layers": data.get("TotalLayer") or None,
            "elapsed": round(elapsed),
            "remaining": round(_float(remaining)) if remaining is not None else None,
            "filament_mm": round(_float(data.get("usedMaterialLength")), 1),
            "filament_g_total": meta.get("filamentWeight"),
        }

    light = _float(data.get("lightSw"), 0) > 0 if "lightSw" in data else None
    return {
        "status": status,
        "message": message,
        "temps": temps,
        "job": job,
        "light": light,
        "updated": now,
    }


class _ShortSession:
    """Короткое WS-подключение для одной команды/запроса из синхронной вьюхи."""

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout

    async def run(self, send=None, wait_for=None, drain=2):
        import aiohttp

        url = f"ws://{self.host}:{self.port}"
        collected = {}
        try:
            async with aiohttp.ClientSession() as session, session.ws_connect(
                url, timeout=self.timeout, heartbeat=None
            ) as ws:
                if send is not None:
                    await ws.send_str(json.dumps(send, separators=(",", ":")))
                deadline = asyncio.get_event_loop().time() + self.timeout
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        msg = await asyncio.wait_for(
                            ws.receive(), timeout=max(deadline - asyncio.get_event_loop().time(), 0.1)
                        )
                    except asyncio.TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    if msg.data == "ok":
                        continue
                    try:
                        frame = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(frame, dict) and frame.get("ModeCode") == "heart_beat":
                        await ws.send_str("ok")
                        continue
                    if isinstance(frame, dict):
                        collected.update(frame)
                        if wait_for and wait_for in frame:
                            return collected
                        drain -= 1
                        if drain <= 0 and not wait_for:
                            return collected
        except Exception as e:  # noqa: BLE001 — любая ошибка соединения
            raise base.DriverError("Нет связи с принтером: проверьте IP, порт и что принтер включён") from e
        if wait_for and wait_for not in collected:
            raise base.DriverError("Принтер не ответил на запрос вовремя")
        return collected


class CrealityNativeClient(base.BaseClient):
    def _session(self):
        return _ShortSession(self.host, self.port, self.timeout)

    def _run(self, coro):
        return asyncio.run(coro)

    def _set(self, **params):
        self._run(self._session().run(send={"method": "set", "params": params}))

    # --- проверка связи ---

    def check(self):
        started = time.monotonic()
        data = self._run(self._session().run(drain=3))
        latency_ms = round((time.monotonic() - started) * 1000)
        status, _ = derive_status(data)
        return {
            "latency_ms": latency_ms,
            "moonraker": "",
            "protocol": "Creality (WebSocket 9999)",
            "klipper": parse_firmware(data.get("modelVersion")) or data.get("model", ""),
            "hostname": data.get("hostname", ""),
            "camera": "",
            "camera_ok": False,
            "klippy_state": "",
            "printer_state": base.STATUS_LABELS.get(status, status),
        }

    def history_totals(self):
        raise base.DriverError("У этой модели нет истории печати на самом принтере")

    # --- команды ---

    def pause(self):
        self._set(pause=1)

    def resume(self):
        self._set(pause=0)

    def cancel(self):
        self._set(stop=1)

    def heaters_off(self):
        self._run(self._session().run(send={"method": "set", "params": {"gcodeCmd": "M104 S0"}}))
        self._set(gcodeCmd="M140 S0")

    def set_light(self, on):
        self._set(lightSw=1 if on else 0)

    # --- файлы ---

    def list_files(self):
        data = self._run(
            self._session().run(
                send={"method": "get", "params": {"reqGcodeFile": 1}},
                wait_for="retGcodeFileInfo2",
                drain=0,
            )
        )
        entries = data.get("retGcodeFileInfo2") or []
        files = [
            {
                "path": e.get("path") or e.get("name") or "",
                "size": int(e.get("file_size") or 0),
                "modified": float(e.get("create_time") or 0),
            }
            for e in entries
            if isinstance(e, dict) and (e.get("path") or e.get("name"))
        ]
        return sorted(files, key=lambda f: f["modified"], reverse=True)


class CrealityNativeMonitor(base.BaseMonitor):
    """Постоянное соединение: принтер сам шлёт частичные обновления состояния.

    История заданий принтер не хранит (память проекта, printers.md), поэтому задания в БД
    пишутся только по переходам, увиденным вживую с момента подключения — `history_since`
    не используется (ничем не наполнить).
    """

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session
        self.data = {}
        self._job = None  # активное задание: {external_id, filename, start_time}
        self._gcode_meta = {}
        self._gcode_meta_for = None

    async def run(self, on_state, on_job, history_since=0.0):
        url = f"ws://{self.host}:{self.port}"
        async with self.session.ws_connect(url, heartbeat=None) as ws:
            async for msg in ws:
                import aiohttp

                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        return
                    continue
                if msg.data == "ok":
                    continue
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("ModeCode") == "heart_beat":
                    await ws.send_str("ok")
                    continue

                self.data.update(frame)
                entries = self.data.pop("retGcodeFileInfo2", None)
                if entries is not None:
                    self.handle_retgcodefile(entries, self.data.get("printFileName") or "")
                await self._refresh_gcode_meta(ws)
                state = normalize({**self.data, "_gcode_meta": self._gcode_meta}, self.profile)
                await self._track_job(state, on_job)
                await on_state(state)
        raise ConnectionError("Соединение закрыто принтером")

    async def _refresh_gcode_meta(self, ws):
        filename = self.data.get("printFileName") or ""
        if not filename or filename == self._gcode_meta_for:
            return
        self._gcode_meta_for = filename
        self._gcode_meta = {}
        try:
            await ws.send_str(json.dumps({"method": "get", "params": {"reqGcodeFile": 1}}))
        except Exception:  # noqa: BLE001 — метаданные необязательны
            pass

    async def _track_job(self, state, on_job):
        job = state.get("job")
        if job and job["filename"]:
            key = f"{job['filename']}@{round(self.data.get('printStartTime') or 0)}"
            if self._job is None or self._job["external_id"] != key:
                self._job = {
                    "external_id": key,
                    "filename": job["filename"],
                    "start_time": self.data.get("printStartTime") or time.time(),
                }
            return
        if self._job is None:
            return
        # Задание было активным и перестало им быть — печать закончилась (сами или отменой).
        ended = self._job
        self._job = None
        progress = _float(self.data.get("printProgress")) / 100
        status, _ = derive_status(self.data)
        await on_job(
            {
                "external_id": ended["external_id"],
                "filename": ended["filename"],
                "status": "completed" if progress >= 0.999 and status != base.ERROR else "cancelled",
                "start_time": ended["start_time"],
                "end_time": time.time(),
                "print_duration": _float(self.data.get("printJobTime")),
                "total_duration": _float(self.data.get("printJobTime")),
                "filament_mm": _float(self.data.get("usedMaterialLength")),
                "filament_g": self._gcode_meta.get("filamentWeight"),
            },
            live=True,
        )

    def handle_retgcodefile(self, entries, filename):
        """Выделить метаданные текущего файла из ответа `retGcodeFileInfo2` (как в ha_creality_ws)."""
        base_name = filename.rsplit("/", 1)[-1]
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("path") == filename or e.get("name") == base_name:
                weight = e.get("filamentWeight")
                self._gcode_meta = {"filamentWeight": float(weight) if weight not in (None, "") else None}
                return
