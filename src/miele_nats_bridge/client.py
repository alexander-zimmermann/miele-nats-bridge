"""Miele 3rd Party API access: REST reads plus the server-sent event stream.

Localization has to be requested **both ways**, because the two halves of the API
disagree about how: the REST endpoints honour only the documented ``?language=``
query parameter and ignore ``Accept-Language``, while the events endpoint does the
exact opposite. Sending one form alone yields English from half the API, which
would put two languages into the same archive.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from .auth import TokenManager
from .config import Settings
from .metrics import Metrics

logger = logging.getLogger(__name__)

# The stream is long-lived; the read timeout only needs to outlast the server's
# own keep-alive interval so a silent connection is noticed and reconnected.
_SSE_READ_TIMEOUT = 300.0
_REST_TIMEOUT = 30.0


class RateLimitedError(RuntimeError):
    """HTTP 429 from the cloud API; carries the server's requested delay."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class MieleClient:
    def __init__(
        self,
        settings: Settings,
        metrics: Metrics,
        tokens: TokenManager,
        http: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._metrics = metrics
        self._tokens = tokens
        self._http = http

    async def _headers(self) -> dict[str, str]:
        token = await self._tokens.access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept-Language": self._settings.language,
        }

    async def fetch_devices(self) -> dict[str, Any]:
        """Full state of every appliance the consent covers, keyed by deviceId."""
        payload = await self._get("/devices")
        if not isinstance(payload, dict):
            raise RuntimeError(f"GET /devices returned {type(payload).__name__}, expected object")
        return payload

    async def fetch_programs(self, device_id: str) -> list[dict[str, Any]]:
        """Available programs for one appliance.

        Only answered while the appliance is switched on: an idle dishwasher
        replies 400 "not in the correct state", the ovens 500 instead.
        """
        payload = await self._get(f"/devices/{device_id}/programs")
        if not isinstance(payload, list):
            raise RuntimeError(f"GET programs returned {type(payload).__name__}, expected array")
        return payload

    async def _get(self, path: str) -> Any:
        headers = await self._headers()
        headers["Accept"] = "application/json"
        # REST honours only the query parameter; the header is sent anyway so both
        # halves of the API are addressed the same way.
        response = await self._http.get(
            f"{self._settings.api_base}{path}",
            headers=headers,
            params={"language": self._settings.language},
            timeout=_REST_TIMEOUT,
        )
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            self._metrics.api_errors.labels(reason="rate_limited").inc()
            raise RateLimitedError(_retry_after(response))
        if response.status_code != httpx.codes.OK:
            self._metrics.api_errors.labels(reason=f"http_{response.status_code}").inc()
            raise RuntimeError(
                f"GET {path} failed: HTTP {response.status_code} {response.text[:200]}"
            )
        return response.json()

    async def stream_events(self) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``(event_kind, payload)`` until the stream ends or errors.

        The cloud pushes ``devices`` with the full state of every appliance and
        ``actions`` with the currently permitted operations. Both are complete
        snapshots, so a missed event is corrected by the next one.
        """
        headers = await self._headers()
        headers["Accept"] = "text/event-stream"
        timeout = httpx.Timeout(_REST_TIMEOUT, read=_SSE_READ_TIMEOUT)

        async with aconnect_sse(
            self._http,
            "GET",
            f"{self._settings.api_base}/devices/all/events",
            headers=headers,
            timeout=timeout,
        ) as source:
            response = source.response
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                self._metrics.api_errors.labels(reason="rate_limited").inc()
                raise RateLimitedError(_retry_after(response))
            response.raise_for_status()

            self._metrics.miele_connected.set(1)
            logger.info("event stream connected")
            try:
                async for event in source.aiter_sse():
                    kind = event.event or "message"
                    self._metrics.events.labels(kind=kind).inc()
                    if not event.data:
                        continue
                    try:
                        yield kind, json.loads(event.data)
                    except json.JSONDecodeError:
                        self._metrics.api_errors.labels(reason="bad_event_json").inc()
                        logger.warning("event %r carried invalid JSON", kind)
            finally:
                self._metrics.miele_connected.set(0)
                logger.info("event stream closed")


def _retry_after(response: httpx.Response, default: float = 60.0) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except ValueError:
            logger.debug("unparsable Retry-After header: %r", value)
    return default
