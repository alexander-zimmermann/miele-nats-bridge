"""Normalization tests built on the observed API payloads of the six appliances."""

from __future__ import annotations

from typing import Any

from miele_nats_bridge.normalize import normalize_eco, normalize_state

# Trimmed from a real GET /devices response with every appliance idle.
IDLE_OVEN: dict[str, Any] = {
    "ProgramID": {"value_raw": 0, "value_localized": "", "key_localized": "Programmbezeichnung"},
    "status": {"value_raw": 1, "value_localized": "Aus", "key_localized": "Status"},
    "programPhase": {"value_raw": 0, "value_localized": "", "key_localized": "Programmphase"},
    "remainingTime": [0, 0],
    "startTime": [0, 0],
    "elapsedTime": [0, 0],
    "targetTemperature": [{"value_raw": -32768, "value_localized": None, "unit": "Celsius"}],
    "coreTargetTemperature": [{"value_raw": -32768, "value_localized": None, "unit": "Celsius"}],
    "temperature": [],
    "coreTemperature": [{"value_raw": -32768, "value_localized": None, "unit": "Celsius"}],
    "signalInfo": False,
    "signalFailure": False,
    "signalDoor": False,
    "remoteEnable": {"fullRemoteControl": True, "smartGrid": False, "mobileStart": False},
    "light": None,
    "ecoFeedback": None,
}


def test_temperature_sentinel_is_dropped() -> None:
    out = normalize_state("backofen", IDLE_OVEN)
    for key in (
        "temperature_c",
        "target_temperature_c",
        "core_temperature_c",
        "core_target_temperature_c",
    ):
        assert key not in out


def test_idle_oven_reports_status_and_signals() -> None:
    out = normalize_state("backofen", IDLE_OVEN)
    assert out["status"] == 1
    assert out["status_name"] == "Aus"
    assert out["failure"] is False
    assert out["door_open"] is False
    assert out["remote_control"] is True
    assert out["mobile_start"] is False
    # An idle appliance reports program 0, which maps to the reserved "none" index.
    assert out["program"] == 0
    assert out["phase"] == 0
    assert "program_name" not in out


def test_localized_temperature_is_used_verbatim() -> None:
    state = dict(IDLE_OVEN)
    state["temperature"] = [{"value_raw": 2513, "value_localized": 25.13, "unit": "Celsius"}]
    out = normalize_state("tellerwaermer", state)
    assert out["temperature_c"] == 25.13


def test_raw_temperature_is_scaled_when_localized_missing() -> None:
    state = dict(IDLE_OVEN)
    state["temperature"] = [{"value_raw": 2513, "value_localized": None, "unit": "Celsius"}]
    out = normalize_state("tellerwaermer", state)
    assert out["temperature_c"] == 25.13


def test_durations_become_minutes() -> None:
    state = dict(IDLE_OVEN)
    state["remainingTime"] = [1, 45]
    state["elapsedTime"] = []
    out = normalize_state("backofen", state)
    assert out["remaining_minutes"] == 105
    # An empty array means unavailable and must not become 0.
    assert "elapsed_minutes" not in out


def test_program_name_loses_non_breaking_space() -> None:
    state = dict(IDLE_OVEN)
    state["ProgramID"] = {"value_raw": 24003, "value_localized": "Kaffee\xa0lang"}
    out = normalize_state("kaffeemaschine", state)
    assert out["program_name"] == "Kaffee lang"
    assert out["program_id"] == 24003
    assert out["program"] == 4


def test_unknown_program_maps_to_other() -> None:
    state = dict(IDLE_OVEN)
    state["ProgramID"] = {"value_raw": 999999, "value_localized": "Neu"}
    out = normalize_state("kaffeemaschine", state)
    assert out["program"] == 255
    assert out["program_id"] == 999999


def test_light_enum_becomes_boolean() -> None:
    state = dict(IDLE_OVEN)
    state["light"] = 1
    assert normalize_state("dampfgarer", state)["light"] is True
    state["light"] = 2
    assert normalize_state("dampfgarer", state)["light"] is False


def test_eco_is_empty_while_idle() -> None:
    assert normalize_eco(IDLE_OVEN) == {}


def test_eco_reports_energy_and_water() -> None:
    state = dict(IDLE_OVEN)
    state["ecoFeedback"] = {
        "currentEnergyConsumption": {"unit": "kWh", "value": 0.7},
        "currentWaterConsumption": {"unit": "l", "value": 11.5},
        "energyForecast": 0.6,
        "waterForecast": 0.5,
    }
    out = normalize_eco(state)
    assert out["energy_kwh"] == 0.7
    assert out["energy_kwh_unit"] == "kWh"
    assert out["water_l"] == 11.5
    assert out["energy_forecast"] == 0.6


def test_sentinel_delivered_through_value_localized_is_dropped() -> None:
    """How -32768 reached every archived core-temperature row for four appliances."""
    state = {
        **IDLE_OVEN,
        "coreTemperature": [{"value_raw": -3276800, "value_localized": -32768, "unit": "Celsius"}],
    }
    assert "core_temperature_c" not in normalize_state("backofen", state)


def test_temperature_below_absolute_zero_is_dropped() -> None:
    """Whatever encoding it arrives in, nothing colder than absolute zero is a reading."""
    state = {
        **IDLE_OVEN,
        "temperature": [{"value_raw": -32769, "value_localized": -327.68, "unit": "Celsius"}],
    }
    assert "temperature_c" not in normalize_state("backofen", state)


def test_real_readings_survive_the_floor() -> None:
    state = {
        **IDLE_OVEN,
        "temperature": [{"value_raw": 18030, "value_localized": 180.3, "unit": "Celsius"}],
    }
    assert normalize_state("backofen", state)["temperature_c"] == 180.3


def _status(raw: int, localized: str | None) -> dict[str, Any]:
    return {**IDLE_OVEN, "status": {"value_raw": raw, "value_localized": localized}}


def test_status_text_passes_short_names_through() -> None:
    out = normalize_state("backofen", _status(5, "In Betrieb"))
    assert out["status_text"] == "In Betrieb"
    assert out["status_name"] == "In Betrieb"


def test_status_text_shortens_what_dpt_16_cannot_carry() -> None:
    """These two are the only observed names above 14 characters."""
    assert normalize_state("geschirrspueler", _status(3, "Programm gewählt"))["status_text"] == (
        "Progr. gewählt"
    )
    assert normalize_state("backofen", _status(255, "nicht verbunden"))["status_text"] == "Getrennt"


def test_status_text_names_the_code_when_miele_supplies_no_text() -> None:
    """The coffee system reports 147 with an empty name; inventing a word would lie."""
    out = normalize_state("kaffeemaschine", _status(147, ""))
    assert out["status_text"] == "Code 147"
    assert "status_name" not in out


def test_every_status_text_fits_dpt_16_001() -> None:
    """The contract DPT 16.001 imposes: Latin-1 encodable, at most 14 bytes.

    Asserted directly rather than through xknx, which this bridge does not depend on.
    """
    from miele_nats_bridge.normalize import KNX_TEXT_MAX, STATUS_SHORT, _knx_text

    observed = [
        "Aus",
        "Bereit",
        "Programm gewählt",
        "In Betrieb",
        "Pause",
        "Ende",
        "Abbruch",
        "nicht verbunden",
        *STATUS_SHORT.values(),
    ]
    for name in observed:
        encoded = _knx_text(name).encode("latin-1")
        assert len(encoded) <= KNX_TEXT_MAX, name
