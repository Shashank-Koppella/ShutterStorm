"""Local web UI: ``python -m shutterstorm dashboard`` -> http://127.0.0.1:8765

Serves the Stormglass frontend (static files in ``shutterstorm/web/``, see
docs/design/STORMGLASS.md) and a small JSON API. Only the standard library; listens on
127.0.0.1 unless ``--host`` is given (inside the controller container).

The action buttons run the same CLI commands as a subprocess (``run``, ``run --experiments``,
``plan``, ``stop``, ``panic``), so every safety layer still applies. Hardening for a local
server that can start faults: POST requests need a JSON body and the ``X-ShutterStorm``
header (a plain cross-site form or <img> cannot send it), and requests whose Host header is not
localhost are refused (DNS rebinding).

    GET  /                          the app
    GET  /static/<file>             app assets (css, js, svg)
    GET  /api/status                containers, journal, job, mode, catalog
    GET  /api/runs                  result runs of the current mode
    GET  /api/runs/<id>             one run (findings without raw probe lists)
    GET  /api/runs/<id>/timeline?experiment=<label>   per-journey probe series
    GET  /api/runs/<id>/topology    saved dependency map + observed impact
    GET  /api/runs/<id>/events?after=<seq>            structured events (live views)
    GET  /api/plan/latest           newest dry-run plan with candidate features
    GET  /api/safety                pending undo entries, guard liveness, STOP file
    POST /api/plan  {budget}        dry plan (injects nothing)
    POST /api/plan-run {budget, strict}   AI-planned run
    POST /api/run  {fault, strict}  one catalog fault
    POST /api/stop | /api/panic
    GET  /runs/<id>/report.html, /graph   existing pages
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import webapi
from .config import CORE_CONTAINERS, Settings
from .context import build_context
from .faults import lookup

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
RUN_FOLDER_RE = re.compile(r"run folder: (\d{8}-\d{6}-[A-Za-z0-9_-]+)")

GRAPH_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Dependency Graph</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;background:#fff;color:#1c1c1e;font:15px/1.45 system-ui,Segoe UI,sans-serif}main{max-width:1200px;margin:0 auto;padding:20px 16px}
pre.src{background:#f6f6f4;border:1px solid #ddd;border-radius:8px;padding:10px;overflow:auto;font-size:12px}h2{margin-top:32px}</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script></head><body><main>
<h1>ShutterStorm dependency graphs</h1><p>Source: docs/DEPENDENCY_GRAPH.md (generated from the code's imports and the live Immich config).
If the diagrams do not render (offline), the Mermaid source is shown instead.</p>
__SECTIONS__
<script>if(window.mermaid){mermaid.initialize({startOnLoad:true,securityLevel:"strict"})}</script></main></body></html>"""


class Job:
    """The one experiment subprocess the dashboard may run at a time."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.proc: subprocess.Popen | None = None
        self.label = ""
        self.kind = ""
        self.started_at: float | None = None
        self.log_path = home / "runs" / "dashboard_job.log"
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str], label: str, kind: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log = self.log_path.open("w", encoding="utf-8")
            self.proc = subprocess.Popen([sys.executable, "-m", "shutterstorm", *args], cwd=str(self.home),
                                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.label, self.kind, self.started_at = label, kind, time.time()
            return True

    def status(self) -> dict:
        text = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
        lines = [line for line in text.splitlines() if not line.startswith(("journey ", "J0 ", "J1 ", "J2 ", "J3 ",
                                                                            "J4 ", "J5 ", "J6 "))]
        found = RUN_FOLDER_RE.findall(text) if self.proc is not None else []
        return {"running": self.running, "label": self.label, "kind": self.kind, "started_at": self.started_at,
                "exit": None if self.proc is None else self.proc.poll(), "run_id": found[-1] if found else None,
                "log": "\n".join(lines[-120:]) if self.proc is not None else ""}


def _graph_page(home: Path) -> str:
    doc = home / "docs" / "DEPENDENCY_GRAPH.md"
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    sections = []
    for chunk in text.split("```mermaid")[1:]:
        src = chunk.split("```", 1)[0].strip()
        sections.append(f"<div class='mermaid'>{_esc(src)}</div><details><summary>Mermaid source</summary>"
                        f"<pre class='src'>{_esc(src)}</pre></details>")
    body = "".join(f"<h2>Diagram {i + 1}</h2>{s}" for i, s in enumerate(sections)) or \
        "<p>docs/DEPENDENCY_GRAPH.md not found.</p>"
    return GRAPH_PAGE.replace("__SECTIONS__", body)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_handler(settings: Settings, ctx, job: Job):
    home = settings.home

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        # ---------------------------------------------------------- responses
        def _send(self, code: int, body: bytes, ctype: str, csp: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            if csp:
                self.send_header("Content-Security-Policy", APP_CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            host = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return host in LOCAL_HOSTS

        # ---------------------------------------------------------- GET
        def do_GET(self) -> None:
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            url = urlparse(self.path)
            path, query = unquote(url.path), parse_qs(url.query)
            if path in ("/", "/index.html"):
                return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8", csp=True)
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/graph":
                return self._send(200, _graph_page(home).encode(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(self._status())
            if path == "/api/runs":
                return self._json({"mode": _mode(settings), "runs": webapi.list_runs(settings)})
            if path == "/api/plan/latest":
                return self._json({"plan": webapi.latest_plan(settings)})
            if path == "/api/safety":
                return self._json(webapi.safety_now(settings, ctx.journal))
            m = re.fullmatch(r"/api/runs/([^/]+)(/timeline|/topology|/events)?", path)
            if m:
                run_id, sub = m.group(1), m.group(2)
                if sub is None:
                    data = webapi.run_detail(settings, run_id)
                elif sub == "/timeline":
                    data = webapi.timeline(settings, run_id, (query.get("experiment") or [""])[0])
                elif sub == "/topology":
                    data = webapi.topology(settings, run_id)
                else:
                    try:
                        after = int((query.get("after") or ["0"])[0])
                    except ValueError:
                        after = 0
                    data = webapi.events(settings, run_id, after)
                return self._json(data) if data is not None else self._json({"error": "not found"}, 404)
            m = re.fullmatch(r"/runs/([^/]+)/report\.html", path)
            if m:
                d = webapi.run_dir(settings, m.group(1))
                if d is not None and (d / "report.html").exists():
                    return self._send(200, (d / "report.html").read_bytes(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def _static(self, rel: str) -> None:
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR not in target.parents or not target.is_file() or target.suffix not in STATIC_TYPES:
                return self._send(404, b"not found", "text/plain")
            self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix])

        def _status(self) -> dict:
            containers = []
            for name in CORE_CONTAINERS:
                st = ctx.docker.state(name)
                containers.append({"name": name, **{k: st.get(k) for k in ("exists", "running", "paused", "health")}})
            return {"server_time": time.time(), "mode": _mode(settings), "containers": containers,
                    "journal": [e["label"] for e in ctx.journal.entries()], "stop_file": ctx.journal.stop_requested(),
                    "job": job.status(), "catalog": webapi.catalog(settings)}

        # ---------------------------------------------------------- POST
        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json({"error": "forbidden host"}, 403)
            if self.headers.get("X-ShutterStorm") != "1" or \
                    not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json({"error": "missing X-ShutterStorm header or JSON content type"}, 403)
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "invalid JSON"}, 400)
            if not isinstance(body, dict):
                return self._json({"error": "body must be a JSON object"}, 400)
            strict = ["--max-error-rate", "0.1", "--breach-rounds", "1"] if body.get("strict") is True else []
            sim = ["--simulate"] if settings.simulate else []
            if self.path == "/api/run":
                fault = body.get("fault")
                if lookup(fault) is None:
                    return self._json({"error": "unknown fault"}, 400)
                args = ["run", "--experiments", fault, *strict]
                return self._json({"started": job.start([*sim, *args], " ".join(args), "run")})
            if self.path in ("/api/plan-run", "/api/plan"):
                try:
                    budget = str(max(1, min(8, int(body.get("budget") or 6))))
                except (TypeError, ValueError):
                    return self._json({"error": "budget must be a number 1-8"}, 400)
                if self.path == "/api/plan-run":
                    args, kind = ["run", "--budget", budget, *strict], "run"
                else:
                    args, kind = ["plan", "--budget", budget], "plan"
                return self._json({"started": job.start([*sim, *args], " ".join(args), kind)})
            if self.path == "/api/stop":
                ctx.journal.request_stop()
                return self._json({"stop": "requested"})
            if self.path == "/api/panic":
                subprocess.Popen([sys.executable, "-m", "shutterstorm", *sim, "panic"], cwd=str(home),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._json({"panic": "requested"})
            self._json({"error": "unknown"}, 404)

    return Handler


def _mode(settings: Settings) -> str:
    return "simulated" if settings.simulate else "real"


def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765):
    ctx = build_context(settings)
    server = ThreadingHTTPServer((host, port), make_handler(settings, ctx, Job(settings.home)))
    return server, ctx


def serve(settings: Settings, port: int = 8765, host: str = "127.0.0.1") -> None:
    server, ctx = make_server(settings, host, port)
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"ShutterStorm dashboard: http://{shown}:{server.server_address[1]}   (Ctrl+C to quit)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ctx.close()
