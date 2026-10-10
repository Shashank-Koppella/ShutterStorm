"""User-journey probes (J0..J6), timing and the probe-asset tracker.

Every probe returns a :class:`ProbeResult`. A request that exceeds
``PROBE_TIMEOUT_SECONDS`` is recorded as *hung* (``timed_out=True``), which is
deliberately distinct from a fast error: hung dependencies are the most
important finding in this project.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import requests

from .config import PROBE_PREFIX, Settings
from .immich import ImmichClient

JOURNEYS = {
    "J0": "liveness (ping)",
    "J1": "login",
    "J2": "browse (metadata search)",
    "J3": "smart search (ML)",
    "J4": "upload",
    "J5": "async processing (thumbnail)",
    "J6": "integrity (SHA-1 of original)",
}
PROTECTED_JOURNEYS = ("J0", "J1", "J2", "J6")
ALL_JOURNEYS = tuple(JOURNEYS)
FAST_FAIL_SECONDS = 5.0


@dataclass
class ProbeResult:
    """Outcome of one probe call."""

    journey: str
    ok: bool
    status_code: int | None
    latency_ms: float
    timed_out: bool
    error_text: str = ""
    phase: str = ""
    experiment: str = ""
    t: float = field(default_factory=time.time)

    @property
    def outcome(self) -> str:
        """``ok``, ``hung`` (timed out) or ``fast-fail``."""
        if self.ok:
            return "ok"
        return "hung" if self.timed_out else "fast-fail"

    def to_dict(self) -> dict:
        return asdict(self)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; ``None`` for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[k]


def summarize(results: list[ProbeResult]) -> dict[str, dict]:
    """Per-journey counts and p50/p95 latency of successful calls."""
    summary: dict[str, dict] = {}
    for journey in ALL_JOURNEYS:
        rs = [r for r in results if r.journey == journey]
        if not rs:
            continue
        ok_lat = [r.latency_ms for r in rs if r.ok]
        summary[journey] = {
            "calls": len(rs),
            "ok": sum(r.ok for r in rs),
            "hung": sum(r.timed_out for r in rs),
            "fast_fail": sum((not r.ok) and (not r.timed_out) for r in rs),
            "p50_ms": percentile(ok_lat, 50),
            "p95_ms": percentile(ok_lat, 95),
        }
    return summary


def make_png(width: int = 64, height: int = 64, seed: bytes | None = None) -> bytes:
    """Build a unique random-colour PNG in pure Python (zlib + struct)."""
    noise = seed or os.urandom(16)
    pixels = hashlib.shake_256(noise).digest(width * height * 3)
    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class AssetTracker:
    """Remembers every asset ShutterStorm uploads, so cleanup deletes only those.

    State is persisted in ``.shutterstorm/probe_assets.json`` so ``panic`` can
    clean up after a crashed run. Uploads that timed out are kept as *pending
    filenames* and resolved later by searching for the exact file name.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.assets: dict[str, str] = data.get("assets", {})          # id -> filename
        self.pending: list[str] = data.get("pending_filenames", [])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"assets": self.assets, "pending_filenames": self.pending}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, asset_id: str, filename: str) -> None:
        with self._lock:
            self.assets[asset_id] = filename
            if filename in self.pending:
                self.pending.remove(filename)
            self._save()

    def add_pending(self, filename: str) -> None:
        with self._lock:
            if filename not in self.pending:
                self.pending.append(filename)
                self._save()

    def reconcile(self, client: ImmichClient) -> list[str]:
        """Look up uploads whose response never arrived. Returns newly found IDs."""
        found = []
        for filename in list(self.pending):
            try:
                matches = client.find_assets_by_filename(filename)
            except (requests.RequestException, RuntimeError):
                continue
            for asset in matches:
                self.add(asset["id"], filename)
                found.append(asset["id"])
        return found

    def cleanup(self, client: ImmichClient) -> dict:
        """Delete tracked assets, but only ones whose stored file name has our prefix."""
        self.reconcile(client)
        to_delete, skipped = [], []
        for asset_id in list(self.assets):
            try:
                resp = client.get_asset(asset_id)
            except requests.RequestException as exc:
                skipped.append({"id": asset_id, "reason": f"lookup failed: {exc.__class__.__name__}"})
                continue
            if resp.status_code in (400, 404):
                with self._lock:
                    self.assets.pop(asset_id, None)
                continue
            name = resp.json().get("originalFileName", "") if resp.status_code == 200 else ""
            if name.startswith(PROBE_PREFIX):
                to_delete.append(asset_id)
            else:
                skipped.append({"id": asset_id, "reason": f"name {name!r} lacks probe prefix"})
        if to_delete:
            resp = client.delete_assets(to_delete)
            if resp.status_code not in (200, 204):
                return {"deleted": 0, "skipped": skipped, "error": f"HTTP {resp.status_code}",
                        "remaining": list(self.assets), "pending": list(self.pending)}
            with self._lock:
                for asset_id in to_delete:
                    self.assets.pop(asset_id, None)
        with self._lock:
            self._save()
        return {"deleted": len(to_delete), "skipped": skipped,
                "remaining": list(self.assets), "pending": list(self.pending)}


@dataclass
class UploadRecord:
    """An upload made by J4, kept to check eventual processing and integrity."""

    filename: str
    sha1: str
    asset_id: str | None
    phase: str
    experiment: str


class Prober:
    """Runs user journeys against Immich and times them."""

    def __init__(self, settings: Settings, tracker: AssetTracker, run_tag: str,
                 client_factory: Callable[[], ImmichClient] | None = None) -> None:
        self.settings = settings
        self.tracker = tracker
        self.run_tag = run_tag
        self.timeout = settings.probe_timeout_seconds
        self._factory = client_factory or (lambda: ImmichClient(settings.immich_url, self.timeout))
        self._counter = 0
        self._counter_lock = threading.Lock()
        self.token: str | None = None
        self.uploads: list[UploadRecord] = []
        self.experiment = ""

    # ---------------------------------------------------------------- helpers
    def client(self) -> ImmichClient:
        """A fresh client (one per thread) that reuses the session token."""
        c = self._factory()
        c.timeout = self.timeout
        c.token = self.token
        return c

    def authenticate(self) -> None:
        """Log in once and keep the token for the probes."""
        c = self._factory()
        resp = c.login(self.settings.immich_email, self.settings.immich_password)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Immich login failed: HTTP {resp.status_code}")
        self.token = c.token

    def _timed(self, journey: str, phase: str, call: Callable[[], requests.Response],
               ok_codes: tuple[int, ...] = (200, 201)) -> tuple[ProbeResult, requests.Response | None]:
        start = time.perf_counter()
        resp = None
        try:
            resp = call()
            ok = resp.status_code in ok_codes
            res = ProbeResult(journey, ok, resp.status_code, _ms(start), False,
                              "" if ok else _short(resp.text), phase, self.experiment)
        except requests.Timeout:
            res = ProbeResult(journey, False, None, _ms(start), True,
                              f"no response within {self.timeout:.0f}s", phase, self.experiment)
        except requests.RequestException as exc:
            res = ProbeResult(journey, False, None, _ms(start), False, _short(str(exc)), phase, self.experiment)
        return res, resp

    def _next_filename(self) -> str:
        with self._counter_lock:
            self._counter += 1
            n = self._counter
        return f"{PROBE_PREFIX}{self.run_tag}-{n}.png"

    # ---------------------------------------------------------------- journeys
    def j0_liveness(self, phase: str = "") -> ProbeResult:
        res, resp = self._timed("J0", phase, lambda: self.client().ping())
        if res.ok and resp is not None and resp.json().get("res") != "pong":
            res.ok, res.error_text = False, "unexpected ping body"
        return res

    def j1_login(self, phase: str = "") -> ProbeResult:
        c = self.client()
        res, _ = self._timed("J1", phase, lambda: c.login(
            self.settings.immich_email, self.settings.immich_password, store=False))
        return res

    def j2_browse(self, phase: str = "") -> ProbeResult:
        res, _ = self._timed("J2", phase, lambda: self.client().metadata_search(size=10))
        return res

    def j3_smart_search(self, phase: str = "") -> ProbeResult:
        # Immich caches the CLIP embedding per query text, so a repeated query never
        # reaches the ML service. Every probe therefore uses a unique query.
        query = f"tree {os.urandom(4).hex()}"
        res, _ = self._timed("J3", phase, lambda: self.client().smart_search(query, size=10))
        return res

    def upload_chain(self, phase: str, async_deadline: float, journeys: tuple[str, ...]) -> list[ProbeResult]:
        """J4 upload, then J6 integrity and J5 async processing on the new asset."""
        filename = self._next_filename()
        data = make_png()
        sha1 = hashlib.sha1(data).hexdigest()
        c = self.client()
        self.tracker.add_pending(filename)  # in case the response never arrives
        res4, resp = self._timed("J4", phase, lambda: c.upload(data, filename), ok_codes=(201,))
        results = [res4]
        asset_id = None
        if resp is not None and resp.status_code in (200, 201):
            asset_id = resp.json().get("id")
            if asset_id:
                self.tracker.add(asset_id, filename)
        self.uploads.append(UploadRecord(filename, sha1, asset_id, phase, self.experiment))
        if not asset_id:
            return results
        if "J6" in journeys:
            results.append(self.j6_integrity(asset_id, sha1, phase))
        if "J5" in journeys:
            results.append(self.j5_processing(asset_id, async_deadline, phase))
        return results

    def j5_processing(self, asset_id: str, deadline_s: float, phase: str = "") -> ProbeResult:
        """Poll the asset until ``thumbhash`` is set (thumbnail job finished)."""
        start = time.perf_counter()
        last_err = ""
        c = self.client()
        while time.perf_counter() - start < deadline_s:
            try:
                resp = c.get_asset(asset_id, timeout=min(self.timeout, max(0.5, deadline_s)))
                if resp.status_code == 200 and resp.json().get("thumbhash"):
                    return ProbeResult("J5", True, 200, _ms(start), False, "", phase, self.experiment)
                last_err = "" if resp.status_code == 200 else f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                last_err = exc.__class__.__name__
            time.sleep(min(1.0, max(0.05, deadline_s / 20)))
        msg = f"thumbnail not generated within {deadline_s:.0f}s" + (f" ({last_err})" if last_err else "")
        return ProbeResult("J5", False, None, _ms(start), True, msg, phase, self.experiment)

    def j6_integrity(self, asset_id: str, sha1: str, phase: str = "") -> ProbeResult:
        res, resp = self._timed("J6", phase, lambda: self.client().original(asset_id))
        if res.ok and resp is not None and hashlib.sha1(resp.content).hexdigest() != sha1:
            res.ok, res.error_text = False, "SHA-1 mismatch between uploaded and downloaded bytes"
        return res

    # ---------------------------------------------------------------- rounds
    def run_round(self, phase: str, journeys: tuple[str, ...] = ALL_JOURNEYS,
                  async_deadline: float = 90.0) -> list[ProbeResult]:
        """Run the requested journeys concurrently (so one hang does not block the rest)."""
        simple = {"J0": self.j0_liveness, "J1": self.j1_login, "J2": self.j2_browse, "J3": self.j3_smart_search}
        results: list[ProbeResult] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(fn, phase) for j, fn in simple.items() if j in journeys]
            if "J4" in journeys:
                futures.append(pool.submit(self.upload_chain, phase, async_deadline, journeys))
            for fut in futures:
                value = fut.result()
                results.extend(value if isinstance(value, list) else [value])
        results.sort(key=lambda r: r.journey)
        return results

    def warmup(self) -> None:
        """One untimed smart search so a cold ML model does not distort the baseline."""
        try:
            self.client().smart_search(f"tree warmup {time.time()}", timeout=max(self.timeout, 60))
        except requests.RequestException:
            pass

    def queue_snapshot(self) -> dict:
        """Waiting/active/failed counts per BullMQ queue (or the error)."""
        try:
            queues = self.client().queues(timeout=self.timeout)
            return {q["name"]: {k: q["statistics"].get(k, 0) for k in ("waiting", "active", "failed", "delayed")}
                    | {"paused": q.get("isPaused", False)} for q in queues}
        except (requests.RequestException, RuntimeError) as exc:
            return {"error": _short(str(exc))}


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."
