"""Поддельный принтер Creality (нативный протокол, WebSocket 9999) для тестов.

Телеметрия и список файлов — запись реального Creality K1 (память проекта, printers.md;
web/tests/fixtures/creality_k1_idle.json, creality_k1_gcode_files.json), 2026-10-28.
"""

import asyncio
import json
import threading
from pathlib import Path

from aiohttp import web

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeCreality:
    def __init__(self, fixture="creality_k1_idle.json"):
        self.state = load_fixture(fixture)
        self.gcode_files = load_fixture("creality_k1_gcode_files.json")
        self.received = []  # params из каждой команды set/get
        self.ws_clients = []
        self.loop = None
        self.port = None
        self._runner = None
        self._thread = None

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.append(ws)
        await ws.send_str(json.dumps(self.state))
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            if msg.data == "ok":
                continue
            data = json.loads(msg.data)
            method, params = data.get("method"), data.get("params") or {}
            self.received.append((method, params))
            if method == "set":
                self.state.update(params)
                await ws.send_str(json.dumps(params))
            elif method == "get" and "reqGcodeFile" in params:
                await ws.send_str(json.dumps({"retGcodeFileInfo2": self.gcode_files}))
        return ws

    async def close_clients(self):
        for ws in self.ws_clients:
            await ws.close()

    async def push(self, patch):
        """Отправить подключённым клиентам частичное обновление, как это делает принтер."""
        self.state.update(patch)
        for ws in self.ws_clients:
            await ws.send_str(json.dumps(patch))

    def app(self):
        app = web.Application()
        app.router.add_get("/", self.websocket)
        return app

    async def start_async(self):
        self._runner = web.AppRunner(self.app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop_async(self):
        await self._runner.cleanup()

    def start_thread(self):
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            self.loop.run_until_complete(self.start_async())
            ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(5)
        return self

    def stop_thread(self):
        asyncio.run_coroutine_threadsafe(self.stop_async(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
