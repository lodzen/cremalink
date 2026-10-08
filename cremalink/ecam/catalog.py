"""
Machine recipe catalogue — parse the recipe-bearing blob families.

Ported from ``delonghi_coffeelink``'s ``catalog.py`` (same authorial
grammar, pure Python — no HA, no I/O).

Every recipe-bearing datapoint is a self-describing binary blob::

    d0 <len> <family 2B> [<profile 1B>] <bevid 1B> <TLV params…> <crc16>

The CRC is the same CRC16 AUG-CCITT the command builder uses, so a blob
can be *proved* intact before anything is derived from it. Families:

===========  ==============================================================
family       meaning
===========  ==============================================================
``b0 f0``    factory descriptor — the capability schema (min/default/max)
``a6 f0``    per-profile instance — the current value of each parameter
``a8 f0``    a per-profile ordered short list of beverage ids
``a4 f0``    profile names (UTF-16BE, user-entered)
``aa f0``    custom-recipe slot names
``ba f0``    bean-system names
===========  ==============================================================

Rules that make it parseable without per-beverage special cases:

- **TLV**: tags ``0x01``, ``0x09`` and ``0x0F`` carry a 16-bit big-endian
  value, every other tag carries 8 bits. In a ``b0f0`` descriptor the
  same tags carry a *triple* (min, default, max).
- **Parse by family, never by property name.** Datapoint numbering
  differs per model; the blob header is identical.

Deliberate refusals (a beverage that lies is worse than one missing):

- A blob failing any gate (prefix, declared length, CRC) is *unreadable*,
  never *absent*.
- A TLV walk leaving a trailing byte returns ``None`` — a partial parse
  could build a wrong brew command, so the whole walk is refused.
- ``a8f0`` lists are per-profile recency orderings, NOT the brewable set.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from cremalink.core.binary import crc16_ccitt

BLOB_PREFIX = 0xD0

FAMILY_DESCRIPTOR = bytes([0xB0, 0xF0])
FAMILY_PROFILE_RECIPE = bytes([0xA6, 0xF0])
FAMILY_PRIORITY = bytes([0xA8, 0xF0])
FAMILY_PROFILE_NAMES = bytes([0xA4, 0xF0])
FAMILY_CUSTOM_NAMES = bytes([0xAA, 0xF0])
FAMILY_BEAN_NAMES = bytes([0xBA, 0xF0])

FAMILY_LABELS = {
    FAMILY_DESCRIPTOR: "factory_descriptor",
    FAMILY_PROFILE_RECIPE: "profile_recipe",
    FAMILY_PRIORITY: "priority_list",
    FAMILY_PROFILE_NAMES: "profile_names",
    FAMILY_CUSTOM_NAMES: "custom_names",
    FAMILY_BEAN_NAMES: "bean_names",
}

#: Families carrying user-entered names — diagnostics redact their content.
TEXT_BLOB_FAMILIES = frozenset(
    {FAMILY_PROFILE_NAMES, FAMILY_CUSTOM_NAMES, FAMILY_BEAN_NAMES}
)

#: TLV tags whose value is 16-bit big-endian; every other tag is 8-bit.
WIDE_TAGS = frozenset({0x01, 0x09, 0x0F})

#: Beverage-id ranges that are not one of the built-in drinks.
CUSTOM_SLOT_IDS = frozenset(range(0xE6, 0xEC))
BEAN_SYSTEM_IDS = frozenset(range(0xC8, 0xCE))

TAG_COFFEE_ML = 0x01
TAG_MILK = 0x09
TAG_WATER_ML = 0x0F
TAG_TEMPERATURE = 0x1B
TAG_CUP_COUNT = 0x1E
TAG_INTENSITY = 0x02

TAG_NAMES = {
    TAG_COFFEE_ML: "coffee_ml",
    TAG_MILK: "milk",
    TAG_WATER_ML: "water_ml",
    TAG_TEMPERATURE: "temperature",
    TAG_CUP_COUNT: "cup_count",
    TAG_INTENSITY: "intensity",
}

#: Smallest payload each family can carry and still be indexable.
_MIN_PAYLOAD = {
    FAMILY_DESCRIPTOR: 1,
    FAMILY_PROFILE_RECIPE: 2,
    FAMILY_PRIORITY: 1,
    FAMILY_PROFILE_NAMES: 2,
    FAMILY_CUSTOM_NAMES: 2,
    FAMILY_BEAN_NAMES: 2,
}


@dataclass(frozen=True)
class Beverage:
    """One catalogue beverage."""

    bev_id: int
    kind: str  # "builtin" | "custom" | "bean_system"
    declared: bool = False
    ranges: dict = field(default_factory=dict)  # tag -> (min, default, max)
    profiles: dict = field(default_factory=dict)  # profile -> {tag: value}
    in_priority_list: bool | None = None
    priority_position: dict = field(default_factory=dict)
    defined: bool | None = None  # custom slot actually programmed
    profiles_differ: bool = False
    off_default: dict | None = None
    source_properties: tuple = ()
    # Filled from the app's model table (``source == "model_table"``) only.
    name: str | None = None
    ingredients: tuple[int, ...] = ()  # parameter ids the drink uses
    use_for_custom: bool | None = None


@dataclass(frozen=True)
class RecipeCatalogue:
    """The parsed recipe catalogue — advisory only; brew never depends on it."""

    fingerprint: tuple
    source: str  # "machine" | "model_table" | "empty"
    beverages: dict[int, Beverage]
    priority_lists: dict[int, list[int]]
    names: dict[str, dict[int, str]]
    stats: dict


def _b64_bytes(value: Any) -> bytes | None:
    """Decode a datapoint value to bytes, tolerating a truncated dump."""
    if not isinstance(value, str):
        return None
    clean = "".join(value.split())
    if not clean:
        return None
    try:
        return base64.b64decode(clean[: len(clean) // 4 * 4], validate=True)
    except (ValueError, binascii.Error):
        return None


def blob_family(value: Any) -> bytes | None:
    """The family bytes of a machine blob, read without decoding all of it."""
    if not isinstance(value, str):
        return None
    head = "".join(value.split())[:8]
    if len(head) < 8:
        return None
    try:
        raw = base64.b64decode(head, validate=True)
    except (ValueError, binascii.Error):
        return None
    return bytes(raw[2:4]) if raw[0] == BLOB_PREFIX else None


def fingerprint(properties: dict) -> tuple:
    """A change-detection key over catalogue datapoints, and only those."""
    return tuple(
        (name, "".join(value.split()))
        for name in sorted(properties)
        for value in (
            properties[name].get("value")
            if isinstance(properties[name], dict)
            else properties[name],
        )
        if isinstance(value, str) and blob_family(value) in FAMILY_LABELS
    )


def decode_blob(value: Any) -> dict | None:
    """Classify one datapoint value as a machine blob, or ``None``.

    Three gates: ``0xd0`` prefix, declared length, CRC. The length gate is
    ``declared <= len(raw)``: the machine appends a 4-byte timestamp to
    some datapoints, and strict equality misgrades those live channels as
    truncated.
    """
    raw = _b64_bytes(value)
    if raw is None or len(raw) < 6 or raw[0] != BLOB_PREFIX:
        return None
    declared = raw[1] + 1
    family = bytes(raw[2:4])
    out: dict[str, Any] = {
        "raw": raw,
        "family": family,
        "family_label": FAMILY_LABELS.get(family, family.hex(" ")),
        "declared_length": declared,
        "length": len(raw),
    }
    if declared > len(raw):
        out.update(truncated=True, crc_valid=False, frame=None, payload=None)
        return out
    frame = raw[:declared]
    crc_valid = crc16_ccitt(frame[:-2]) == frame[-2:]
    out.update(
        truncated=False,
        crc_valid=crc_valid,
        frame=frame,
        trailer=raw[declared:],
        payload=frame[4:-2],
    )
    return out


def parse_tlv(payload: bytes) -> dict[int, int] | None:
    """Parse a TLV parameter payload; ``None`` if it does not consume exactly."""
    out: dict[int, int] = {}
    i = 0
    end = len(payload)
    while i < end:
        tag = payload[i]
        width = 2 if tag in WIDE_TAGS else 1
        if i + 1 + width > end:
            return None
        out[tag] = int.from_bytes(payload[i + 1 : i + 1 + width], "big")
        i += 1 + width
    return out


def parse_triples(payload: bytes) -> dict[int, tuple[int, int, int]] | None:
    """Parse a factory descriptor into ``tag -> (min, default, max)``."""
    out: dict[int, tuple[int, int, int]] = {}
    i = 0
    end = len(payload)
    while i < end:
        tag = payload[i]
        width = 2 if tag in WIDE_TAGS else 1
        if i + 1 + 3 * width > end:
            return None
        out[tag] = tuple(
            int.from_bytes(payload[i + 1 + n * width : i + 1 + (n + 1) * width], "big")
            for n in range(3)
        )
        i += 1 + 3 * width
    return out


def decode_text(payload: bytes) -> str | None:
    """Decode the first UTF-16BE name in a text blob payload."""
    try:
        text = payload.decode("utf-16-be", errors="ignore")
    except (UnicodeDecodeError, ValueError):
        return None
    name = text.split("\x00", 1)[0].strip()
    return name or None


def iter_blobs(properties: dict) -> Iterator[tuple[str, dict]]:
    """Yield ``(property_name, decoded_blob)`` for every machine blob."""
    for name in sorted(properties):
        prop = properties.get(name)
        value = prop.get("value") if isinstance(prop, dict) else prop
        blob = decode_blob(value)
        if blob is not None:
            yield name, blob


def _slot_is_defined(params: dict[int, int]) -> bool:
    """True when a custom slot is actually programmed (dispenses anything)."""
    return any(params.get(tag) for tag in (TAG_COFFEE_ML, TAG_MILK, TAG_WATER_ML))


def build_catalog(properties: dict) -> RecipeCatalogue:
    """Build the machine's beverage catalogue from property values."""
    beverages: dict[int, dict] = {}
    priority: dict[int, list[int]] = {}
    names: dict[str, dict[int, str]] = {"profiles": {}, "custom": {}, "beans": {}}
    stats = {
        "blobs": 0,
        "crc_ok": 0,
        "crc_failed": 0,
        "truncated": 0,
        "unparsed_payloads": 0,
        "short_payloads": 0,
        "families": {},
    }

    def entry(bev_id: int) -> dict:
        if bev_id not in beverages:
            if bev_id in CUSTOM_SLOT_IDS:
                kind = "custom"
            elif bev_id in BEAN_SYSTEM_IDS:
                kind = "bean_system"
            else:
                kind = "builtin"
            beverages[bev_id] = {
                "id": bev_id,
                "kind": kind,
                "declared": False,
                "ranges": {},
                "profiles": {},
                "in_priority_list": None,
                "priority_position": {},
                "defined": None,
                "profiles_differ": False,
                "off_default": None,
                "source_properties": [],
            }
        return beverages[bev_id]

    for name, blob in iter_blobs(properties):
        stats["blobs"] += 1
        label = blob["family_label"]
        stats["families"][label] = stats["families"].get(label, 0) + 1
        if blob["truncated"]:
            stats["truncated"] += 1
            # A truncated descriptor still proves the drink exists.
            if blob["family"] == FAMILY_DESCRIPTOR and blob["length"] >= 5:
                item = entry(blob["raw"][4])
                item["declared"] = True
                item["source_properties"].append(name)
            continue
        if not blob["crc_valid"]:
            stats["crc_failed"] += 1
            continue
        stats["crc_ok"] += 1
        family, payload = blob["family"], blob["payload"]
        if len(payload) < _MIN_PAYLOAD.get(family, 0):
            stats["short_payloads"] += 1
            continue

        if family == FAMILY_DESCRIPTOR:
            bev_id, body = payload[0], payload[1:]
            item = entry(bev_id)
            item["declared"] = True
            item["source_properties"].append(name)
            triples = parse_triples(body)
            if triples is None:
                stats["unparsed_payloads"] += 1
            else:
                item["ranges"] = triples
        elif family == FAMILY_PROFILE_RECIPE:
            profile, bev_id, body = payload[0], payload[1], payload[2:]
            item = entry(bev_id)
            item["source_properties"].append(name)
            params = parse_tlv(body)
            if params is None:
                stats["unparsed_payloads"] += 1
            else:
                item["profiles"][profile] = params
        elif family == FAMILY_PRIORITY:
            # payload = <profile> <bevid…>; NO header byte after profile.
            priority[payload[0]] = list(payload[1:])
        elif family in (FAMILY_PROFILE_NAMES, FAMILY_CUSTOM_NAMES, FAMILY_BEAN_NAMES):
            bucket = {
                FAMILY_PROFILE_NAMES: "profiles",
                FAMILY_CUSTOM_NAMES: "custom",
                FAMILY_BEAN_NAMES: "beans",
            }[family]
            text = decode_text(payload[2:])
            if text:
                names[bucket][payload[0]] = text

    _apply_priority(beverages, priority)
    for item in beverages.values():
        _finalize(item)

    beverage_objs = {
        bev_id: Beverage(
            bev_id=item["id"],
            kind=item["kind"],
            declared=item["declared"],
            ranges=item["ranges"],
            profiles=item["profiles"],
            in_priority_list=item["in_priority_list"],
            priority_position=item["priority_position"],
            defined=item["defined"],
            profiles_differ=item["profiles_differ"],
            off_default=item["off_default"],
            source_properties=tuple(item["source_properties"]),
        )
        for bev_id, item in beverages.items()
    }

    return RecipeCatalogue(
        fingerprint=fingerprint(properties),
        source="machine" if beverage_objs else "empty",
        beverages=beverage_objs,
        priority_lists=priority,
        names=names,
        stats=stats,
    )


def _apply_priority(beverages: dict[int, dict], priority: dict[int, list[int]]) -> None:
    """Record which drinks appear in each profile's ordered short list."""
    if not priority:
        return
    listed = {bev_id for ids in priority.values() for bev_id in ids}
    for profile, ids in priority.items():
        for position, bev_id in enumerate(ids):
            item = beverages.setdefault(
                bev_id,
                {
                    "id": bev_id,
                    "kind": (
                        "custom"
                        if bev_id in CUSTOM_SLOT_IDS
                        else "bean_system"
                        if bev_id in BEAN_SYSTEM_IDS
                        else "builtin"
                    ),
                    "declared": False,
                    "ranges": {},
                    "profiles": {},
                    "in_priority_list": None,
                    "priority_position": {},
                    "defined": None,
                    "profiles_differ": False,
                    "off_default": None,
                    "source_properties": [],
                },
            )
            item["priority_position"][profile] = position
    for item in beverages.values():
        item["in_priority_list"] = item["id"] in listed


def _finalize(item: dict) -> None:
    """Derive the customisation signals once all blobs have been folded in."""
    profiles = item["profiles"]
    if item["kind"] == "custom" and profiles:
        item["defined"] = any(_slot_is_defined(params) for params in profiles.values())

    if len(profiles) > 1:
        tags = {tag for params in profiles.values() for tag in params}
        item["profiles_differ"] = any(
            len({params.get(tag) for params in profiles.values()}) > 1 for tag in tags
        )

    # Only a readable factory descriptor can say whether a value changed.
    if item["ranges"] and profiles:
        off_default: dict[int, bool] = {}
        for tag, triple in item["ranges"].items():
            default = triple[1]
            values = [params[tag] for params in profiles.values() if tag in params]
            if values:
                off_default[tag] = any(value != default for value in values)
        item["off_default"] = off_default


def catalog_beverage_ids(catalog: RecipeCatalogue | None) -> set[int]:
    """Every beverage id the machine declared, whatever its kind."""
    if catalog is None:
        return set()
    return {bev_id for bev_id, item in catalog.beverages.items() if item.declared}


def _bev_kind(bev_id: int) -> str:
    if bev_id in CUSTOM_SLOT_IDS:
        return "custom"
    return "bean_system" if bev_id in BEAN_SYSTEM_IDS else "builtin"


def load_model_declaration(model: str | None) -> dict | None:
    """The app's own recipe declaration for an ``appModelId``, or ``None``.

    Source: ``MachinesModels.json`` (Coffee Link 4.9.6) — shipped as
    ``resources/model_recipes.json`` for the Wi-Fi families only
    (``PD_SOUL``, ``PD_SOUL_BETTER``, ``STRIKER_BEST``). ``STRIKER_GOOD``
    declares no recipes in the table, so it has no entry.
    """
    if not model:
        return None
    table = json.loads(
        resources.files("cremalink.resources")
        .joinpath("model_recipes.json")
        .read_text(encoding="utf-8")
    )
    return table.get(model)


def build_declared_catalog(model: str | None) -> RecipeCatalogue:
    """Build an advisory catalogue from the app's model table.

    Carries what the table declares — beverage ids, names and the
    parameter ids each drink uses — and nothing the machine alone knows:
    no min/default/max ranges and no per-profile values. Use it when the
    machine publishes no recipe datapoints (striker models) or to
    cross-check a machine-built catalogue with :func:`diff_declared`.
    """
    declaration = load_model_declaration(model)
    if not declaration:
        return RecipeCatalogue(
            fingerprint=(),
            source="empty",
            beverages={},
            priority_lists={},
            names={"beverages": {}},
            stats={"model": model, "declared": 0},
        )
    beverages = {
        recipe["id"]: Beverage(
            bev_id=recipe["id"],
            kind=_bev_kind(recipe["id"]),
            declared=True,
            name=recipe["name"],
            ingredients=tuple(recipe["ingredients"]),
            use_for_custom=recipe["use_for_custom"],
        )
        for recipe in declaration["recipes"]
    }
    return RecipeCatalogue(
        fingerprint=(
            "model_table",
            model,
            tuple((b.bev_id, b.name, b.ingredients) for b in beverages.values()),
        ),
        source="model_table",
        beverages=beverages,
        priority_lists={},
        names={"beverages": {b.bev_id: b.name for b in beverages.values()}},
        stats={
            "model": model,
            "source_type": declaration["source_type"],
            "declared": len(beverages),
            "n_standard": declaration["n_standard"],
            "n_custom": declaration["n_custom"],
        },
    )


def diff_declared(machine: RecipeCatalogue, declared: RecipeCatalogue) -> dict:
    """Compare a machine-built catalogue with the model-table declaration.

    A non-empty result flags model-map drift (wrong ``recipe_declaration``
    or a firmware that changed the drink set) — not a catalogue failure.
    """
    on_machine = catalog_beverage_ids(machine)
    in_table = catalog_beverage_ids(declared)
    return {
        "missing_on_machine": sorted(in_table - on_machine),
        "not_in_table": sorted(on_machine - in_table),
    }
