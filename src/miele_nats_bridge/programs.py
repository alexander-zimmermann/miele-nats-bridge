"""Fitting Miele program and phase codes into the 0..255 range of DPT 5.010.

Miele numbers each appliance family in its own block. Most blocks are small
enough to pass through untouched — the dishwasher uses 1..44, the ovens 6..75 —
so the value on the KNX bus stays identical to the one in Miele's own
documentation, and a firmware update that adds an operating mode shows up with a
stable number of its own instead of forcing a renumbering in Basalte.

Only blocks that start beyond 255 need shifting, which one subtrahend per
appliance expresses completely. No lookup tables, and therefore no ordering to
preserve.

The raw code and the plain-text name are published to NATS unshifted; only the
KNX side sees the compacted value.
"""

from __future__ import annotations

# 0 is reserved for "no program", which is what an idle appliance reports.
NONE = 0
OTHER = 255

# Subtrahend per appliance slug. 0 passes the raw id through unchanged.
#
# The coffee system numbers its beverages from 24000, so it subtracts 23999
# rather than 24000: that puts the first beverage at 1 and keeps 0 reserved.
PROGRAM_OFFSET: dict[str, int] = {
    "geschirrspueler": 0,  # observed 1..44
    "backofen": 0,  # observed 6..31
    "tellerwaermer": 0,  # no programs at all
    "mikrowelle": 0,  # observed 6..31
    "kaffeemaschine": 23999,  # observed 24000..24050
    "dampfgarer": 0,  # observed 6..75
}

# Same mechanism for programPhase, whose blocks all start well beyond 255.
#
# Only two block starts are confirmed by observation so far. The ovens keep a
# zero offset, which reports OTHER for their phases — honest until a real run
# reveals where their block begins.
PHASE_OFFSET: dict[str, int] = {
    "geschirrspueler": 1791,  # block starts at 1792
    "kaffeemaschine": 4351,  # block starts at 4352
    "backofen": 0,  # unconfirmed
    "mikrowelle": 0,  # unconfirmed
    "dampfgarer": 0,  # unconfirmed
    "tellerwaermer": 0,  # no programs, no phases
}


def _compact(offsets: dict[str, int], slug: str, raw: int | None) -> int:
    if raw is None or raw == 0:
        return NONE
    offset = offsets.get(slug)
    if offset is None:
        return OTHER
    value = raw - offset
    # Anything outside the appliance's own block lands here, e.g. the coffee
    # system's maintenance cycles, which are numbered in a different range.
    return value if 0 < value < OTHER else OTHER


def compact_program(slug: str, raw: int | None) -> int:
    """Shift a raw ProgramID into 1..254, or OTHER when it falls outside the block."""
    return _compact(PROGRAM_OFFSET, slug, raw)


def compact_phase(slug: str, raw: int | None) -> int:
    """Shift a raw programPhase into 1..254, or OTHER when it falls outside the block."""
    return _compact(PHASE_OFFSET, slug, raw)


def clean_name(value: str | None) -> str:
    """Strip the non-breaking spaces Miele embeds in localized program names."""
    if not value:
        return ""
    return value.replace("\xa0", " ").strip()
