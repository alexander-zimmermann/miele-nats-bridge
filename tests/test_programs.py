"""Compaction table behaviour: stable indices, reserved values, unknown ids."""

from __future__ import annotations

from miele_nats_bridge.programs import (
    NONE,
    OTHER,
    PROGRAM_IDS,
    clean_name,
    compact_phase,
    compact_program,
)


def test_index_is_position_plus_one() -> None:
    assert compact_program("kaffeemaschine", 24000) == 1
    assert compact_program("kaffeemaschine", 24023) == 24
    assert compact_program("kaffeemaschine", 24050) == 25


def test_zero_and_none_map_to_reserved_none() -> None:
    assert compact_program("kaffeemaschine", 0) == NONE
    assert compact_program("kaffeemaschine", None) == NONE
    assert compact_phase("backofen", 0) == NONE


def test_unknown_id_and_unknown_slug_map_to_other() -> None:
    assert compact_program("kaffeemaschine", 24099) == OTHER
    assert compact_program("nichtvorhanden", 24000) == OTHER


def test_empty_table_maps_everything_to_other() -> None:
    # The four appliances whose program list is still pending must not silently
    # collapse onto a real index.
    assert compact_program("backofen", 3000) == OTHER


def test_indices_stay_below_the_other_sentinel() -> None:
    for slug, ids in PROGRAM_IDS.items():
        assert len(ids) < OTHER, f"{slug} table would collide with OTHER"
        assert len(set(ids)) == len(ids), f"{slug} table has duplicate ids"


def test_clean_name_normalizes_non_breaking_space() -> None:
    assert clean_name("Latte\xa0macchiato") == "Latte macchiato"
    assert clean_name("") == ""
    assert clean_name(None) == ""
