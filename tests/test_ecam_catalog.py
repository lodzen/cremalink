"""US5 — recipe catalogue parsing over the vendored blob corpus."""

import base64
import json
from pathlib import Path

from cremalink.ecam.catalog import (
    BEAN_SYSTEM_IDS,
    CUSTOM_SLOT_IDS,
    build_catalog,
    build_declared_catalog,
    catalog_beverage_ids,
    decode_blob,
    diff_declared,
    fingerprint,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ecam"


def _corpus_properties():
    """All vendored `lan_d*` datapoint captures as {name: {"value": b64}}."""
    props = {}
    for path in sorted(FIXTURES.glob("lan_d*.json")):
        data = json.loads(path.read_text()).get("data", {})
        if "name" in data and "value" in data:
            props[data["name"]] = {"value": data["value"]}
    return props


def test_corpus_blobs_decode_and_crc_validate():
    props = _corpus_properties()
    assert props
    crc_ok = families = 0
    for prop in props.values():
        blob = decode_blob(prop["value"])
        if blob is None or blob["truncated"]:
            continue
        crc_ok += int(blob["crc_valid"])
        families += 1
    assert families > 50
    # The vendored corpus was captured live — every complete blob is CRC-valid.
    assert crc_ok == families


def test_catalog_beverage_set():
    catalog = build_catalog(_corpus_properties())
    declared = catalog_beverage_ids(catalog)
    # 21 factory beverages + bean slot + 6 custom slots = 28 (FR-023).
    factory = {b for b in declared if b < 0xC8}
    assert len(factory) == 21
    assert {0x01, 0x02, 0x07, 0x10, 0x1B} <= factory
    custom = declared & CUSTOM_SLOT_IDS
    assert custom == set(range(0xE6, 0xEC))
    beans = declared & BEAN_SYSTEM_IDS
    assert 0xC8 in beans
    assert len(declared) == 28


def test_catalog_descriptor_triples_and_profile_values():
    catalog = build_catalog(_corpus_properties())
    espresso = catalog.beverages[0x01]
    assert espresso.declared
    # b0f0 descriptor: tag -> (min, default, max) triples.
    assert espresso.ranges
    for triple in espresso.ranges.values():
        assert len(triple) == 3
        assert triple[0] <= triple[1] <= triple[2]
    # a6f0: per-profile current values.
    assert espresso.profiles
    assert 1 in espresso.profiles


def test_catalog_priority_lists_and_names():
    catalog = build_catalog(_corpus_properties())
    # a8f0: ordered per-profile recency lists exist.
    assert catalog.priority_lists
    for profile, ids in catalog.priority_lists.items():
        assert 1 <= profile <= 5
        assert all(isinstance(i, int) for i in ids)
    # a4f0: user-entered profile names decoded.
    assert catalog.names["profiles"].get(1) == "Alice"
    # aaf0/baf0 custom + bean names parsed (values may be empty strings
    # on un-programmed slots — keys exist where blobs provided names).


def test_fingerprint_stability_and_sensitivity():
    props = _corpus_properties()
    fp1 = fingerprint(props)
    assert fingerprint(props) == fp1  # stable on unchanged data
    changed = dict(props)
    name, prop = next(
        (n, p) for n, p in changed.items() if decode_blob(p["value"]) is not None
    )
    changed[name] = {"value": prop["value"][:-4] + "AAAA"}
    assert fingerprint(changed) != fp1


def test_crc_invalid_blob_skipped():
    props = _corpus_properties()
    name, prop = next(
        (n, p) for n, p in props.items() if decode_blob(p["value"]) is not None
    )
    raw = base64.b64decode("".join(prop["value"].split()))
    corrupted = bytearray(raw)
    corrupted[5] ^= 0xFF
    props[name] = {"value": base64.b64encode(bytes(corrupted)).decode()}
    cat_bad = build_catalog(props)
    cat_good = build_catalog(_corpus_properties())
    assert cat_bad.stats["crc_failed"] >= 1
    # The corrupted blob contributed nothing.
    assert (
        cat_bad.stats["crc_ok"] == cat_good.stats["crc_ok"] - 1
        or cat_bad.stats["crc_failed"] >= cat_good.stats["crc_failed"] + 1
    )


def test_declared_catalog_matches_live_soul_capture():
    """The app's PD_SOUL declaration lists exactly the beverages the
    live ECAM610 publishes (28: 21 factory + bean + 6 custom slots)."""
    declared = build_declared_catalog("PD_SOUL")
    assert declared.source == "model_table"
    assert diff_declared(build_catalog(_corpus_properties()), declared) == {
        "missing_on_machine": [],
        "not_in_table": [],
    }


def test_declared_catalog_striker_best():
    catalog = build_declared_catalog("STRIKER_BEST")
    assert catalog.source == "model_table"
    assert len(catalog.beverages) == 48
    assert catalog.stats["n_standard"] == 18 and catalog.stats["n_custom"] == 6
    espresso = catalog.beverages[1]
    assert espresso.name == "Espresso Coffee"
    assert espresso.kind == "builtin" and espresso.declared
    assert espresso.ingredients == (1, 2, 27, 30, 8, 24, 25)
    assert espresso.use_for_custom is True
    assert espresso.ranges == {} and espresso.profiles == {}  # table has no values
    assert catalog.beverages[200].kind == "bean_system"
    assert {b for b, v in catalog.beverages.items() if v.kind == "custom"} == set(
        CUSTOM_SLOT_IDS
    )
    # Striker-only drinks the Soul table does not declare.
    assert {50, 80, 100} <= set(catalog.beverages)
    assert catalog.names["beverages"][9] == "Caff\u00e9 Latte"


def test_declared_catalog_unknown_or_undeclared_model_is_empty():
    for model in (None, "", "STRIKER_GOOD", "NOT_A_MODEL"):
        catalog = build_declared_catalog(model)
        assert catalog.source == "empty" and not catalog.beverages


def test_declared_catalog_fingerprint_is_stable_and_per_model():
    assert (
        build_declared_catalog("PD_SOUL").fingerprint
        == build_declared_catalog("PD_SOUL").fingerprint
    )
    assert (
        build_declared_catalog("PD_SOUL").fingerprint
        != build_declared_catalog("PD_SOUL_BETTER").fingerprint
    )
