"""Traffic-spike load generator (the ``traffic_spike`` fault).

N worker threads replay read-only user traffic against our own Immich as fast as it
answers: browse (metadata search, Postgres), smart search (ML + Postgres) and ping.
It never uploads or deletes anything, so there is nothing to clean up in Immich.

The generator is a thread of the process that owns the fault, so it cannot outlive
it: if ShutterStorm dies, the load stops with it (the guard has nothing to undo).
In-process, the journal recipe ``stop_load`` stops it via :func:`stop_load`.
"""
from __future__ import annotations

import os
import threading
import time

import requests

from .immich import ImmichClient

REQUEST_TIMEOUT_S = 10.0
_REGISTRY: dict[str, "LoadGenerator"] = {}
_LOCK = threading.Lock()


class LoadGenerator:
    """A fixed pool of worker threads, each sending one request at a time."""

    def __init__(self, base_url: str, token: str | None, workers: int) -> None:
        self.base_url = base_url
        self.token = token
        self.workers = workers
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.started = 0.0
        self.stopped = 0.0
        self._lock = threading.Lock()
        self.counts = {"sent": 0, "ok": 0, "errors": 0, "timeouts": 0}

    def start(self) -> None:
        self.started = time.time()
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True, name=f"shutterstorm-load-{i}")
            t.start()
            self.threads.append(t)

    def _worker(self, n: int) -> None:
        client = ImmichClient(self.base_url, REQUEST_TIMEOUT_S)
        client.token = self.token
        i = 0
        while not self.stop_event.is_set():
            i += 1
            try:
                # Mix: 3 x browse (DB), 1 x smart search with a unique query (ML), 1 x ping.
                kind = (n + i) % 5
                if kind == 0:
                    resp = client.smart_search(f"spike {os.urandom(3).hex()}", size=5)
                elif kind == 1:
                    resp = client.ping()
                else:
                    resp = client.metadata_search(size=25)
                ok = resp.status_code in (200, 201)
                self._count("ok" if ok else "errors")
            except requests.Timeout:
                self._count("timeouts")
            except requests.RequestException:
                self._count("errors")
                self.stop_event.wait(0.2)  # connection refused: do not spin

    def _count(self, key: str) -> None:
        with self._lock:
            self.counts["sent"] += 1
            self.counts[key] += 1

    def stop(self) -> None:
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=REQUEST_TIMEOUT_S + 2)
        self.stopped = time.time()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self.threads)

    def stats(self) -> dict:
        end = self.stopped or time.time()
        duration = max(0.001, end - self.started)
        with self._lock:
            counts = dict(self.counts)
        return {**counts, "workers": self.workers, "duration_s": round(duration, 1),
                "requests_per_s": round(counts["sent"] / duration, 1)}


def start_load(ctx, load_id: str, workers: int) -> LoadGenerator:
    """Start a generator against the sandbox's Immich URL (the only target it can have)."""
    gen = LoadGenerator(ctx.settings.immich_url, ctx.admin().token, workers)
    with _LOCK:
        _REGISTRY[load_id] = gen
    ctx.log(f"traffic spike: {workers} concurrent clients against {ctx.settings.immich_url}")
    gen.start()
    return gen


def stop_load(load_id: str) -> dict | None:
    """Stop a generator; idempotent. Unknown id = it belonged to another (dead) process."""
    with _LOCK:
        gen = _REGISTRY.get(load_id)
    if gen is None:
        return None
    if gen.running:
        gen.stop()
    return gen.stats()


def load_running(load_id: str) -> bool:
    with _LOCK:
        gen = _REGISTRY.get(load_id)
    return bool(gen and gen.running)


def load_stats(load_id: str) -> dict | None:
    with _LOCK:
        gen = _REGISTRY.get(load_id)
    return gen.stats() if gen else None


def stop_all() -> int:
    """Stop every generator of this process (used by panic)."""
    with _LOCK:
        gens = list(_REGISTRY.values())
    for gen in gens:
        if gen.running:
            gen.stop()
    return len(gens)
