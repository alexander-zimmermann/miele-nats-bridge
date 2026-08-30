"""Stream orchestration: consume cloud events, normalize, publish changes to NATS.

The cloud repeats the full state of every appliance every few seconds, so the
bridge publishes only when a normalized payload actually changed. Without that,
an idle kitchen would write six identical messages into JetStream every tick and
drown the archive in noise.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from nats_bridge_core import Publisher

from .auth import ConsentRequiredError
from .client import MieleClient, RateLimitedError
from .config import ApplianceConfig, Settings
from .metrics import Metrics
from .normalize import TEMPERATURE_KEYS, normalize_eco, normalize_state

logger = logging.getLogger(__name__)


class MieleBridge:
    def __init__(
        self,
        settings: Settings,
        appliances: list[ApplianceConfig],
        client: MieleClient,
        publisher: Publisher,
        metrics: Metrics,
    ) -> None:
        self._settings = settings
        self._client = client
        self._publisher = publisher
        self._metrics = metrics
        self._by_id = {a.device_id: a for a in appliances}
        self._last: dict[tuple[str, str], dict[str, Any]] = {}
        self._unknown_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> asyncio.Task[None]:
        """Start the stream loop and hand back its task so the caller can watch it fail."""
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        backoff = self._settings.sse_backoff_initial_seconds
        while True:
            try:
                # A resync before every (re)connect closes the gap in which the
                # stream was down and state changed unobserved.
                await self._resync("reconnect" if self._last else "startup")
                async for kind, payload in self._client.stream_events():
                    if kind == "devices" and isinstance(payload, dict):
                        self._handle_devices(payload)
                backoff = self._settings.sse_backoff_initial_seconds
                logger.warning("event stream ended without error, reconnecting")
            except asyncio.CancelledError:
                raise
            except ConsentRequiredError:
                # Not retryable: only a new consent round produces a working
                # refresh token, so fail the process instead of looping forever.
                raise
            except RateLimitedError as exc:
                logger.warning("rate limited, sleeping %.0fs", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                continue
            except Exception:
                # Cloud reachability is deliberately not fatal: a Miele outage
                # sets miele_connected 0 instead of restart-looping the pod.
                logger.exception("event stream failed, retrying in %.0fs", backoff)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._settings.sse_backoff_max_seconds)

    async def _resync(self, trigger: str) -> None:
        devices = await self._client.fetch_devices()
        self._metrics.resyncs.labels(trigger=trigger).inc()
        logger.info("resync (%s): %d appliance(s)", trigger, len(devices))
        self._handle_devices(devices)

    def _handle_devices(self, devices: dict[str, Any]) -> None:
        for device_id, entry in devices.items():
            appliance = self._by_id.get(device_id)
            if appliance is None:
                if device_id not in self._unknown_ids:
                    self._unknown_ids.add(device_id)
                    logger.warning(
                        "appliance %s is covered by the consent but not configured", device_id
                    )
                continue
            if not isinstance(entry, dict):
                continue
            state = entry.get("state")
            if not isinstance(state, dict):
                continue

            self._publish_if_changed(
                appliance, "state", appliance.state_subject, normalize_state(appliance.name, state)
            )
            # ecoFeedback is null outside a running or just-finished programme.
            eco = normalize_eco(state)
            if eco:
                self._publish_if_changed(appliance, "eco", appliance.eco_subject, eco)

    def _publish_if_changed(
        self, appliance: ApplianceConfig, kind: str, subject: str, payload: dict[str, Any]
    ) -> None:
        if not payload:
            return
        key = (appliance.name, kind)
        if not self._is_change(self._last.get(key), payload):
            return
        self._last[key] = payload
        self._publisher.enqueue((appliance.name, kind), subject, payload)

    def _is_change(self, previous: dict[str, Any] | None, payload: dict[str, Any]) -> bool:
        """Whether a payload differs enough from the last published one to republish.

        Every field but the temperatures counts as changed on any difference.
        Temperatures need to move by at least the configured threshold: the
        appliances report 1/100 °C and drift continuously even while switched
        off, which would otherwise publish every few seconds forever. The
        comparison is against the last *published* payload, not the last one
        seen, so slow drift still crosses the threshold eventually instead of
        creeping past it in sub-threshold steps.
        """
        if previous is None or previous.keys() != payload.keys():
            return True

        threshold = self._settings.temperature_min_delta_c
        for key, value in payload.items():
            other = previous[key]
            if (
                threshold > 0
                and key in TEMPERATURE_KEYS
                and isinstance(value, float)
                and isinstance(other, float)
            ):
                if abs(value - other) >= threshold:
                    return True
                continue
            if other != value:
                return True
        return False
