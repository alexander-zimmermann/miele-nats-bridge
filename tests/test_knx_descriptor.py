"""The shipped knx.yaml names only fields the bridge actually publishes."""

from __future__ import annotations

from typing import Any

from nats_bridge_core import knx_descriptor
from test_normalize import IDLE_OVEN

from miele_nats_bridge.normalize import normalize_state

# IDLE_OVEN with every KNX-relevant reading present: programme, both probes, finished signal
RUNNING_OVEN: dict[str, Any] = {
    **IDLE_OVEN,
    "ProgramID": {"value_raw": 6, "value_localized": "Heißluft plus"},
    "status": {"value_raw": 5, "value_localized": "In Betrieb"},
    "programPhase": {"value_raw": 3073, "value_localized": "Aufheizen"},
    "remainingTime": [1, 5],
    "temperature": [{"value_raw": 18030, "value_localized": 180.3, "unit": "Celsius"}],
    "coreTemperature": [{"value_raw": 6200, "value_localized": 62.0, "unit": "Celsius"}],
    "signalInfo": True,
}


def test_every_descriptor_field_is_a_published_state_field() -> None:
    descriptor = knx_descriptor.load_package("miele_nats_bridge")
    payload = normalize_state("backofen", RUNNING_OVEN)

    assert list(descriptor.subjects) == ["state"]
    missing = [name for name in descriptor.subjects["state"].fields if name not in payload]
    assert missing == []
