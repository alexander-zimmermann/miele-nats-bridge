"""Event-stream parsing: which SSE frames are payload and which are noise."""

from __future__ import annotations

from typing import Any, cast

import httpx
import respx

from miele_nats_bridge.auth import TokenManager
from miele_nats_bridge.client import MieleClient
from miele_nats_bridge.config import API_BASE, Settings
from miele_nats_bridge.metrics import Metrics

EVENTS_URL = f"{API_BASE}/devices/all/events"

# The keep-alive carries a bare timestamp, not JSON, and arrives every ~20s.
PING = "event: ping\ndata: 2026-08-21T12:00:01.977Z\n\n"
DEVICES = 'event: devices\ndata: {"000105454657": {"state": {"status": 1}}}\n\n'


class _StubTokens:
    async def access_token(self) -> str:
        return "access-1"


def _client(metrics: Metrics) -> MieleClient:
    return MieleClient(
        settings=Settings(),
        metrics=metrics,
        tokens=cast(TokenManager, _StubTokens()),
        http=httpx.AsyncClient(),
    )


def _mock_stream(body: str) -> None:
    respx.get(EVENTS_URL).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
    )


async def _drain(client: MieleClient) -> list[tuple[str, Any]]:
    return [item async for item in client.stream_events()]


@respx.mock
async def test_keepalive_is_not_an_api_error() -> None:
    """A ping must not be parsed as JSON — it used to count one error per ping."""
    _mock_stream(PING + DEVICES + PING)
    metrics = Metrics()

    received = await _drain(_client(metrics))

    assert [kind for kind, _ in received] == ["devices"]
    assert metrics.registry.get_sample_value("miele_events_total", {"kind": "ping"}) == 2
    assert (
        metrics.registry.get_sample_value("miele_api_errors_total", {"reason": "bad_event_json"})
        is None
    )


@respx.mock
async def test_malformed_payload_event_still_counts_as_an_error() -> None:
    """Skipping the keep-alive must not blunt the real malformed-JSON detector."""
    _mock_stream("event: devices\ndata: {not json\n\n")
    metrics = Metrics()

    received = await _drain(_client(metrics))

    assert received == []
    assert (
        metrics.registry.get_sample_value("miele_api_errors_total", {"reason": "bad_event_json"})
        == 1
    )


@respx.mock
async def test_connected_gauge_falls_back_to_zero_when_the_stream_ends() -> None:
    _mock_stream(DEVICES)
    metrics = Metrics()

    await _drain(_client(metrics))

    assert metrics.registry.get_sample_value("miele_connected") == 0
