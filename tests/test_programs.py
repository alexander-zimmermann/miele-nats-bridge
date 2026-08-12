"""Offset compaction: passthrough where the block fits, shift where it does not."""

from __future__ import annotations

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


def test_unconfirmed_phase_blocks_report_other() -> None:
    # The oven phase block start is unknown, so its codes must not be invented.
    assert compact_phase("backofen", 3073) == OTHER
    assert compact_phase("dampfgarer", 3078) == OTHER


def test_every_appliance_has_both_offsets() -> None:
    assert PROGRAM_OFFSET.keys() == PHASE_OFFSET.keys()


def test_clean_name_normalizes_non_breaking_space() -> None:
    assert clean_name("Latte\xa0macchiato") == "Latte macchiato"
    assert clean_name("") == ""
    assert clean_name(None) == ""
