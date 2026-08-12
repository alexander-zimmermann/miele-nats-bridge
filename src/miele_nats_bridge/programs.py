"""Compaction of Miele program and phase codes into the 0..255 range of DPT 5.010.

Miele program ids are namespaced per appliance type and run far above 255 — the
coffee system alone uses 24000..24050. KNX group addresses cannot carry that, so
each appliance gets an ordered tuple of the raw ids it can report and the compact
index is the position in that tuple, starting at 1.

Two rules keep the index stable for Basalte, which stores it per scene:

- never reorder or remove an entry; append new ids at the end
- an id missing from the tuple maps to OTHER, never to a neighbouring index

The raw ids come from ``GET /v1/devices/{id}/programs``, which the appliance only
answers while it is switched on. The plain-text program name is published to NATS
and archived unchanged; only the KNX side sees the compact index.
"""

from __future__ import annotations

# 0 is reserved for "no program", which is what an idle appliance reports.
NONE = 0
OTHER = 255

# Ordered raw program ids per appliance slug; compact index = position + 1.
PROGRAM_IDS: dict[str, tuple[int, ...]] = {
    # CVA7845, from GET /devices/{id}/programs. 24050 is reported without a name.
    "kaffeemaschine": (
        24000,  # Ristretto
        24001,  # Espresso
        24002,  # Kaffee
        24003,  # Kaffee lang
        24004,  # Cappuccino
        24005,  # Cappuccino Italiano
        24006,  # Latte macchiato
        24007,  # Espresso macchiato
        24008,  # Cafè au lait
        24009,  # Caffè latte
        24010,  # Caffè Americano
        24011,  # Long black
        24012,  # Flat white
        24013,  # Heißwasser
        24014,  # Warmwasser
        24015,  # Heiße Milch
        24016,  # Milchschaum
        24017,  # Schwarzer Tee
        24018,  # Kräutertee
        24019,  # Früchtetee
        24020,  # Grüner Tee
        24021,  # Weißer Tee
        24022,  # Japan Tee
        24023,  # Chai Latte
        24050,
    ),
    # The warming drawer has no programs at all; /programs returns an empty list.
    "tellerwaermer": (),
    # Pending: these four answer /programs only while switched on.
    "geschirrspueler": (),
    "backofen": (),
    "mikrowelle": (),
    "dampfgarer": (),
}

# Ordered raw programPhase codes per appliance slug; same indexing rule.
# Miele blocks phase codes per device type well above 255, so they need the same
# treatment as program ids. Populated from observed runs.
PHASE_IDS: dict[str, tuple[int, ...]] = {
    "geschirrspueler": (),
    "backofen": (),
    "tellerwaermer": (),
    "mikrowelle": (),
    "kaffeemaschine": (),
    "dampfgarer": (),
}


def _compact(table: dict[str, tuple[int, ...]], slug: str, raw: int | None) -> int:
    if raw is None or raw == 0:
        return NONE
    try:
        index = table[slug].index(raw) + 1
    except KeyError, ValueError:
        return OTHER
    # A table longer than 254 entries would collide with OTHER; report OTHER
    # rather than silently aliasing two programs onto the same index.
    return index if index < OTHER else OTHER


def compact_program(slug: str, raw: int | None) -> int:
    """Map a raw ProgramID to its stable 1..254 index, or OTHER when unknown."""
    return _compact(PROGRAM_IDS, slug, raw)


def compact_phase(slug: str, raw: int | None) -> int:
    """Map a raw programPhase to its stable 1..254 index, or OTHER when unknown."""
    return _compact(PHASE_IDS, slug, raw)


def clean_name(value: str | None) -> str:
    """Strip the non-breaking spaces Miele embeds in localized program names."""
    if not value:
        return ""
    return value.replace("\xa0", " ").strip()
