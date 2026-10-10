"""Simulation mode: a fake Docker backend + an in-process fake Immich HTTP server.

Used by pytest and by ``--simulate`` offline demos. Everything the real code does
runs unchanged: :class:`FakeDocker` answers the same ``docker ...`` argument lists
that :class:`~shutterstorm.dockerctl.RealDocker` would run, and the fake Immich is
a real HTTP server on 127.0.0.1, so the real :class:`ImmichClient` talks to it.

The fake reproduces the manual findings:

* M-01 ML paused  -> smart search hangs (then errors after unpause)
* M-02 ML stopped -> smart search fails fast with a 500
* M-03 Redis paused -> upload is stored but the response never arrives; no thumbnail until unpause

All output from simulation mode is labelled SIMULATED. It is never a real finding.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import COMPOSE_PROJECT, ML, ML2, OVERRIDE_FILE, POSTGRES, REDIS, SERVER
from .dockerctl import CmdResult

ML_IMAGE = "ghcr.io/immich-app/immich-machine-learning:v3.3.1"
NETSHOOT = "nicolaka/netshoot"
HANG_CAP_S = 15.0
COMPOSE = {
    "name": "immich",
    "services": {
        "immich-server": {"container_name": SERVER, "image": "ghcr.io/immich-app/immich-server:v3.3.1",
                          "depends_on": {"redis": {"condition": "service_started"},
                                         "database": {"condition": "service_started"}},
                          "ports": [{"target": 2283, "published": "2283"}]},
        "immich-machine-learning": {"container_name": ML, "image": ML_IMAGE},
        "redis": {"container_name": REDIS, "image": "docker.io/valkey/valkey:9"},
        "database": {"container_name": POSTGRES, "image": "ghcr.io/immich-app/postgres:14"},
    },
}
SERVICE_CONTAINER = {"immich-server": SERVER, "immich-machine-learning": ML, "redis": REDIS, "database": POSTGRES,
                     "immich-machine-learning-2": ML2}


def default_system_config() -> dict:
    return {
        "machineLearning": {"enabled": True, "urls": ["http://immich-machine-learning:3003"],
                            "availabilityChecks": {"enabled": True, "timeout": 2000, "interval": 30000},
                            "clip": {"enabled": True, "modelName": "ViT-B-32__openai"}},
        "job": {q: {"concurrency": c} for q, c in [("thumbnailGeneration", 3), ("metadataExtraction", 5),
                                                    ("videoConversion", 1), ("faceDetection", 2),
                                                    ("smartSearch", 2), ("backgroundTask", 5)]},
        "server": {"externalDomain": ""},
    }


class _QuietServer(ThreadingHTTPServer):
    """Clients that time out hang up on hung handlers; that is expected, not an error."""

    def handle_error(self, request, client_address) -> None:
        import sys

        if not isinstance(sys.exc_info()[1], (ConnectionError, OSError)):
            super().handle_error(request, client_address)


class SimWorld:
    """Shared state of the simulated sandbox (containers, Immich data, config)."""

    EMAIL = "admin@sim.local"
    PASSWORD = "sim-password"

    def __init__(self, base_dir: Path) -> None:
        self.cond = threading.Condition()
        self.containers: dict[str, dict] = {name: self._container(name) for name in (SERVER, ML, REDIS, POSTGRES)}
        # A container that is NOT ours: the scope check must refuse it.
        self.containers["other_team_db"] = self._container("other_team_db", project="someone-else")
        self.helpers: dict[str, dict] = {}
        self.netem = False
        self.system_config = default_system_config()
        self.assets: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.immich_dir = Path(base_dir)
        self.immich_dir.mkdir(parents=True, exist_ok=True)
        (self.immich_dir / "docker-compose.yml").write_text("# simulated compose file\n", encoding="utf-8")
        (self.immich_dir / ".env").write_text("IMMICH_VERSION=v3.3.1\n", encoding="utf-8")
        (self.immich_dir / OVERRIDE_FILE).unlink(missing_ok=True)
        self.docker = FakeDocker(self)
        self.server = _QuietServer(("127.0.0.1", 0), _make_handler(self))
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def _container(name: str, project: str = COMPOSE_PROJECT) -> dict:
        return {"running": True, "paused": False, "nano": 0, "project": project}

    # ---------------------------------------------------------------- state
    def change(self, fn) -> None:
        """Mutate state under the lock and wake up waiting (hung) requests."""
        with self.cond:
            fn()
            self.cond.notify_all()

    def up(self, name: str) -> bool:
        c = self.containers.get(name)
        return bool(c and c["running"] and not c["paused"])

    def wait_until(self, predicate, cap: float = HANG_CAP_S) -> bool:
        with self.cond:
            return self.cond.wait_for(predicate, timeout=cap)

    def shutdown(self) -> None:
        # Release every hung handler, then stop the server.
        self.change(lambda: [c.update(paused=False, running=True) for c in self.containers.values()])
        self.server.shutdown()
        self.server.server_close()


# ====================================================================== fake docker
class FakeDocker:
    """Interprets ``docker`` argument lists against :class:`SimWorld`."""

    def __init__(self, world: SimWorld) -> None:
        self.w = world
        self.calls: list[list[str]] = []

    def run(self, args: list[str], timeout: float = 60, cwd: Path | None = None) -> CmdResult:
        self.calls.append(list(args))
        try:
            return self._dispatch(args)
        except KeyError as exc:
            return CmdResult(1, "", f"Error: No such container: {exc.args[0]}")

    def _ok(self, out: str = "") -> CmdResult:
        return CmdResult(0, out, "")

    def _inspect(self, name: str) -> dict:
        if name in self.w.helpers:
            h = self.w.helpers[name]
            return {"Name": "/" + name, "State": {"Running": True, "Paused": False, "Status": "running"},
                    "HostConfig": {"NanoCpus": 0}, "Config": {"Labels": h["labels"], "Env": []}}
        c = self.w.containers[name]
        status = "paused" if c["paused"] else ("running" if c["running"] else "exited")
        health = {"Status": "healthy"} if c["running"] else None
        return {"Name": "/" + name, "State": {"Running": c["running"], "Paused": c["paused"], "Status": status,
                                              "Health": health},
                "HostConfig": {"NanoCpus": c["nano"]},
                "Config": {"Labels": {"com.docker.compose.project": c["project"]}, "Env": ["TZ=Etc/UTC"]}}

    def _dispatch(self, args: list[str]) -> CmdResult:
        w = self.w
        cmd = args[0]
        if cmd == "version":
            return self._ok("29.0.0-sim\n")
        if cmd == "inspect":
            name = args[1]
            if name not in w.containers and name not in w.helpers:
                return CmdResult(1, "[]", f"Error: No such object: {name}")
            return self._ok(json.dumps([self._inspect(name)]))
        if cmd in ("pause", "unpause", "stop", "start"):
            name = args[-1]
            c = w.containers[name]
            if cmd == "pause" and (c["paused"] or not c["running"]):
                return CmdResult(1, "", f"container {name} is not running or already paused")
            if cmd == "unpause" and not c["paused"]:
                return CmdResult(1, "", f"container {name} is not paused")
            updates = {"pause": {"paused": True}, "unpause": {"paused": False},
                       "stop": {"running": False, "paused": False}, "start": {"running": True}}[cmd]
            w.change(lambda: c.update(updates))
            return self._ok(name + "\n")
        if cmd == "restart":
            name = args[-1]
            c = w.containers[name]
            w.change(lambda: c.update(running=False, paused=False))
            time.sleep(0.4)
            w.change(lambda: c.update(running=True))
            return self._ok(name + "\n")
        if cmd == "update":
            name = args[-1]
            cpus = float(args[args.index("--cpus") + 1])
            w.change(lambda: w.containers[name].update(nano=int(cpus * 1e9)))
            return self._ok(name + "\n")
        if cmd == "logs":
            name = args[-1]
            lines = [f"[sim] {name} log line {i}" for i in range(5)]
            if name == SERVER and not w.up(ML):
                lines.append("[sim] Machine learning request to http://immich-machine-learning:3003 failed")
            return self._ok("\n".join(lines) + "\n")
        if cmd == "ps":
            return self._ok("\n".join(sorted(w.helpers)) + ("\n" if w.helpers else ""))
        if cmd == "image":
            return self._ok("[]") if args[2] == NETSHOOT else CmdResult(1, "", "No such image")
        if cmd == "run":
            name = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1].split("=")
            w.helpers[name] = {"labels": {label[0]: label[1]}}
            return self._ok(uuid.uuid4().hex + "\n")
        if cmd == "exec":
            rest = args[2:]
            if rest[:2] == ["tc", "qdisc"]:
                if rest[2] == "add":
                    w.change(lambda: setattr(w, "netem", True))
                elif rest[2] == "del":
                    w.change(lambda: setattr(w, "netem", False))
                    return self._ok()
                elif rest[2] == "show":
                    return self._ok("qdisc netem 8001: root delay 3s\n" if w.netem else "qdisc noqueue 0: root\n")
            return self._ok()
        if cmd == "rm":
            w.helpers.pop(args[-1], None)
            return self._ok(args[-1] + "\n")
        if cmd == "compose":
            return self._compose(args[1:])
        return CmdResult(1, "", f"FakeDocker does not support: {args}")

    def _compose(self, args: list[str]) -> CmdResult:
        w = self.w
        files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
        if "config" in args:
            return self._ok(json.dumps(COMPOSE))
        if "up" in args:
            services: dict = {}
            if OVERRIDE_FILE in files and (w.immich_dir / OVERRIDE_FILE).exists():
                services = json.loads((w.immich_dir / OVERRIDE_FILE).read_text(encoding="utf-8")).get("services", {})
            if "immich-machine-learning-2" in services:
                w.change(lambda: w.containers.setdefault(ML2, SimWorld._container(ML2)))
            elif "--remove-orphans" in args:
                w.change(lambda: w.containers.pop(ML2, None))
            if "--force-recreate" in args:
                target = SERVICE_CONTAINER[args[-1]]
                w.change(lambda: w.containers[target].update(nano=0, running=True, paused=False))
            return self._ok()
        return CmdResult(1, "", f"FakeDocker compose does not support: {args}")


# ====================================================================== fake immich
def _parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    boundary = re.search(r"boundary=([^;]+)", content_type).group(1).strip('"').encode()
    fields: dict[str, bytes] = {}
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        name = re.search(rb'name="([^"]+)"', head)
        if name:
            fields[name.group(1).decode()] = data[:-2] if data.endswith(b"\r\n") else data
    return fields


def _make_handler(w: SimWorld):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # keep test output quiet
            pass

        # ------------------------------------------------------------ helpers
        def _send(self, status: int, payload=None, raw: bytes | None = None) -> None:
            body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the client already gave up (timeout)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

        def _gate(self, name: str, error: str) -> bool:
            """True = continue. A paused container hangs the request; a stopped one fails fast."""
            c = w.containers.get(name)
            if c and c["paused"]:
                w.wait_until(lambda: not c["paused"])
                self._send(500, {"message": f"{error} (dependency was frozen)"})
                return False
            if not c or not c["running"]:
                self._send(500, {"message": error})
                return False
            return True

        def _authed(self) -> bool:
            token = self.headers.get("Authorization", "").replace("Bearer ", "")
            if token not in w.tokens:
                self._send(401, {"message": "Unauthorized"})
                return False
            return True

        def _server_up(self) -> bool:
            if not w.containers[SERVER]["running"]:
                self._send(503, {"message": "server restarting"})
                return False
            return True

        def _asset_dto(self, a: dict) -> dict:
            if a["thumbhash"] is None and w.up(REDIS):
                a["thumbhash"] = "c2ltdGh1bWI="  # job runs once the queue is reachable
            return {"id": a["id"], "originalFileName": a["filename"], "thumbhash": a["thumbhash"]}

        # ------------------------------------------------------------ routes
        def do_GET(self) -> None:
            body_needed = self._body()  # noqa: F841 - drain
            if not self._server_up():
                return
            path = self.path.split("?")[0]
            if path == "/api/server/ping":
                return self._send(200, {"res": "pong"})
            if path == "/api/server/version":
                return self._send(200, {"major": 3, "minor": 3, "patch": 1})
            if not self._authed() or not self._gate(POSTGRES, "Database unavailable"):
                return
            if path == "/api/queues":
                if not self._gate(REDIS, "Redis unavailable"):
                    return
                return self._send(200, [{"name": q, "isPaused": False, "statistics": {
                    "active": 0, "completed": 0, "failed": 0, "delayed": 0, "waiting": 0, "paused": 0}}
                    for q in ("thumbnailGeneration", "metadataExtraction", "smartSearch")])
            if path == "/api/system-config":
                return self._send(200, w.system_config)
            m = re.fullmatch(r"/api/assets/([\w-]+)(/original|/thumbnail)?", path)
            if m:
                a = w.assets.get(m.group(1))
                if not a:
                    return self._send(404, {"message": "Not found"})
                if m.group(2) == "/original":
                    return self._send(200, raw=a["data"])
                return self._send(200, self._asset_dto(a))
            self._send(404, {"message": "unknown route"})

        def do_POST(self) -> None:
            raw = self._body()
            if not self._server_up():
                return
            path = self.path.split("?")[0]
            if path == "/api/auth/login":
                if not self._gate(POSTGRES, "Database unavailable"):
                    return
                data = json.loads(raw or b"{}")
                if data.get("email") != w.EMAIL or data.get("password") != w.PASSWORD:
                    return self._send(401, {"message": "Incorrect email or password"})
                token = uuid.uuid4().hex
                w.tokens.add(token)
                return self._send(201, {"accessToken": token, "userId": "sim-admin", "isAdmin": True})
            if not self._authed() or not self._gate(POSTGRES, "Database unavailable"):
                return
            if path == "/api/search/metadata":
                data = json.loads(raw or b"{}")
                items = [self._asset_dto(a) for a in w.assets.values()
                         if not data.get("originalFileName") or a["filename"] == data["originalFileName"]]
                return self._send(200, {"assets": {"items": items[:data.get("size", 10)], "total": len(items)}})
            if path == "/api/search/smart":
                return self._smart_search()
            if path == "/api/assets":
                fields = _parse_multipart(raw, self.headers.get("Content-Type", ""))
                data = fields.get("assetData", b"")
                asset = {"id": str(uuid.uuid4()), "filename": fields.get("filename", b"x.png").decode(),
                         "data": data, "sha1": hashlib.sha1(data).hexdigest(), "thumbhash": None}
                w.assets[asset["id"]] = asset  # stored *before* the job is queued (M-03 ghost upload)
                if not w.containers[REDIS]["running"]:
                    # Observed on the real system (API_NOTES #10): with Redis *stopped* the upload hangs too.
                    w.wait_until(lambda: w.containers[REDIS]["running"])
                    return self._send(500, {"message": "Failed to queue jobs"})
                if not self._gate(REDIS, "Failed to queue jobs"):
                    return
                return self._send(201, {"id": asset["id"], "status": "created"})
            self._send(404, {"message": "unknown route"})

        def _smart_search(self) -> None:
            cfg = w.system_config["machineLearning"]
            names = [ML2 if "machine-learning-2" in u else ML for u in cfg["urls"]]
            names = [n for n in names if n in w.containers] or [ML]
            chosen = names[0]
            if cfg["availabilityChecks"]["enabled"] and len(names) > 1:
                chosen = next((n for n in names if w.up(n)), names[0])
            if not self._gate(chosen, "Failed to search smart (Immich Server Error)"):
                return
            if chosen == ML and w.netem:
                time.sleep(3.0)
            if w.containers[chosen]["nano"] and w.containers[chosen]["nano"] < 1e9:
                time.sleep(0.8)
            items = [self._asset_dto(a) for a in list(w.assets.values())[:3]]
            self._send(200, {"assets": {"items": items, "total": len(items)}})

        def do_PUT(self) -> None:
            raw = self._body()
            if not self._server_up() or not self._authed() or not self._gate(POSTGRES, "Database unavailable"):
                return
            if self.path == "/api/system-config":
                w.change(lambda: setattr(w, "system_config", copy.deepcopy(json.loads(raw))))
                return self._send(200, w.system_config)
            self._send(404, {"message": "unknown route"})

        def do_DELETE(self) -> None:
            raw = self._body()
            if not self._server_up() or not self._authed() or not self._gate(POSTGRES, "Database unavailable"):
                return
            if self.path == "/api/assets":
                for asset_id in json.loads(raw or b"{}").get("ids", []):
                    w.assets.pop(asset_id, None)
                return self._send(204)
            self._send(404, {"message": "unknown route"})

    return Handler
