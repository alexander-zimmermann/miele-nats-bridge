"""Offset compaction: passthrough where the block fits, shift where it does not."""

from __future__ import annotations

import pytest

from miele_nats_bridge.programs import (
    NONE,
    OTHER,
    PHASE_OFFSET,
    PROGRAM_OFFSET,
    clean_name,
    compact_phase,
    compact_program,
)


def test_small_blocks_pass_through_unchanged() -> None:
    # The value on the bus stays the one from Miele's own documentation.
    assert compact_program("geschirrspueler", 3) == 3  # ECO
    assert compact_program("geschirrspueler", 44) == 44  # PowerWash
    assert compact_program("backofen", 13) == 13  # Heißluft plus
    assert compact_program("dampfgarer", 72) == 72  # Sous-vide


def test_coffee_block_is_shifted_off_zero() -> None:
    # 24000 must not land on 0, which means "no program".
    assert compact_program("kaffeemaschine", 24000) == 1
    assert compact_program("kaffeemaschine", 24023) == 24
    assert compact_program("kaffeemaschine", 24050) == 51


def test_zero_and_none_map_to_reserved_none() -> None:
    assert compact_program("kaffeemaschine", 0) == NONE
    assert compact_program("geschirrspueler", None) == NONE
    assert compact_phase("geschirrspueler", 0) == NONE


def test_value_outside_the_block_maps_to_other() -> None:
    # The coffee system reports maintenance cycles from a different range.
    assert compact_program("kaffeemaschine", 17004) == OTHER
    # An oven id beyond the DPT range cannot be represented either.
    assert compact_program("backofen", 3073) == OTHER
    assert compact_program("unbekanntes-geraet", 3) == OTHER


def test_phase_blocks_are_shifted() -> None:
    assert compact_phase("geschirrspueler", 1792) == 1
    assert compact_phase("geschirrspueler", 1799) == 8
    assert compact_phase("kaffeemaschine", 4352) == 1


def test_codes_outside_the_block_report_other() -> None:
    # 3078 belongs to the oven block; the steam oven blocks elsewhere entirely.
    assert compact_phase("dampfgarer", 3078) == OTHER
    assert compact_phase("geschirrspueler", 4352) == OTHER


def test_every_appliance_has_both_offsets() -> None:
    assert PROGRAM_OFFSET.keys() == PHASE_OFFSET.keys()
    # A zero phase offset means the block start was never confirmed, which is
    # how three appliances reported nothing but OTHER for a week.
    assert all(offset > 0 for offset in PHASE_OFFSET.values())


def test_clean_name_normalizes_non_breaking_space() -> None:
    assert clean_name("Latte\xa0macchiato") == "Latte macchiato"
    assert clean_name("") == ""
    assert clean_name(None) == ""


# Phase block starts, each confirmed against archived runs of the real appliance.
# Miele blocks phases at deviceType * 256; the Tellerwärmer reports out of the
# oven's block despite being deviceType 25, so the table is observed, not derived.
OBSERVED_PHASES = [
    ("geschirrspueler", 1792, 1),
    ("geschirrspueler", 1794, 3),
    ("geschirrspueler", 1800, 9),
    ("backofen", 3073, 2),
    ("backofen", 3078, 7),
    ("backofen", 3084, 13),
    ("tellerwaermer", 3073, 2),
    ("tellerwaermer", 3094, 23),
    ("mikrowelle", 3330, 3),
    ("mikrowelle", 3334, 7),
    ("kaffeemaschine", 4352, 1),
    ("kaffeemaschine", 4405, 54),
    ("dampfgarer", 7938, 3),
    ("dampfgarer", 7961, 26),
]


@pytest.mark.parametrize(("slug", "raw", "expected"), OBSERVED_PHASES)
def test_observed_phase_codes_compact_into_range(slug: str, raw: int, expected: int) -> None:
    assert compact_phase(slug, raw) == expected


def test_runtime_only_program_ids_still_report_other() -> None:
    """Scattered runtime ids have no block, so OTHER is the honest answer."""
    for slug, raw in (
        ("kaffeemaschine", 17004),
        ("kaffeemaschine", 24787),
        ("dampfgarer", 333),
        ("dampfgarer", 2028),
        ("dampfgarer", 2048),
    ):
        assert compact_program(slug, raw) == OTHER
