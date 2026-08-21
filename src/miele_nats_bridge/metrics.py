"""Prometheus metrics registry and a tiny HTTP server exposing /metrics and /healthz."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

from .logging_setup import TrackedStreamHandler

logger = logging.getLogger(__name__)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.miele_connected = Gauge(
            "miele_connected",
            "1 if the Miele cloud event stream is currently up, 0 otherwise",
            registry=self.registry,
        )
        self.nats_connected = Gauge(
            "nats_connected",
            "1 if NATS client is currently connected, 0 otherwise",
            registry=self.registry,
        )
        # Absolute expiry rather than a countdown: a gauge that decreases on its
        # own would go stale between scrapes, so alerting subtracts from time().
        self.token_expiry = Gauge(
            "miele_token_expiry_timestamp_seconds",
            "Unix timestamp at which the current access token expires",
            registry=self.registry,
        )
        self.events = Counter(
            "miele_events_total",
            "Server-sent events received from the cloud by type (devices | actions | ping)",
            ["kind"],
            registry=self.registry,
        )
        self.api_errors = Counter(
            "miele_api_errors_total",
            "Cloud API errors by reason",
            ["reason"],
            registry=self.registry,
        )
        self.resyncs = Counter(
            "miele_resyncs_total",
            "Full GET /devices resyncs by trigger (startup | reconnect)",
            ["trigger"],
            registry=self.registry,
        )
        self.messages_published = Counter(
            "miele_messages_published_total",
            "Normalized messages successfully published to NATS by kind (state | eco)",
            ["device", "kind"],
            registry=self.registry,
        )
        self.publish_errors = Counter(
            "miele_publish_errors_total",
            "Publish errors by reason",
            ["device", "reason"],
            registry=self.registry,
        )
        self.last_event_ts = Gauge(
            "miele_last_event_received_timestamp",
            "Unix timestamp of the last SSE frame received, keep-alive included: "
            "stream liveness, not appliance activity, which is bursty",
            registry=self.registry,
        )
        # Surface logger-health state so a stuck stdout is visible in Prometheus,
        # not just via liveness. Source of truth is TrackedStreamHandler.
        self.log_emit_errors = Gauge(
            "miele_bridge_log_emit_errors",
            "Cumulative count of logging handler emit() failures since pod start",
            registry=self.registry,
        )
        self.log_emit_errors.set_function(lambda: float(TrackedStreamHandler.emit_errors_total))
        self.log_last_emit_ok_timestamp = Gauge(
            "miele_bridge_log_last_emit_ok_timestamp",
            "Monotonic-seconds timestamp of the last successful log emit",
            registry=self.registry,
        )
        self.log_last_emit_ok_timestamp.set_function(
            lambda: float(TrackedStreamHandler.last_emit_ok_ts)
        )


async def serve(
    metrics: Metrics,
    port: int,
    is_healthy: Callable[[], Awaitable[bool]] | Callable[[], bool],
) -> asyncio.AbstractServer:
    """Start a tiny HTTP server exposing /metrics and /healthz."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            # Drain the rest of the request headers.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            parts = request_line.decode("ascii", errors="replace").split()
            path = parts[1] if len(parts) >= 2 else "/"

            # The handler serves exactly one response per connection, so
            # announce that instead of HTTP/1.1's implicit keep-alive.
            if path.startswith("/metrics"):
                body = generate_latest(metrics.registry)
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Connection: close\r\n"
                    + f"Content-Type: {CONTENT_TYPE_LATEST}\r\n".encode("ascii")
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body
                )
            elif path.startswith("/healthz"):
                result = is_healthy()
                if asyncio.iscoroutine(result):
                    ok = await result
                else:
                    ok = bool(result)
                status = b"200 OK" if ok else b"503 Service Unavailable"
                body = b"ok\n" if ok else b"unhealthy\n"
                writer.write(
                    b"HTTP/1.1 " + status + b"\r\n"
                    b"Connection: close\r\n"
                    b"Content-Type: text/plain\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body
                )
            else:
                body = b"not found\n"
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\n"
                    b"Connection: close\r\n"
                    b"Content-Type: text/plain\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body
                )
            await writer.drain()
        except Exception:
            logger.exception("metrics http handler failed")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, host="0.0.0.0", port=port)
    logger.info("metrics server listening on :%d", port)
    return server
