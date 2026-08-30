"""Change-filter tests: what actually reaches NATS and what is suppressed."""

from __future__ import annotations

from typing import Any, cast

from nats_bridge_core import Publisher

from miele_nats_bridge.bridge import MieleBridge
from miele_nats_bridge.client import MieleClient
from miele_nats_bridge.config import ApplianceConfig, Settings
from miele_nats_bridge.metrics import Metrics

# The Tellerwärmer switched off, drifting: this is the payload that produced 69 %
# of all archived rows before the threshold existed.
IDLE_WARMER: dict[str, Any] = {
    "status": 1,
    "status_name": "Aus",
    "temperature_c": 28.31,
    "failure": False,
    "info": False,
    "remote_control": True,
}


class _FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, dict[str, Any]]] = []

    def enqueue(self, ctx: object, subject: str, payload: dict[str, Any]) -> bool:
        self.calls.append((ctx, subject, payload))
        return True

    @property
    def sent(self) -> list[dict[str, Any]]:
        return [payload for *_, payload in self.calls]


def _bridge(threshold: float = 0.5) -> tuple[MieleBridge, _FakePublisher]:
    publisher = _FakePublisher()
    bridge = MieleBridge(
        settings=Settings(temperature_min_delta_c=threshold),
        appliances=[ApplianceConfig(device_id="000175853376", name="tellerwaermer")],
        client=cast(MieleClient, None),
        publisher=cast(Publisher, publisher),
        metrics=cast(Metrics, None),
    )
    return bridge, publisher


def _feed(bridge: MieleBridge, payload: dict[str, Any]) -> None:
    appliance = bridge._by_id["000175853376"]
    bridge._publish_if_changed(appliance, "state", appliance.state_subject, payload)


def test_first_payload_is_published() -> None:
    bridge, publisher = _bridge()
    _feed(bridge, IDLE_WARMER)
    assert publisher.calls == [
        (("tellerwaermer", "state"), "miele.tellerwaermer.state", IDLE_WARMER)
    ]


def test_identical_payload_is_suppressed() -> None:
    bridge, publisher = _bridge()
    _feed(bridge, IDLE_WARMER)
    _feed(bridge, dict(IDLE_WARMER))
    assert len(publisher.sent) == 1


def test_sub_threshold_drift_is_suppressed() -> None:
    bridge, publisher = _bridge()
    _feed(bridge, IDLE_WARMER)
    for celsius in (28.30, 28.25, 28.20, 28.11):
        _feed(bridge, {**IDLE_WARMER, "temperature_c": celsius})
    assert len(publisher.sent) == 1


def test_drift_publishes_once_it_crosses_the_threshold() -> None:
    """Sub-threshold steps must accumulate against the last *published* value."""
    bridge, publisher = _bridge()
    _feed(bridge, IDLE_WARMER)
    for celsius in (28.10, 27.90, 27.81):
        _feed(bridge, {**IDLE_WARMER, "temperature_c": celsius})
    assert [p["temperature_c"] for p in publisher.sent] == [28.31, 27.81]


def test_non_temperature_change_always_publishes() -> None:
    bridge, publisher = _bridge()
    _feed(bridge, IDLE_WARMER)
    _feed(bridge, {**IDLE_WARMER, "status": 5, "status_name": "In Betrieb"})
    assert len(publisher.sent) == 2


def test_appearing_field_publishes() -> None:
    """An oven switching on adds temperature_c, which the key comparison catches."""
    bridge, publisher = _bridge()
    _feed(bridge, IDLE_WARMER)
    _feed(bridge, {**IDLE_WARMER, "target_temperature_c": 75.0})
    assert len(publisher.sent) == 2


def test_threshold_zero_restores_exact_comparison() -> None:
    bridge, publisher = _bridge(threshold=0.0)
    _feed(bridge, IDLE_WARMER)
    _feed(bridge, {**IDLE_WARMER, "temperature_c": 28.30})
    assert len(publisher.sent) == 2


def test_empty_payload_is_never_published() -> None:
    bridge, publisher = _bridge()
    _feed(bridge, {})
    assert publisher.sent == []
