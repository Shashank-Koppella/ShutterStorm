"""Minimal Immich v3.3.1 API client (only endpoints verified against the OpenAPI spec).

Methods return :class:`requests.Response` objects (or parsed JSON for admin helpers)
and let ``requests`` exceptions propagate, so callers such as the probes can tell a
timeout (hung dependency) apart from a fast error.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests


class ImmichError(RuntimeError):
    """Raised by the admin helpers when Immich answers with an error status."""


class ImmichClient:
    """Thin wrapper around the Immich REST API."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.api = base_url.rstrip("/") + "/api"
        self.timeout = timeout
        self.token: str | None = None
        self.session = requests.Session()

    # ---------------------------------------------------------------- basics
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, timeout: float | None = None, **kwargs) -> requests.Response:
        """Send one request. ``path`` starts with ``/`` and is relative to ``/api``."""
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        return self.session.request(
            method, self.api + path, headers=headers, timeout=timeout or self.timeout, **kwargs
        )

    def ping(self) -> requests.Response:
        return self.request("GET", "/server/ping")

    def version(self) -> requests.Response:
        return self.request("GET", "/server/version")

    def login(self, email: str, password: str, store: bool = True) -> requests.Response:
        """POST /auth/login. With ``store`` the access token is kept for later calls."""
        resp = self.session.post(
            self.api + "/auth/login", json={"email": email, "password": password}, timeout=self.timeout
        )
        if store and resp.status_code in (200, 201):
            self.token = resp.json()["accessToken"]
        return resp

    # ---------------------------------------------------------------- assets
    def upload(self, data: bytes, filename: str, timeout: float | None = None) -> requests.Response:
        """POST /assets (multipart). 201 = created, 200 = duplicate."""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fields = {"fileCreatedAt": now, "fileModifiedAt": now, "filename": filename}
        files = {"assetData": (filename, data, "image/png")}
        return self.request("POST", "/assets", timeout=timeout, data=fields, files=files)

    def get_asset(self, asset_id: str, timeout: float | None = None) -> requests.Response:
        return self.request("GET", f"/assets/{asset_id}", timeout=timeout)

    def thumbnail(self, asset_id: str) -> requests.Response:
        return self.request("GET", f"/assets/{asset_id}/thumbnail")

    def original(self, asset_id: str, timeout: float | None = None) -> requests.Response:
        return self.request("GET", f"/assets/{asset_id}/original", timeout=timeout)

    def delete_assets(self, ids: list[str]) -> requests.Response:
        return self.request("DELETE", "/assets", json={"ids": ids, "force": True})

    # ---------------------------------------------------------------- search
    def smart_search(self, query: str, size: int = 10, timeout: float | None = None) -> requests.Response:
        """POST /search/smart (needs the ML service)."""
        return self.request("POST", "/search/smart", timeout=timeout, json={"query": query, "size": size})

    def metadata_search(self, size: int = 10, timeout: float | None = None, **filters: Any) -> requests.Response:
        """POST /search/metadata (database only)."""
        return self.request("POST", "/search/metadata", timeout=timeout, json={"size": size, **filters})

    def find_assets_by_filename(self, filename: str) -> list[dict]:
        """Return assets whose original file name equals ``filename``."""
        resp = self.metadata_search(size=50, originalFileName=filename)
        if resp.status_code != 200:
            raise ImmichError(f"metadata search failed: HTTP {resp.status_code}")
        items = resp.json().get("assets", {}).get("items", [])
        return [a for a in items if a.get("originalFileName") == filename]

    # ---------------------------------------------------------------- admin
    def queues(self, timeout: float | None = None) -> list[dict]:
        resp = self.request("GET", "/queues", timeout=timeout)
        if resp.status_code != 200:
            raise ImmichError(f"GET /queues: HTTP {resp.status_code}")
        return resp.json()

    def get_system_config(self) -> dict:
        resp = self.request("GET", "/system-config")
        if resp.status_code != 200:
            raise ImmichError(f"GET /system-config: HTTP {resp.status_code}")
        return resp.json()
