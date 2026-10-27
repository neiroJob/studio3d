"""Поддельный Moonraker для тестов: HTTP + WebSocket на localhost.

Ответы собраны по документации Moonraker и записям реального QIDI Q1 Pro (tests/fixtures).
"""

import asyncio
import json
import threading
from pathlib import Path

from aiohttp import web

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeMoonraker:
    def __init__(self, fixture="moonraker_q1pro_printing.json"):
        self.data = load_fixture(fixture)
        self.status = json.loads(json.dumps(self.data["status"]))
        self.calls = []
        self.uploads = []
        self.gcode_files = {}  # путь → bytes, для /server/files/gcodes/<путь>
        self.ws_clients = []
        self.loop = None
        self.port = None
        self._runner = None
        self._thread = None

    # --- HTTP ---

    def _ok(self, result):
        return web.json_response({"result": result})

    async def server_info(self, request):
        return self._ok(self.data["server_info"])

    async def printer_info(self, request):
        return self._ok(self.data["printer_info"])

    async def objects_query(self, request):
        self.calls.append(("query", request.raw_path.split("?", 1)[1]))
        wanted = {k: v for k, v in request.query.items()}
        status = {k: self.status.get(k, {}) for k in wanted}
        return self._ok({"eventtime": 1.0, "status": status})

    async def history_totals(self, request):
        return self._ok({"job_totals": self.data["history_totals"]})

    async def history_list(self, request):
        since = float(request.query.get("since", 0))
        jobs = [j for j in self.data["history"] if (j.get("end_time") or 0) > since]
        return self._ok({"count": len(jobs), "jobs": jobs})

    async def files_list(self, request):
        return self._ok(self.data["files"])

    async def metadata(self, request):
        return self._ok(self.data["metadata"])

    async def command(self, request):
        self.calls.append((request.path, dict(request.query)))
        return self._ok("ok")

    async def upload(self, request):
        form = await request.post()
        f = form["file"]
        self.uploads.append({"filename": f.filename, "size": len(f.file.read()), "print": form.get("print")})
        return web.json_response(
            {"result": {"item": {"path": f.filename}, "print_started": bool(form.get("print"))}}
        )

    async def gcode_file(self, request):
        data = self.gcode_files.get(request.match_info["path"])
        if data is None:
            return web.json_response({"error": {"code": 404, "message": "not found"}}, status=404)
        rng = request.headers.get("Range", "")
        if rng.startswith("bytes=-"):
            part = data[-int(rng[7:]) :]
        elif rng.startswith("bytes="):
            start, end = rng[6:].split("-")
            part = data[int(start) : int(end) + 1 if end else None]
        else:
            return web.Response(body=data)
        return web.Response(body=part, status=206)

    # --- WebSocket JSON-RPC ---

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.append(ws)
        async for msg in ws:
            data = json.loads(msg.data)
            method = data.get("method")
            if method == "server.info":
                result = self.data["server_info"]
            elif method == "printer.objects.subscribe":
                self.calls.append(("subscribe", sorted(data["params"]["objects"])))
                result = {"eventtime": 1.0, "status": self.status}
            else:
                result = {}
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": data["id"], "result": result}))
        return ws

    async def notify(self, method, params):
        for ws in self.ws_clients:
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    async def close_clients(self):
        for ws in self.ws_clients:
            await ws.close()

    def app(self):
        app = web.Application(client_max_size=100 * 1024 * 1024)
        app.router.add_get("/server/info", self.server_info)
        app.router.add_get("/printer/info", self.printer_info)
        app.router.add_get("/printer/objects/query", self.objects_query)
        app.router.add_get("/server/history/totals", self.history_totals)
        app.router.add_get("/server/history/list", self.history_list)
        app.router.add_get("/server/files/list", self.files_list)
        app.router.add_get("/server/files/metadata", self.metadata)
        app.router.add_post("/server/files/upload", self.upload)
        app.router.add_get("/server/files/gcodes/{path:.*}", self.gcode_file)
        for name in ("pause", "resume", "cancel", "start"):
            app.router.add_post(f"/printer/print/{name}", self.command)
        app.router.add_post("/printer/gcode/script", self.command)
        app.router.add_get("/websocket", self.websocket)
        return app

    # --- запуск в отдельном потоке (для синхронных тестов) ---

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
