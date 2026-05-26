from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .dashboard import render_dashboard
from .store import Store
from .util import json_loads


def latest_queue(store: Store) -> dict:
    snap = store.row("select * from queue_snapshots order by created_at desc limit 1")
    if not snap:
        return {"items": []}
    return {
        "id": snap["id"],
        "created_at": snap["created_at"],
        "reason": snap["reason"],
        "capacity": snap["capacity"],
        "active_jobs": snap["active_jobs"],
        "items": json_loads(snap["items_json"], []),
    }


def status_payload(store: Store) -> dict:
    running = [
        dict(row)
        for row in store.rows("select * from jobs where status='running' order by created_at desc limit 20")
    ]
    priorities = [
        dict(row)
        for row in store.rows("select * from project_priorities where status='active' order by priority desc, updated_at desc limit 20")
    ]
    recent = [
        dict(row)
        for row in store.rows("select * from jobs where status!='running' order by updated_at desc limit 20")
    ]
    return {
        "running": running,
        "priorities": priorities,
        "recent": recent,
        "queue": latest_queue(store),
    }


class Handler(BaseHTTPRequestHandler):
    store = Store()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_html()
        elif path == "/api/status":
            self._send_json(status_payload(self.store))
        elif path == "/api/queue":
            self._send_json(latest_queue(self.store))
        elif path == "/api/dashboard":
            self._send_text(render_dashboard(self.store, limit=12))
        elif path == "/events":
            self._send_events()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        return

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        self._send_text(
            """<!doctype html><meta charset=utf-8><title>AutoCode</title>
<style>body{font:14px ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;background:#101214;color:#e8ecef}pre{white-space:pre-wrap;margin:16px}</style>
<pre id=out>connecting...</pre><script>
const out=document.getElementById('out');
const es=new EventSource('/events');
es.onmessage=e=>{out.textContent=JSON.parse(e.data).dashboard};
es.onerror=()=>{out.textContent+='\\n[event stream disconnected]'};
</script>"""
        )

    def _send_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for _ in range(3600):
            payload = {"dashboard": render_dashboard(self.store, limit=12), "status": status_payload(self.store)}
            self.wfile.write(f"data: {json.dumps(payload, default=str)}\n\n".encode("utf-8"))
            self.wfile.flush()
            time.sleep(1)


def run_web(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"AutoCode web dashboard: http://{host}:{port}")
    server.serve_forever()
