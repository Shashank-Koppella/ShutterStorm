"""Mistral chat-completions client with disk cache, replay mode, rate limit and budget.

Modes (``LLM_MODE``):

* ``live``   - call the API (cache hits are reused), fall back on any failure
* ``replay`` - serve only from the cache; a cache miss returns ``None`` (fallback)
* ``off``    - never call; every agent uses its deterministic fallback

Every call returns ``(data, source)`` where ``source`` is ``live``, ``cache`` or
``fallback`` so the report can show where each piece of text came from.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from .config import Settings, redact

API_URL = "https://api.mistral.ai/v1/chat/completions"
MIN_INTERVAL_S = 1.1  # free tier: stay at ~1 request per second


class LLMClient:
    """Small, defensive JSON-only chat client."""

    def __init__(self, settings: Settings, log=None) -> None:
        self.settings = settings
        self.mode = settings.llm_mode
        self.model = settings.mistral_model
        self.cache_dir = settings.state_dir / "llm_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.budget = settings.llm_budget
        self.calls = 0
        self.log = log or (lambda msg: None)
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.stats = {"live": 0, "cache": 0, "fallback": 0}

    # ---------------------------------------------------------------- cache
    def _key(self, messages: list[dict]) -> str:
        blob = json.dumps({"model": self.model, "messages": messages}, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    # ---------------------------------------------------------------- API
    def complete_json(self, messages: list[dict], validate: Callable[[dict], list[str]]) -> tuple[dict | None, str]:
        """Return ``(parsed_json, source)``; ``(None, 'fallback')`` when no valid answer is available."""
        messages = [{**m, "content": redact(m["content"], self.settings.secrets())} for m in messages]
        key = self._key(messages)
        cached = self._cache_path(key)
        if self.mode != "off" and cached.exists():
            data = json.loads(cached.read_text(encoding="utf-8"))
            if not validate(data):
                self.stats["cache"] += 1
                return data, "cache"
        if self.mode != "live" or not self.settings.mistral_api_key:
            self.stats["fallback"] += 1
            return None, "fallback"

        attempt_messages = messages
        for attempt in range(2):  # one retry on invalid JSON / schema
            content = self._post(attempt_messages)
            if content is None:
                break
            try:
                data = json.loads(content)
                errors = validate(data) if isinstance(data, dict) else ["response is not a JSON object"]
            except json.JSONDecodeError as exc:
                data, errors = None, [f"invalid JSON: {exc}"]
            if not errors:
                cached.write_text(json.dumps(data, indent=2), encoding="utf-8")
                self.stats["live"] += 1
                return data, "live"
            self.log(f"LLM answer rejected ({'; '.join(errors)[:200]}); {'retrying' if attempt == 0 else 'using fallback'}")
            attempt_messages = messages + [
                {"role": "assistant", "content": content[:4000]},
                {"role": "user", "content": "Your answer was invalid: " + "; ".join(errors)[:500]
                 + ". Reply again with ONLY a JSON object that matches the requested schema."},
            ]
        self.stats["fallback"] += 1
        return None, "fallback"

    def _post(self, messages: list[dict]) -> str | None:
        """POST with ~1 rps pacing, budget, and backoff on 429/5xx. Returns message content."""
        for attempt in range(5):
            with self._lock:
                if self.calls >= self.budget:
                    self.log(f"LLM budget of {self.budget} calls used up; using fallbacks")
                    return None
                wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
                self.calls += 1
            try:
                resp = requests.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self.settings.mistral_api_key}",
                             "Content-Type": "application/json"},
                    json={"model": self.model, "messages": messages, "temperature": 0.2,
                          "response_format": {"type": "json_object"}},
                    timeout=60,
                )
            except requests.RequestException as exc:
                self.log(f"LLM request error: {exc.__class__.__name__}")
                resp = None
            if resp is not None and resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            status = resp.status_code if resp is not None else "network"
            if resp is not None and resp.status_code not in (429, 500, 502, 503, 504):
                self.log(f"LLM HTTP {status}: {redact(resp.text[:200], self.settings.secrets())}")
                return None
            backoff = min(30, 2 ** attempt) + random.uniform(0, 1)
            self.log(f"LLM HTTP {status}; backing off {backoff:.1f}s")
            time.sleep(backoff)
        return None
