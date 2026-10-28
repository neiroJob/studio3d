"""Драйвер Moonraker (Klipper): QIDI Q1 Pro, Plus 4, Q2; Elegoo Neptune 4 Max; Creality K1C ревизии 2025.

API: https://moonraker.readthedocs.io — HTTP для команд и файлов, WebSocket (JSON-RPC) для состояния.
Особенности, найденные на реальных принтерах, — в памяти проекта (printers.md, lessonslearned № 7):
имена объектов Klipper с пробелом в query-строке нужно URL-кодировать.
"""

import asyncio
import collections
import itertools
import json
import re
import time
import urllib.parse

import requests

from . import base

STATUS_FIELDS = {
    "webhooks": ["state", "state_message"],
    "print_stats": [
        "state",
        "filename",
        "print_duration",
        "total_duration",
        "filament_used",
        "message",
        "info",
    ],
    "virtual_sdcard": ["progress", "is_active", "file_position"],
    "extruder": ["temperature", "target"],
    "heater_bed": ["temperature", "target"],
}
TEMP_FIELDS = ["temperature", "target"]


def subscribe_objects(profile):
    objects = dict(STATUS_FIELDS)
    chamber = (profile.objects.get("chamber") if profile else None) or ""
    if chamber:
        objects[chamber] = TEMP_FIELDS
    if profile and profile.light_object:
        objects[profile.light_object] = ["value"]
    return objects


# Частые ошибки Klipper → пояснение по-русски. Исходный текст сохраняется в скобках.
KLIPPER_HINTS = [
    (
        "Must home axis first",
        "Оси не в исходном положении: G-code начал движение без парковки (G28). "
        "Проверьте стартовый G-code / PRINT_START в профиле слайсера",
    ),
    (
        "Move out of range",
        "Движение за пределы рабочей области: файл нарезан для другого принтера "
        "или неверно задан стол в слайсере",
    ),
    ("Extrude below minimum temp", "Попытка выдавить пластик при холодном сопле"),
    (
        "Heater extruder not heating at expected rate",
        "Сопло не нагревается: проверьте нагреватель и термистор",
    ),
    (
        "Heater heater_bed not heating at expected rate",
        "Стол не нагревается: проверьте нагреватель и термистор",
    ),
    ("Lost communication with MCU", "Потеряна связь с платой принтера"),
    ("Timer too close", "Плата принтера не успела обработать команды (Timer too close)"),
]


def explain(message):
    """Добавить пояснение к известной ошибке Klipper."""
    for needle, hint in KLIPPER_HINTS:
        if needle.lower() in (message or "").lower():
            return f"{hint} ({message})"
    return message


def _pair(obj):
    if not isinstance(obj, dict) or obj.get("temperature") is None:
        return None
    return [round(float(obj.get("temperature") or 0), 1), round(float(obj.get("target") or 0), 1)]


def normalize(status, meta=None, profile=None, connected=True, now=None):
    """Состояние Moonraker (слитые ответы subscribe/notify) → единая модель состояния."""
    now = now or time.time()
    if not connected:
        return base.offline_state(updated=now)
    meta = meta or {}
    webhooks = status.get("webhooks") or {}
    stats = status.get("print_stats") or {}
    chamber_name = (profile.objects.get("chamber") if profile else None) or ""

    temps = {
        "nozzle": _pair(status.get("extruder")),
        "bed": _pair(status.get("heater_bed")),
        "chamber": _pair(status.get(chamber_name)) if chamber_name else None,
    }

    klippy = webhooks.get("state", "ready")
    message = ""
    if klippy in ("shutdown", "error"):
        result = base.ERROR
        message = explain(webhooks.get("state_message") or "Klipper остановлен")
    elif klippy == "startup":
        return {**base.offline_state("Klipper запускается", now), "temps": temps}
    else:
        state = stats.get("state", "standby")
        result = {
            "printing": base.PRINTING,
            "paused": base.PAUSED,
            "error": base.ERROR,
        }.get(state, base.READY)
        if state == "error":
            message = explain(stats.get("message") or "Ошибка печати")

    job = None
    if result in (base.PRINTING, base.PAUSED) or (result == base.ERROR and stats.get("filename")):
        progress = float((status.get("virtual_sdcard") or {}).get("progress") or 0)
        elapsed = float(stats.get("print_duration") or 0)
        info = stats.get("info") or {}
        estimated = meta.get("estimated_time")
        if estimated:
            remaining = max(float(estimated) - elapsed, 0)
        elif progress > 0.01:
            remaining = elapsed / progress - elapsed
        else:
            remaining = None
        job = {
            "filename": stats.get("filename") or "",
            "progress": round(progress, 4),
            "layer": info.get("current_layer"),
            "total_layers": info.get("total_layer") or meta.get("layer_count"),
            "elapsed": round(elapsed),
            "remaining": round(remaining) if remaining is not None else None,
            "filament_mm": round(float(stats.get("filament_used") or 0), 1),
            "filament_g_total": meta.get("filament_weight_total"),
        }

    light = None
    if profile and profile.light_object and isinstance(status.get(profile.light_object), dict):
        light = float(status[profile.light_object].get("value") or 0) > 0
    return {"status": result, "message": message, "temps": temps, "job": job, "light": light, "updated": now}


def job_from_history(item):
    """Запись server/history → поля PrintJob."""
    meta = item.get("metadata") or {}
    filament_mm = float(item.get("filament_used") or 0)
    weight = meta.get("filament_weight_total")
    total_mm = meta.get("filament_total")
    grams = None
    if weight:
        if item.get("status") == "completed" or not total_mm:
            grams = float(weight)
        else:
            grams = float(weight) * min(filament_mm / float(total_mm), 1.0)
    return {
        "external_id": str(item.get("job_id")),
        "filename": item.get("filename") or "",
        "status": item.get("status") or "other",
        "start_time": float(item.get("start_time") or 0),
        "end_time": float(item["end_time"]) if item.get("end_time") else None,
        "print_duration": float(item.get("print_duration") or 0),
        "total_duration": float(item.get("total_duration") or 0),
        "filament_mm": filament_mm,
        "filament_g": round(grams, 2) if grams is not None else None,
    }


class MoonrakerClient(base.BaseClient):
    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def _headers(self):
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def _request(self, method, path, **kwargs):
        timeout = kwargs.pop("timeout", self.timeout)
        try:
            resp = requests.request(
                method, self.base_url + path, headers=self._headers(), timeout=timeout, **kwargs
            )
        except requests.Timeout as e:
            raise base.DriverError("Принтер не ответил вовремя") from e
        except requests.ConnectionError as e:
            raise base.DriverError("Нет связи с принтером: проверьте IP, порт и что принтер включён") from e
        if resp.status_code in (401, 403):
            raise base.DriverError("Принтер требует авторизацию: укажите API-ключ")
        try:
            data = resp.json()
        except ValueError as e:
            raise base.DriverError(f"Неожиданный ответ принтера (HTTP {resp.status_code})") from e
        if "error" in data:
            err = data["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            if text in ("Bad Request", "Internal Server Error", "Service Unavailable"):
                text = f"принтер отклонил команду ({text})"
            raise base.DriverError(f"Принтер вернул ошибку: {text}")
        return data.get("result")

    def get(self, path, **params):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return self._request("GET", path + query)

    def post(self, path, **params):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return self._request("POST", path + query)

    # --- проверка связи ---

    def check(self):
        started = time.monotonic()
        server = self.get("/server/info")
        latency_ms = round((time.monotonic() - started) * 1000)
        result = {
            "latency_ms": latency_ms,
            "moonraker": server.get("moonraker_version", "?"),
            "klippy_state": server.get("klippy_state", "?"),
            "klipper": "",
            "hostname": "",
            "camera": "",
            "camera_ok": False,
            "printer_state": "",
        }
        try:
            info = self.get("/printer/info")
            result["klipper"] = info.get("software_version", "")
            result["hostname"] = info.get("hostname", "")
        except base.DriverError:
            pass
        if result["klippy_state"] == "ready":
            try:
                q = self.query({"print_stats": ["state"]})
                result["printer_state"] = q.get("status", {}).get("print_stats", {}).get("state", "")
            except base.DriverError:
                pass
        snap = self.profile.snapshot_url_for(self.host) if self.profile else ""
        page = self.profile.page_url_for(self.host) if self.profile else ""
        if snap:
            result["camera"] = snap
            result["camera_ok"] = fetch_snapshot(snap, timeout=4) is not None
        elif page:
            result["camera"] = page
            result["camera_ok"] = page_reachable(page, timeout=4)
        return result

    def query(self, objects):
        # Имена вида «heater_generic chamber» обязательно кодируем (lessonslearned № 7).
        parts = []
        for name, fields in objects.items():
            key = urllib.parse.quote(name)
            parts.append(f"{key}={','.join(fields)}" if fields else key)
        return self._request("GET", "/printer/objects/query?" + "&".join(parts))

    def history_totals(self):
        totals = (self.get("/server/history/totals") or {}).get("job_totals") or {}
        return {"print_seconds": float(totals.get("total_print_time") or 0)}

    # --- команды ---

    def pause(self):
        self.post("/printer/print/pause")

    def resume(self):
        self.post("/printer/print/resume")

    def cancel(self):
        self.post("/printer/print/cancel")

    def start(self, filename):
        self.post("/printer/print/start", filename=filename)

    def gcode(self, script):
        self.post("/printer/gcode/script", script=script)

    def heaters_off(self):
        """Выключить все нагреватели (стандартная команда Klipper, в том числе нагреватель камеры)."""
        self.gcode("TURN_OFF_HEATERS")

    def set_light(self, on):
        if self.profile and self.profile.light_native_port:
            from . import creality_native

            port = self.profile.light_native_port
            creality_native.set_light_native(self.host, port, on, timeout=self.timeout)
            return
        if not self.profile or not self.profile.light_on:
            raise base.DriverError("У этой модели нет управления светом")
        self.gcode(self.profile.light_on if on else self.profile.light_off)

    # --- файлы ---

    def list_files(self):
        items = self.get("/server/files/list", root="gcodes") or []
        files = [
            {
                "path": f.get("path") or f.get("filename") or "",
                "size": int(f.get("size") or 0),
                "modified": float(f.get("modified") or 0),
            }
            for f in items
        ]
        # QIDI хранит служебные копии в скрытых папках (.cache — копия при печати с экрана,
        # .plr — восстановление после отключения питания). Пользователю их не показываем.
        files = [
            f for f in files if f["path"] and not any(part.startswith(".") for part in f["path"].split("/"))
        ]
        return sorted(files, key=lambda f: f["modified"], reverse=True)

    def read_head_tail(self, path):
        """Начало и конец файла на принтере (для проверки G-code), не скачивая его целиком."""
        from ..gcode_check import HEAD_BYTES, TAIL_BYTES

        url = self.base_url + "/server/files/gcodes/" + urllib.parse.quote(path)
        try:
            with requests.get(
                url,
                headers={**self._headers(), "Range": f"bytes=0-{HEAD_BYTES - 1}"},
                stream=True,
                timeout=self.timeout,
            ) as r:
                if r.status_code not in (200, 206):
                    raise base.DriverError(f"Не удалось прочитать файл (HTTP {r.status_code})")
                head = b""
                for chunk in r.iter_content(64 * 1024):
                    head += chunk
                    if len(head) >= HEAD_BYTES:
                        break
            tail = b""
            with requests.get(
                url,
                headers={**self._headers(), "Range": f"bytes=-{TAIL_BYTES}"},
                stream=True,
                timeout=self.timeout,
            ) as r:
                if r.status_code == 206:  # без поддержки Range весь файл не качаем
                    tail = r.content
        except requests.RequestException as e:
            raise base.DriverError("Не удалось прочитать файл с принтера") from e
        return head[:HEAD_BYTES].decode("utf-8", "replace"), tail.decode("utf-8", "replace")

    def file_metadata(self, filename):
        return self.get("/server/files/metadata", filename=filename) or {}

    def thumbnail(self, filename, timeout=5):
        """Превью модели из метаданных G-code (server/files/metadata → thumbnails[]).

        Слайсер кладёт превью рядом с файлом, в .thumbs/<имя>-<ШxВ>.png (relative_path —
        относительно папки самого G-code-файла, не относительно корня gcodes).
        """
        try:
            meta = self.file_metadata(filename)
        except base.DriverError:
            return None
        thumbs = meta.get("thumbnails") or []
        if not thumbs:
            return None
        smallest = min(thumbs, key=lambda t: t.get("width") or 0)
        rel = smallest.get("relative_path") or ""
        if not rel:
            return None
        directory = filename.rsplit("/", 1)[0] if "/" in filename else ""
        path = f"{directory}/{rel}" if directory else rel
        url = self.base_url + "/server/files/gcodes/" + urllib.parse.quote(path)
        try:
            resp = requests.get(url, headers=self._headers(), timeout=timeout)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        return resp.content

    def upload(self, fileobj, filename, start=False, progress=None):
        """Загрузка G-code. progress(sent, total) вызывается по мере отправки на принтер."""
        from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

        fields = {"root": "gcodes", "file": (filename, fileobj, "application/octet-stream")}
        if start:
            fields["print"] = "true"
        encoder = MultipartEncoder(fields=fields)
        total = encoder.len
        last = [0.0]

        def callback(monitor):
            now = time.monotonic()
            if progress and (now - last[0] > 0.3 or monitor.bytes_read >= total):
                last[0] = now
                progress(monitor.bytes_read, total)

        body = MultipartEncoderMonitor(encoder, callback)
        try:
            resp = requests.post(
                self.base_url + "/server/files/upload",
                data=body,
                headers={**self._headers(), "Content-Type": body.content_type},
                timeout=(self.timeout, 600),
            )
        except requests.Timeout as e:
            raise base.DriverError("Принтер не ответил вовремя при загрузке файла") from e
        except requests.ConnectionError as e:
            raise base.DriverError("Связь с принтером прервалась при загрузке файла") from e
        if resp.status_code >= 400:
            raise base.DriverError(f"Принтер не принял файл (HTTP {resp.status_code})")
        return resp.json().get("result")


def fetch_snapshot(url, timeout=5):
    """JPEG-снимок с камеры или None."""
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
        return None
    return resp.content


def page_reachable(url, timeout=5):
    """Страница камеры (например, WebRTC) отвечает, не скачивая её целиком."""
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.close()
    except requests.RequestException:
        return False
    return resp.status_code == 200


UNKNOWN_CMD_RE = re.compile(r'Unknown command:\s*"?([^"\s]+)"?', re.IGNORECASE)
SKIP_RESPONSES = ("ok", "B:", "T:", "// Klipper state")


def keep_response(line):
    """Ответы Klipper, которые стоит показать человеку: ошибки, предупреждения, сообщения макросов."""
    line = (line or "").strip()
    return bool(line) and not line.startswith(SKIP_RESPONSES)


def decorate(state, console):
    """Добавить к состоянию последние сообщения принтера и вывод о незнакомых командах."""
    lines = [c for c in console]
    state["console"] = lines[-8:]
    if state.get("status") == base.ERROR:
        unknown = list(dict.fromkeys(m.group(1) for c in lines for m in [UNKNOWN_CMD_RE.search(c)] if m))
        if unknown:
            message = (state.get("message") or "").rstrip()
            if message and not message.endswith("."):
                message += "."
            state["message"] = message + (
                f" Принтер не знает команд из файла: {', '.join(unknown)} — "
                "скорее всего, файл нарезан под другой принтер."
            )
    return state


class MoonrakerMonitor(base.BaseMonitor):
    """Постоянное соединение по WebSocket: подписка на состояние и события истории."""

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session
        self.status = {}
        self.meta = {}
        self._meta_for = None
        self._ids = itertools.count(1)
        self._pending = {}
        self.console = collections.deque(maxlen=20)

    @property
    def http(self):
        return f"http://{self.host}:{self.port}"

    def _headers(self):
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    async def _get(self, path, params=None):
        async with self.session.get(
            self.http + path, params=params, headers=self._headers(), timeout=10
        ) as r:
            data = await r.json(content_type=None)
            if "error" in data:
                raise base.DriverError(str(data["error"]))
            return data.get("result")

    async def history_since(self, since):
        """Задания, завершённые после since (unix time), по порядку."""
        jobs, start = [], 0
        while True:
            params = {"since": f"{since:.3f}", "order": "asc", "limit": "50", "start": str(start)}
            result = await self._get("/server/history/list", params) or {}
            page = result.get("jobs") or []
            jobs.extend(j for j in page if j.get("end_time") and j.get("status") != "in_progress")
            if len(page) < 50:
                return jobs
            start += 50

    async def _call(self, ws, method, params=None):
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await ws.send_str(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": msg_id})
        )
        return await asyncio.wait_for(fut, timeout=10)

    async def _refresh_meta(self):
        filename = (self.status.get("print_stats") or {}).get("filename") or ""
        if filename == self._meta_for:
            return
        self._meta_for = filename
        self.meta = {}
        if filename:
            try:
                self.meta = await self._get("/server/files/metadata", {"filename": filename}) or {}
            except Exception:  # noqa: BLE001 — метаданных может не быть, это не ошибка связи
                self.meta = {}

    async def run(self, on_state, on_job, history_since=0.0):
        import aiohttp

        for item in await self.history_since(history_since):
            await on_job(job_from_history(item), live=False)

        ws_url = f"ws://{self.host}:{self.port}/websocket"
        async with self.session.ws_connect(ws_url, headers=self._headers(), heartbeat=20) as ws:
            reader = asyncio.create_task(self._reader(ws, on_state, on_job))
            try:
                await self._subscribe(ws, on_state)
                await reader
            finally:
                reader.cancel()
                for fut in self._pending.values():
                    fut.cancel()
                self._pending.clear()
            if ws.exception():
                raise ws.exception()
        raise aiohttp.ClientConnectionError("Соединение закрыто принтером")

    async def _subscribe(self, ws, on_state):
        server = await self._call(ws, "server.info")
        if server.get("klippy_state") != "ready":
            self.status = {"webhooks": {"state": server.get("klippy_state", "startup"), "state_message": ""}}
            await on_state(decorate(normalize(self.status, profile=self.profile), self.console))
            return  # дождёмся notify_klippy_ready
        result = await self._call(
            ws, "printer.objects.subscribe", {"objects": subscribe_objects(self.profile)}
        )
        self.status = result.get("status", {})
        await self._refresh_meta()
        await on_state(decorate(normalize(self.status, self.meta, self.profile), self.console))

    async def _reader(self, ws, on_state, on_job):
        import aiohttp

        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    return
                continue
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self._pending:
                fut = self._pending.pop(data["id"])
                if "error" in data:
                    fut.set_exception(base.DriverError(str(data["error"])))
                else:
                    fut.set_result(data.get("result") or {})
                continue
            method = data.get("method")
            params = data.get("params") or []
            if method == "notify_status_update" and params:
                for name, diff in params[0].items():
                    self.status.setdefault(name, {}).update(diff)
                await self._refresh_meta()
                await on_state(decorate(normalize(self.status, self.meta, self.profile), self.console))
            elif method == "notify_gcode_response" and params:
                for line in str(params[0]).splitlines():
                    if line.startswith("File opened:"):
                        self.console.clear()  # новое задание — прошлые сообщения не нужны
                    if keep_response(line):
                        self.console.append(line.strip())
                await on_state(decorate(normalize(self.status, self.meta, self.profile), self.console))
            elif method == "notify_klippy_ready":
                asyncio.create_task(self._subscribe(ws, on_state))
            elif method in ("notify_klippy_shutdown", "notify_klippy_disconnected"):
                state = "shutdown" if method.endswith("shutdown") else "startup"
                self.status.setdefault("webhooks", {})["state"] = state
                await on_state(decorate(normalize(self.status, self.meta, self.profile), self.console))
            elif method == "notify_history_changed" and params:
                event = params[0]
                if event.get("action") == "finished" and event.get("job"):
                    await on_job(job_from_history(event["job"]), live=True)
