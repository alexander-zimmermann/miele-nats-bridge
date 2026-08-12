"""Normalize the Miele API dialect into flat scalar JSON for NATS consumers.

The knx-nats-bridge writer can only extract named scalar fields (no arrays, no
transforms), so every Miele quirk is resolved here:

- temperatures arrive as ``[{value_raw, value_localized, unit}, ...]`` arrays with
  **-32768 as the null sentinel**; the field is dropped rather than published as
  -327.68 °C, and ``value_localized`` is already in °C so no scaling is needed
- durations arrive as ``[hours, minutes]`` and become plain minutes (DPT 7.006)
- program and phase codes run far above the 0..255 of DPT 5.010 and are compacted
  to a stable index; the plain-text name travels alongside for archival
- ``signalFailure`` / ``signalInfo`` / ``signalDoor`` become named booleans
- ``remoteEnable.fullRemoteControl`` becomes ``remote_control``, which is the
  reason a command would be rejected and therefore worth showing in Basalte

Fields the appliance does not report are omitted entirely, so a consumer keeps
the last known value instead of seeing a null.
"""

from __future__ import annotations

from typing import Any

from .programs import clean_name, compact_phase, compact_program

# Miele reports "no value" for temperatures as -32768 rather than omitting the field.
TEMPERATURE_SENTINEL = -32768

# light: 1 = on, 2 = off. Any other value is left to the raw field alone.
_LIGHT_ON = 1
_LIGHT_OFF = 2


def _first(values: Any) -> dict[str, Any] | None:
    """First element of a Miele value array, or None when absent/empty."""
    if isinstance(values, list) and values and isinstance(values[0], dict):
        return values[0]
    return None


def _celsius(values: Any) -> float | None:
    """Read a temperature array's first entry, dropping the -32768 sentinel."""
    entry = _first(values)
    if entry is None:
        return None
    raw = entry.get("value_raw")
    if raw is None or raw == TEMPERATURE_SENTINEL:
        return None
    localized = entry.get("value_localized")
    if isinstance(localized, int | float):
        return float(localized)
    # Fall back to the raw 1/100 °C encoding when the API omits the localized form.
    return round(float(raw) / 100.0, 2)


def _minutes(value: Any) -> int | None:
    """Convert a Miele ``[hours, minutes]`` pair to minutes; [] means unavailable."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    hours, minutes = value
    if not isinstance(hours, int) or not isinstance(minutes, int):
        return None
    return hours * 60 + minutes


def _raw(field: Any) -> int | None:
    if isinstance(field, dict):
        raw = field.get("value_raw")
        if isinstance(raw, int):
            return raw
    return None


def _localized(field: Any) -> str | None:
    if isinstance(field, dict):
        value = field.get("value_localized")
        if isinstance(value, str) and value:
            return clean_name(value)
    return None


def normalize_state(slug: str, state: dict[str, Any]) -> dict[str, Any]:
    """Flat appliance-state payload for ``miele.<slug>.state``."""
    out: dict[str, Any] = {}

    # status fits DPT 5.010 unchanged (1 = off ... 146), so it is published raw.
    status = _raw(state.get("status"))
    if status is not None:
        out["status"] = status
    status_name = _localized(state.get("status"))
    if status_name:
        out["status_name"] = status_name

    # Program and phase: compact index for KNX, raw id and name for archival.
    program_raw = _raw(state.get("ProgramID"))
    if program_raw is not None:
        out["program_id"] = program_raw
        out["program"] = compact_program(slug, program_raw)
    program_name = _localized(state.get("ProgramID"))
    if program_name:
        out["program_name"] = program_name

    phase_raw = _raw(state.get("programPhase"))
    if phase_raw is not None:
        out["phase_id"] = phase_raw
        out["phase"] = compact_phase(slug, phase_raw)
    phase_name = _localized(state.get("programPhase"))
    if phase_name:
        out["phase_name"] = phase_name

    for key, source in (
        ("remaining_minutes", "remainingTime"),
        ("start_minutes", "startTime"),
        ("elapsed_minutes", "elapsedTime"),
    ):
        minutes = _minutes(state.get(source))
        if minutes is not None:
            out[key] = minutes

    for key, source in (
        ("temperature_c", "temperature"),
        ("target_temperature_c", "targetTemperature"),
        ("core_temperature_c", "coreTemperature"),
        ("core_target_temperature_c", "coreTargetTemperature"),
    ):
        celsius = _celsius(state.get(source))
        if celsius is not None:
            out[key] = celsius

    for key, source in (
        ("failure", "signalFailure"),
        ("info", "signalInfo"),
        ("door_open", "signalDoor"),
    ):
        value = state.get(source)
        if isinstance(value, bool):
            out[key] = value

    remote = state.get("remoteEnable")
    if isinstance(remote, dict):
        for key, source in (
            ("remote_control", "fullRemoteControl"),
            ("mobile_start", "mobileStart"),
        ):
            value = remote.get(source)
            if isinstance(value, bool):
                out[key] = value

    light = state.get("light")
    if isinstance(light, int):
        out["light_raw"] = light
        if light in (_LIGHT_ON, _LIGHT_OFF):
            out["light"] = light == _LIGHT_ON

    return out


def normalize_eco(state: dict[str, Any]) -> dict[str, Any]:
    """Flat ecoFeedback payload for ``miele.<slug>.eco``; empty while idle.

    The API reports null outside a running or just-finished programme, which is a
    normal state rather than an error — the caller skips publishing an empty dict.
    """
    eco = state.get("ecoFeedback")
    if not isinstance(eco, dict):
        return {}

    out: dict[str, Any] = {}
    for key, source in (
        ("energy_kwh", "currentEnergyConsumption"),
        ("water_l", "currentWaterConsumption"),
    ):
        entry = eco.get(source)
        if isinstance(entry, dict):
            value = entry.get("value")
            if isinstance(value, int | float):
                out[key] = float(value)
            unit = entry.get("unit")
            if isinstance(unit, str) and unit:
                out[f"{key}_unit"] = unit

    for key, source in (
        ("energy_forecast", "energyForecast"),
        ("water_forecast", "waterForecast"),
    ):
        value = eco.get(source)
        if isinstance(value, int | float):
            out[key] = float(value)

    return out
