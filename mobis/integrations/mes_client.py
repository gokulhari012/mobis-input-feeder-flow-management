"""Configurable HTTP client for the company MES.

The real MES API (endpoint, payload structure, authentication) is not yet
specified - everything here is driven by :class:`MesConfig` placeholders.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from mobis import __version__
from mobis.core.config_models import MesConfig

log = logging.getLogger("mobis.mes")


@dataclass(frozen=True)
class MesResponse:
    ok: bool
    status_code: Optional[int] = None
    reachable: bool = True
    error: str = ""
    body: str = ""
    already_processed: bool = False
    elapsed_ms: float = 0.0

    @property
    def transient(self) -> bool:
        """Errors worth retrying unchanged (network, timeouts, throttling, server errors)."""
        if not self.reachable:
            return True
        return self.status_code in (408, 429) or (self.status_code or 0) >= 500


def parse_headers(text: str) -> dict[str, str]:
    if not (text or "").strip():
        return {}
    headers = json.loads(text)
    if not isinstance(headers, dict):
        raise ValueError("Request headers must be a JSON object, e.g. {\"X-Plant\": \"P1\"}")
    return {str(k): str(v) for k, v in headers.items()}


def join_url(base: str, path: str) -> str:
    base = (base or "").strip().rstrip("/")
    path = (path or "").strip()
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


def validate_mes_config(cfg: MesConfig) -> list[str]:
    errors = []
    if cfg.enabled and not cfg.base_url.strip():
        errors.append("MES base URL is required when MES integration is enabled.")
    if cfg.base_url.strip() and not cfg.base_url.strip().lower().startswith(("http://", "https://")):
        errors.append("MES base URL must start with http:// or https://")
    try:
        parse_headers(cfg.headers_json)
    except ValueError as exc:
        errors.append(f"Request headers: {exc}")
    try:
        from .mes_payload import parse_field_mapping

        parse_field_mapping(cfg.field_mapping_json)
    except ValueError as exc:
        errors.append(f"Field mapping: {exc}")
    if cfg.auth_type in ("BEARER", "API_KEY") and cfg.enabled and not cfg.api_token:
        errors.append("An API token is required for the selected authentication type.")
    if cfg.auth_type == "BASIC" and cfg.enabled and not cfg.username:
        errors.append("A username is required for basic authentication.")
    if cfg.batch_size < 1:
        errors.append("Batch size must be at least 1.")
    if cfg.sync_interval_s < 5:
        errors.append("Synchronisation interval must be at least 5 seconds.")
    return errors


class MesClient:
    def __init__(self, config: MesConfig, session: Optional[requests.Session] = None):
        self.cfg = config
        self._session = session or requests.Session()

    @property
    def url(self) -> str:
        return join_url(self.cfg.base_url, self.cfg.endpoint)

    def _headers(self, request_id: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"MOBIS-IFFM/{__version__}",
            "X-Request-Id": request_id,
        }
        headers.update(parse_headers(self.cfg.headers_json))
        if self.cfg.auth_type == "BEARER" and self.cfg.api_token:
            headers["Authorization"] = f"Bearer {self.cfg.api_token}"
        elif self.cfg.auth_type == "API_KEY" and self.cfg.api_token:
            headers[self.cfg.api_key_header or "X-API-Key"] = self.cfg.api_token
        return headers

    def _auth(self):
        if self.cfg.auth_type == "BASIC":
            return (self.cfg.username, self.cfg.password)
        return None

    def send(self, body: Any, request_id: str) -> MesResponse:
        if not self.cfg.base_url.strip():
            return MesResponse(False, reachable=False, error="MES base URL not configured")
        started = time.perf_counter()
        try:
            response = self._session.request(
                self.cfg.http_method.upper() or "POST",
                self.url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=self._headers(request_id),
                auth=self._auth(),
                timeout=max(1.0, self.cfg.timeout_s),
                verify=self.cfg.verify_tls,
            )
        except requests.exceptions.RequestException as exc:
            return MesResponse(False, None, reachable=False, error=f"MES unreachable: {exc}",
                               elapsed_ms=(time.perf_counter() - started) * 1000)
        except ValueError as exc:  # invalid header JSON etc.
            return MesResponse(False, None, reachable=False, error=f"Invalid MES configuration: {exc}")
        elapsed = (time.perf_counter() - started) * 1000
        text = (response.text or "")[:4000]
        code = response.status_code
        if 200 <= code < 300:
            return MesResponse(True, code, body=text, elapsed_ms=elapsed)
        if code == 409 and self.cfg.treat_409_as_synced:
            return MesResponse(True, code, body=text, already_processed=True, elapsed_ms=elapsed)
        return MesResponse(False, code, True, f"HTTP {code}: {text[:300]}", text, elapsed_ms=elapsed)

    def test_connection(self) -> MesResponse:
        if not self.cfg.base_url.strip():
            return MesResponse(False, reachable=False, error="MES base URL not configured")
        url = join_url(self.cfg.base_url, self.cfg.health_endpoint) if self.cfg.health_endpoint else self.cfg.base_url
        started = time.perf_counter()
        try:
            response = self._session.get(
                url, headers=self._headers("connection-test"), auth=self._auth(),
                timeout=max(1.0, self.cfg.timeout_s), verify=self.cfg.verify_tls,
            )
        except requests.exceptions.RequestException as exc:
            return MesResponse(False, None, reachable=False, error=f"MES unreachable: {exc}")
        except ValueError as exc:
            return MesResponse(False, None, reachable=False, error=f"Invalid MES configuration: {exc}")
        elapsed = (time.perf_counter() - started) * 1000
        ok = response.status_code < 500
        return MesResponse(ok, response.status_code, True,
                           "" if ok else f"HTTP {response.status_code}", (response.text or "")[:2000], elapsed_ms=elapsed)
