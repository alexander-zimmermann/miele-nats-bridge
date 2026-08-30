"""Prometheus metrics registry and a tiny HTTP server exposing /metrics and /healthz."""

from __future__ import annotations

import logging
from typing import cast

from nats_bridge_core import TrackedStreamHandler
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
)

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

    # --- nats_bridge_core.PublisherMetrics -------------------------------
    # ctx is the (appliance, kind) pair handed to enqueue().

    def set_connected(self, connected: bool) -> None:
        self.nats_connected.set(1 if connected else 0)

    def count_published(self, ctx: object) -> None:
        device, kind = cast(tuple[str, str], ctx)
        self.messages_published.labels(device=device, kind=kind).inc()

    def count_error(self, ctx: object, reason: str) -> None:
        device, _ = cast(tuple[str, str], ctx)
        self.publish_errors.labels(device=device, reason=reason).inc()
