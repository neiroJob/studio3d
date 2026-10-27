#!/usr/bin/env python3
"""Запись ответов Moonraker для тестов драйвера. Только чтение: принтеру ничего не отправляется.

    python3 tools/moonraker_dump.py <IP принтера> [порт] > q1pro_idle.json
"""

import json
import sys
import time
import urllib.parse
import urllib.request

host = sys.argv[1]
port = sys.argv[2] if len(sys.argv) > 2 else "7125"
base = f"http://{host}:{port}"


def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


out = {"_recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "_base": f"http://<printer>:{port}"}
for path in [
    "/server/info", "/printer/info", "/server/webcams/list", "/server/history/totals",
    "/server/history/list?limit=3&order=desc", "/server/files/roots", "/printer/objects/list",
]:
    out[path] = get(path)

objects = out["/printer/objects/list"].get("result", {}).get("objects", [])
skip = ("configfile", "gcode_macro ")
wanted = [o for o in objects if not o.startswith(skip)]
query = "&".join(urllib.parse.quote(o) for o in wanted)
out["/printer/objects/query"] = get("/printer/objects/query?" + query)

files = get("/server/files/list?root=gcodes")
items = sorted(files.get("result", []), key=lambda f: f.get("modified", 0), reverse=True)
out["/server/files/list (5 newest)"] = items[:5]
if items:
    name = items[0]["path"]
    out["/server/files/metadata"] = get("/server/files/metadata?filename=" + urllib.parse.quote(name))

for cam in (f"http://{host}:8080/?action=snapshot", f"http://{host}/webcam/?action=snapshot"):
    try:
        with urllib.request.urlopen(cam, timeout=5) as r:
            out["snapshot " + cam.replace(host, "<printer>")] = {
                "status": r.status, "content_type": r.headers.get("Content-Type"), "bytes": len(r.read())}
    except Exception as e:  # noqa: BLE001
        out["snapshot " + cam.replace(host, "<printer>")] = {"_error": str(e)}

json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
