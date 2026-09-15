"""Unit tests for appliance loading from YAML."""

from __future__ import annotations

import textwrap
from pathlib import Path

from miele_nats_bridge.config import ApplianceConfig, Settings


def test_ga_name_binding_is_optional() -> None:
    assert ApplianceConfig(device_id="000105454657", name="geschirrspueler").ga_name == ""


def test_load_appliances_carries_ga_name_binding(tmp_path: Path) -> None:
    # lares binds the appliance to its group-address name prefix in this very
    # file; the bridge has to let the key through, or the ConfigMap that
    # carries the binding kills the pod.
    path = tmp_path / "appliances.yaml"
    path.write_text(
        textwrap.dedent(
            """
            appliances:
              - device_id: "000105454657"
                name: geschirrspueler
                model: G7560
                ga_name: Haushaltstechnik.Geschirrspüler
            """
        )
    )
    (appliance,) = Settings(miele_appliances_file=path).load_appliances()
    assert appliance.ga_name == "Haushaltstechnik.Geschirrspüler"
