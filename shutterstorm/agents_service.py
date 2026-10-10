"""The AI agents as an isolated HTTP service: ``python -m shutterstorm.agents_service``.

Runs in the ``agents`` container, which holds the Mistral key but has no Docker socket and
no network route to Immich: the AI can only receive evidence and send JSON back. The
controller re-checks every answer (see :mod:`shutterstorm.agentclient`).

    GET  /health               -> {"ok": true, "model": ..., "llm_mode": ..., "key": bool}
    POST /agents/<name>        {"run_id": str, "args": {...}} -> {"result": ..., "stats": {...}}

One LLM client per run id, so the per-run call budget and request pacing work as they do
in-process; the answer cache is shared.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agentclient import FUNCTIONS, run_local
from .config import load_settings, redact
from .llm import LLMClient

MAX_BODY = 20 * 1024 * 1024
MAX_RUNS = 20


def make_server(settings, host: str = "127.0.0.1", port: int = 8780, log=print) -> ThreadingHTTPServer:
    clients: dict[str, LLMClient] = {}
    lock = threading.Lock()

    def client_for(run_id: str) -> LLMClient:
        with lock:
            if run_id not in clients:
                if len(clients) >= MAX_RUNS:
                    clients.pop(next(iter(clients)))
                clients[run_id] = LLMClient(settings, lambda m: log(redact(f"[{run_id or '-'}] {m}", settings.secrets())))
            return clients[run_id]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, data) -> None:
            body = json.dumps(data, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True, "model": settings.mistral_model, "llm_mode": settings.llm_mode,
                                        "key": bool(settings.mistral_api_key), "agents": sorted(FUNCTIONS)})
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            name = self.path.removeprefix("/agents/")
            if not self.path.startswith("/agents/") or name not in FUNCTIONS:
                return self._send(404, {"error": f"unknown agent {name!r}"})
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                llm = client_for(str(body.get("run_id", "")))
                before = {**llm.stats, "api_calls": llm.calls}
                result = run_local(llm, name, body.get("args") or {})
                after = {**llm.stats, "api_calls": llm.calls}
                log(f"agent {name} for run {body.get('run_id') or '-'}: "
                    + ", ".join(f"{k} +{after[k] - before[k]}" for k in after if after[k] != before[k]))
                self._send(200, {"result": result, "stats": {k: after[k] - before[k] for k in after}})
            except Exception as exc:  # noqa: BLE001 - the controller falls back on any error
                log(f"agent {name} failed: {exc.__class__.__name__}: {redact(str(exc), settings.secrets())}")
                self._send(500, {"error": f"{exc.__class__.__name__}"})

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shutterstorm.agents_service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args(argv)
    settings = load_settings()
    server = make_server(settings, args.host, args.port, lambda m: print(m, flush=True))
    print(f"ShutterStorm agents service on {args.host}:{args.port} (model {settings.mistral_model}, "
          f"LLM_MODE={settings.llm_mode}, key {'set' if settings.mistral_api_key else 'missing'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
