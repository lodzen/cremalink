"""Brew-with-overrides: TLV merge, 0x83 builder, recipe_params, Device.brew.

Grounded in research.md R1–R10: the app's ``O()`` builder semantics
(E0 filter, u16 widths, bean ``action|0x80``) and LAN-captured datapoints.
"""

import base64
import time
from pathlib import Path

import pytest
from cremalink.core.binary import crc16_ccitt
from cremalink.domain.device import BeanSystem, Device
from cremalink.ecam import catalog as ecam_catalog
from cremalink.ecam import params as ecam_params
from cremalink.ecam.builder import (
    build_brew,
    build_brew_params,
    brew_param_included,
    merge_recipe_params,
)
from cremalink.ecam.machine_profiles import NON_STRIKER

DEVICES = Path(__file__).resolve().parents[1] / "cremalink" / "devices"
PROFILE = NON_STRIKER


def _answer(content: bytes) -> bytes:
    frame = bytes([0xD0, len(content) + 3]) + content
    return frame + crc16_ccitt(frame)


def _a6_blob(profile: int, bev: int, tlv: bytes) -> bytes:
    return _answer(b"\xa6\xf0" + bytes([profile, bev]) + tlv)


def _bean_blob(slot: int, name: str, configured: int, selected: int) -> bytes:
    # Verified layout (cloud captures d250–d256): ``BA F0 <slot>
    # <name[40] UTF-16BE> <tail 6B>`` — no reserved byte after the slot.
    name_b = name.encode("utf-16-be").ljust(40, b"\x00")
    tail = bytes([4, 1, 3, 0, configured, selected])
    return _answer(b"\xba\xf0" + bytes([slot]) + name_b + tail)


def _b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode()


class FakeTransport:
    """Minimal LAN transport: records sends, scripts properties."""

    def __init__(self, *, properties=None):
        self.sent = []
        self.properties = dict(properties or {})
        self.written_properties = []
        self.requested_properties = []

    def send_command(self, command, alternative_property=None):
        self.sent.append((base64.b64decode(command), alternative_property))
        return {}

    def write_property(self, name, value):
        self.written_properties.append((name, value))

    def request_property(self, name):
        self.requested_properties.append(name)

    def get_property(self, name):
        return self.properties.get(name)

    def pop_response(self):
        return None

    def configure(self):
        pass

    def get_properties(self):
        return self.properties

    def refresh_monitor(self):
        pass

    def health(self):
        return {"ok": True}


def _device(**kwargs):
    transport = kwargs.pop("transport", FakeTransport())
    return Device.from_map(
        transport=transport,
        device_map_path=str(DEVICES / "ECAM610.json"),
        response_poll=0,
        response_timeout=0.01,
        **kwargs,
    )


def _catalog(beverages: dict) -> ecam_catalog.RecipeCatalogue:
    return ecam_catalog.RecipeCatalogue(
        fingerprint=(),
        source="test",
        beverages=beverages,
        priority_lists={},
        names={},
        stats={},
    )


def _frame_payload(device) -> bytes:
    # sent payloads carry the 4-byte transport timestamp — trim to frame.
    from cremalink.ecam.builder import trim_frame

    return trim_frame(device.transport.sent[-1][0])


# --- merge_recipe_params / build_brew_params -------------------------------


def test_merge_splices_u8_and_u16_in_order():
    recipe = bytes([0x01, 0x00, 0x50, 0x02, 0x04, 0x1B, 0x01])
    merged = merge_recipe_params(recipe, {2: 3, 1: 0x0060})
    assert merged == bytes([0x01, 0x00, 0x60, 0x02, 0x03, 0x1B, 0x01])


def test_merge_appends_absent_tags():
    recipe = bytes([0x02, 0x04])
    merged = merge_recipe_params(recipe, {0x0F: 0x00FA})
    assert merged == bytes([0x02, 0x04, 0x0F, 0x00, 0xFA])


def test_merge_rejects_u16_overflow_on_u8_tag():
    with pytest.raises(ValueError):
        merge_recipe_params(b"", {2: 0x0100})


def test_build_brew_params_parity_with_build_brew():
    params = {1: 80, 2: 4, 27: 2}
    frame = build_brew_params(7, 1, params, PROFILE, profile_slot=1, accessory=2)
    expected = build_brew(
        7, 1, bytes([1, 0, 80, 2, 4, 27, 2]), PROFILE, profile_slot=1, accessory=2
    )
    assert frame == expected
    # 0D LEN 83 F0 07 01 <TLVs> <06> <crc>
    assert frame[4] == 7 and frame[5] == 1
    assert frame[-3] == (1 << 2) | 2


def test_build_brew_params_e0_filter():
    # {8, 24, 25, 30} never reach the wire; {27, 28} do (research.md R2/R4).
    frame = build_brew_params(
        7, 1, {8: 1, 24: 1, 25: 1, 30: 5, 1: 40, 27: 3, 28: 0}, PROFILE
    )
    tlvs = frame[6:-3]
    tags = [tlvs[i] for i in range(0, len(tlvs), 1)]
    assert 8 not in tags and 24 not in tags and 25 not in tags and 30 not in tags
    assert 1 in tags and 27 in tags and 28 in tags
    for dropped in (8, 24, 25, 30):
        assert not brew_param_included(dropped)


def test_build_brew_params_bean_flag_and_taste_retained():
    # bev 200 → action|0x80; param 2 (intensity) retained (research.md R10).
    frame = build_brew_params(
        0xC8, 1, {1: 48, 2: 3, 27: 2}, PROFILE, profile_slot=1, accessory=2
    )
    assert frame[4] == 0xC8
    assert frame[5] == 0x81
    tlvs = frame[6:-3]
    assert tlvs == bytes([1, 0, 48, 2, 3, 27, 2])


# --- recipe_params ---------------------------------------------------------

ESPRESSO_TLV = bytes([1, 0, 80, 2, 4, 4, 1, 27, 1])


def test_recipe_params_profile_source():
    props = {"d039_1_rec_espresso": _b64(_a6_blob(1, 1, ESPRESSO_TLV))}
    device = _device(transport=FakeTransport(properties=props))
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1, kind="builtin", ranges={1: (30, 60, 120), 2: (1, 4, 5)}
            )
        }
    )
    rp = device.recipe_params(1, catalog=catalog)
    assert rp.source == "profile"
    assert rp.values == {1: 80, 2: 4, 4: 1, 27: 1}
    assert rp.bounds == {1: (30, 60, 120), 2: (1, 4, 5)}


def test_recipe_params_default_fallback():
    device = _device(transport=FakeTransport())
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1, kind="builtin", ranges={1: (30, 60, 120), 2: (1, 4, 5)}
            )
        }
    )
    rp = device.recipe_params(1, catalog=catalog)
    assert rp.source == "default"
    assert rp.values == {1: 60, 2: 4}


def test_recipe_params_unknown_beverage():
    device = _device(transport=FakeTransport())
    with pytest.raises(ValueError):
        device.recipe_params(99, catalog=_catalog({}))


def test_selected_bean_slot_flag_decode():
    props = {
        "d250_beansystem_0": _b64(_bean_blob(0, "BeanAdapt (ON/OFF)", 1, 0)),
        "d251_beansystem_1": _b64(_bean_blob(1, "Crema", 1, 1)),
        "d252_beansystem_2": _b64(_bean_blob(2, "", 0, 0)),
    }
    device = _device(transport=FakeTransport(properties=props))
    assert device.selected_bean_slot() == 1


def test_selected_bean_slot_single_configured_fallback():
    props = {"d251_beansystem_1": _b64(_bean_blob(1, "Crema", 1, 0))}
    device = _device(transport=FakeTransport(properties=props))
    assert device.selected_bean_slot() == 1


def test_bean_systems_decode_names_and_flags():
    props = {
        "d250_beansystem_0": _b64(_bean_blob(0, "BeanAdapt (ON/OFF)", 1, 0)),
        "d251_beansystem_1": _b64(_bean_blob(1, "Crema", 1, 1)),
        "d252_beansystem_2": _b64(_bean_blob(2, "Bohnen 2", 1, 0)),
        "d253_beansystem_3": _b64(_bean_blob(3, "", 0, 0)),
        "d254_beansystem_4": _b64(b"\x01\x02\x03"),  # undecodable — skipped
    }
    device = _device(transport=FakeTransport(properties=props))
    systems = device.bean_systems()
    assert [(s.slot, s.name, s.selected) for s in systems] == [
        (1, "Crema", True),
        (2, "Bohnen 2", False),
    ]


def test_bean_systems_empty_name_falls_back_to_slot_label():
    props = {"d254_beansystem_4": _b64(_bean_blob(4, "", 1, 0))}
    device = _device(transport=FakeTransport(properties=props))
    systems = device.bean_systems()
    assert [(s.slot, s.name) for s in systems] == [(4, "Bean 4")]


def test_bean_systems_none_configured():
    device = _device(transport=FakeTransport())
    assert device.bean_systems() == []


# Verbatim ba f0 frames from the cloud capture
# (captures/cloud_properties_normalized.json, 2026-10-12) — the layout
# evidence: no reserved byte after the slot; 40-byte name field keeps
# stale tail bytes past its first NUL (d252: "Bohnen 2\0\0Bohnen 3").
_D250 = "0DS68AAAQgBlAGEAbgAgAEEAZABhAHAAdAAgACgATwBOAC8ATwBGAEYAKQAAAwEAAAEAcZA="
_D251 = "0DS68AEAQwByAGUAbQBhAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAEDAAEBqho="
_D252 = "0DS68AIAQgBvAGgAbgBlAG4AIAAyAAAAAABCAG8AaABuAGUAbgAgADMAAAAAAAAAAAAAhC8="


def test_bean_systems_decode_verbatim_captures():
    props = {
        "d250_beansystem_0": _D250,  # BeanAdapt flag slot — filtered by [1-6]
        "d251_beansystem_1": _D251,  # configured + selected → "Crema"
        "d252_beansystem_2": _D252,  # unconfigured (00 00 flags) → skipped
    }
    device = _device(transport=FakeTransport(properties=props))
    assert device.bean_systems() == [BeanSystem(1, "Crema", True)]
    assert device.selected_bean_slot() == 1


def test_bean_systems_name_cut_at_first_nul():
    # Stale bytes survive in the 40-byte field past its first NUL —
    # the decode must stop there (real d252 shows a previous name).
    name_field = (
        "Crema".encode("utf-16-be") + b"\x00\x00" + "Bohnen".encode("utf-16-be")
    ).ljust(40, b"\x00")
    blob = _answer(b"\xba\xf0" + bytes([1]) + name_field + bytes([4, 1, 3, 0, 1, 0]))
    device = _device(
        transport=FakeTransport(properties={"d251_beansystem_1": _b64(blob)})
    )
    assert [s.name for s in device.bean_systems()] == ["Crema"]


def test_recipe_params_bean_uses_bs_recipe():
    bean_tlv = bytes([1, 0, 48, 27, 2, 2, 3, 25, 1])
    props = {
        "d251_beansystem_1": _b64(_bean_blob(1, "Crema", 1, 1)),
        "d160_1_bs_recipe_01": _b64(_a6_blob(1, 0xC8, bean_tlv)),
    }
    device = _device(transport=FakeTransport(properties=props))
    catalog = _catalog(
        {
            200: ecam_catalog.Beverage(
                bev_id=200,
                kind="bean_system",
                ranges={1: (30, 40, 60), 2: (1, 4, 5), 27: (0, 1, 4)},
            )
        }
    )
    rp = device.recipe_params(200, catalog=catalog)
    assert rp.source == "profile"
    assert rp.bean_slot == 1
    # values carry the brewable TLV only — E0 drops management tag 25.
    assert rp.values == {1: 48, 27: 2, 2: 3}


# --- Device.brew with overrides --------------------------------------------


def _device_with_espresso():
    props = {"d039_1_rec_espresso": _b64(_a6_blob(1, 1, ESPRESSO_TLV))}
    device = _device(transport=FakeTransport(properties=props))
    device._session().announced_at = time.time()  # session already announced
    return device


def test_brew_sends_single_gated_frame():
    device = _device_with_espresso()
    device.brew(1)
    brews = [f for f, _ in device.transport.sent if f[2] == 0x83]
    assert len(brews) == 1
    assert brews[0][4] == 1  # espresso


def test_brew_overrides_merged_in_frame():
    device = _device_with_espresso()
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1, kind="builtin", ranges={1: (30, 60, 120), 2: (1, 4, 5)}
            )
        }
    )
    rp = device.recipe_params(1, catalog=catalog)
    device.brew(1, overrides={2: 3, 1: 0x0060}, params=rp)
    frame = _frame_payload(device)
    tlvs = frame[6:-3]
    assert tlvs == bytes([1, 0, 0x60, 2, 3, 4, 1, 27, 1])


def test_brew_override_out_of_bounds_rejected():
    device = _device_with_espresso()
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1, kind="builtin", ranges={1: (30, 60, 120), 2: (1, 4, 5)}
            )
        }
    )
    rp = device.recipe_params(1, catalog=catalog)
    with pytest.raises(ValueError, match="outside bounds"):
        device.brew(1, overrides={2: 99}, params=rp)


def test_brew_override_management_param_rejected():
    device = _device_with_espresso()
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1, kind="builtin", ranges={1: (30, 60, 120), 2: (1, 4, 5)}
            )
        }
    )
    rp = device.recipe_params(1, catalog=catalog)
    with pytest.raises(ValueError, match="machine-managed"):
        device.brew(1, overrides={27: 4}, params=rp)  # INDEX_LENGTH (R11)


def test_brew_override_unknown_param_rejected():
    device = _device_with_espresso()
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1, kind="builtin", ranges={1: (30, 60, 120), 2: (1, 4, 5)}
            )
        }
    )
    rp = device.recipe_params(1, catalog=catalog)
    with pytest.raises(ValueError, match="not part of this recipe"):
        device.brew(1, overrides={13: 2}, params=rp)


def test_brew_unknown_recipe_fails_closed():
    device = _device(transport=FakeTransport())
    with pytest.raises(ConnectionError):
        device.brew(3)  # long coffee: no recipe datapoint available


def test_brew_bean_sends_multi_flag():
    bean_tlv = bytes([1, 0, 48, 27, 2, 2, 3])
    props = {
        "d251_beansystem_1": _b64(_bean_blob(1, "Crema", 1, 1)),
        "d160_1_bs_recipe_01": _b64(_a6_blob(1, 0xC8, bean_tlv)),
    }
    device = _device(transport=FakeTransport(properties=props))
    device._session().announced_at = time.time()
    device.brew(200)
    frame = _frame_payload(device)
    assert frame[4] == 0xC8
    assert frame[5] == 0x81  # START | 0x80


def test_params_metadata_scaling():
    # coffee/water raw = ml directly (R11 — LAN descriptors + the app's
    # getCoffeeQty_ml/getHotWaterQty_ml returning 0xB0 values unscaled);
    # only milk scales (deciseconds → seconds).
    assert ecam_params.to_display(1, 216) == 216.0
    assert ecam_params.to_display(15, 250) == 250.0
    assert ecam_params.to_display(9, 120) == 12.0
    assert ecam_params.to_raw(1, 216) == 216
    assert ecam_params.to_raw(9, 12) == 120
    # tag 27 = INDEX_LENGTH — management, not a control (R11).
    assert 27 not in ecam_params.PARAMS
    assert ecam_params.MANAGEMENT_PARAMS == frozenset({27, 28})


def test_adjustable_params_app_enable_rules():
    """Soul espresso bounds → exactly coffee + intensity (R12)."""
    bounds = {
        24: (0, 1, 1),  # PROGRAMABLE
        25: (0, 1, 1),  # VISIBLE
        8: (0, 0, 1),
        1: (20, 40, 180),
        2: (0, 4, 5),
        4: (0, 0, 0),  # blend — degenerate on every Soul descriptor
        27: (0, 1, 4),  # INDEX_LENGTH — never a control
        28: (2, 2, 2),
    }
    enabled = ecam_params.adjustable_params(bounds)
    assert set(enabled) == {1, 2}


def test_adjustable_params_taste_binary_bounds_shown():
    # The app has no bounds-based aroma gate (``U0`` needs only the TASTE
    # ingredient + coffee > 0) — a binary 0/1 range still gets the picker.
    bounds = {24: (0, 1, 1), 1: (20, 40, 180), 2: (0, 0, 1)}
    assert set(ecam_params.adjustable_params(bounds)) == {1, 2}


def test_adjustable_params_not_programable_still_adjustable():
    # No PROGRAMABLE check in the brew sheet (``E1``; R12): 2x espresso
    # has def=0 there yet brews with coffee + intensity overrides.
    bounds = {24: (0, 0, 1), 1: (20, 40, 180), 2: (0, 4, 5)}
    assert set(ecam_params.adjustable_params(bounds)) == {1, 2}


def test_adjustable_params_amount_zero_hides_quantities():
    # Mug To-Go: every quantity default is 0 → the app shows no sliders
    # (``k()/u()/C() > 0`` guards in ``V0``/``Y0``/``c1``; R12).
    bounds = {
        1: (40, 0, 240),
        2: (1, 3, 5),
        9: (60, 0, 460),
        15: (50, 0, 260),
        12: (0, 0, 1),
        24: (0, 1, 1),
        25: (0, 2, 2),
        27: (0, 255, 4),
    }
    assert ecam_params.adjustable_params(bounds) == {}


def test_adjustable_params_profile_values_revive_zero_default():
    # Descriptor default 0 but the profile stored a real amount → shown.
    bounds = {1: (40, 0, 240), 2: (1, 3, 5)}
    assert set(ecam_params.adjustable_params(bounds, {1: 120})) == {1, 2}


def test_adjustable_params_bean_beverage_coffee_only():
    # id == 200 special case in ``E1``: BeanAdapt gets coffee amount
    # alone — the rest is configured in the BeanAdapt flow (R12).
    bounds = {1: (30, 40, 60), 2: (1, 4, 5), 24: (0, 1, 1), 25: (0, 2, 2)}
    assert set(ecam_params.adjustable_params(bounds, beverage_id=200)) == {1}


def test_adjustable_params_tea_temperature_picker():
    # Tea (id 22) carries THE_TEMP(13) — the app's tea-temp picker (R12).
    bounds = {13: (0, 1, 3), 15: (20, 150, 420), 24: (0, 1, 1), 27: (0, 1, 4)}
    assert set(ecam_params.adjustable_params(bounds, beverage_id=22)) == {13, 15}


def test_adjustable_params_milk_drink():
    bounds = {
        1: (20, 65, 180),
        2: (0, 3, 5),
        9: (50, 170, 1080),
        4: (0, 0, 0),
        24: (0, 1, 1),
        25: (0, 1, 1),
        27: (0, 1, 4),
        28: (2, 2, 2),
    }
    assert set(ecam_params.adjustable_params(bounds)) == {1, 2, 9}


def test_recipe_params_catalogue_profile_values_no_fetch():
    """a6f0 values from the build-once catalogue — zero reads (R11)."""
    device = _device(transport=FakeTransport())  # no recipe props at all
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1,
                kind="builtin",
                ranges={1: (30, 60, 120), 2: (1, 4, 5)},
                profiles={1: {1: 216, 2: 5}},
            )
        }
    )
    rp = device.recipe_params(1, catalog=catalog)
    assert rp.source == "profile"
    assert rp.values == {1: 216, 2: 5}
    assert device.transport.requested_properties == []  # store-read only


# --- lazy catalogue: no sweep (protocol.md §13/§14.12, research.md R11) -----

# LAN-captured d001_rec_espresso descriptor (captures/lan_d001_rec_espresso.json):
# coffee (20,40,180), intensity (0,4,5), PROGRAMABLE (0,1,1).
D001_ESPRESSO = bytes.fromhex(
    "d025b0f0010800000118000101010014002800b41b000104020004050400000019000101b996"
)


def _lazy_device(**props):
    return _device(transport=FakeTransport(properties=props))


def test_beverage_catalog_static_zero_requests():
    device = _lazy_device()
    bevs = device.beverage_catalog(fetch_priority=False)
    assert {b.bev_id for b in bevs} >= {1, 2, 200}  # command-map brew entries
    assert device.transport.requested_properties == []
    assert device.transport.sent == []


def test_beverage_catalog_priority_one_get_cached():
    prio = _answer(b"\xa8\xf0" + bytes([1, 7, 1, 2]))
    device = _lazy_device(d261_1_rec_priority=_b64(prio))
    first = device.beverage_catalog()
    assert [b.bev_id for b in first][:3] == [7, 1, 2]  # machine menu order
    assert device.transport.requested_properties == ["d261_1_rec_priority"]
    assert device.recipe_priority_loaded()
    device.beverage_catalog()
    assert device.transport.requested_properties == ["d261_1_rec_priority"]


def test_recipe_params_reads_only_its_descriptor_once():
    props = {
        "d001_rec_espresso": _b64(D001_ESPRESSO),
        "d039_1_rec_espresso": _b64(_a6_blob(1, 1, ESPRESSO_TLV)),
    }
    device = _lazy_device(**props)
    rp = device.recipe_params(1)
    assert rp.bounds[1] == (20, 40, 180)
    assert rp.source == "profile"
    first = list(device.transport.requested_properties)
    assert "d001_rec_espresso" in first
    assert len(set(first)) <= 2  # descriptor + the recipe values — no sweep
    device.recipe_params(1)  # descriptor now cached → no second GET for it
    assert device.transport.requested_properties.count("d001_rec_espresso") == 1


def test_failed_descriptor_read_not_cached():
    device = _lazy_device()  # machine answers nothing
    assert device.recipe_params(1).bounds == {}
    assert 1 not in device._descriptor_cache
    device.transport.properties["d001_rec_espresso"] = _b64(D001_ESPRESSO)
    assert device.recipe_params(1).bounds[1] == (20, 40, 180)


def test_seed_catalog_from_cloud_snapshot_zero_machine_reads():
    """Cloud bulk snapshot → descriptors + menu order cached, 0 GETs (R11)."""
    prio = _answer(b"\xa8\xf0" + bytes([1, 7, 1, 2]))
    device = _lazy_device(d039_1_rec_espresso=_b64(_a6_blob(1, 1, ESPRESSO_TLV)))
    seeded = device.seed_catalog_from_properties(
        {
            "d001_rec_espresso": _b64(D001_ESPRESSO),
            "d261_1_rec_priority": _b64(prio),
            "d302_monitor": "ignored-not-a-catalog-datapoint",
            "d002_rec_regular": None,  # never published → skipped
        }
    )
    assert seeded == 1
    assert device.recipe_priority_loaded()
    assert [b.bev_id for b in device.beverage_catalog()][:3] == [7, 1, 2]
    rp = device.recipe_params(1)
    assert rp.bounds[1] == (20, 40, 180)
    # Only the live recipe VALUES were read — descriptor + menu order came
    # from the seed; cloud a6f0 values are never trusted for a brew.
    assert device.transport.requested_properties == ["d039_1_rec_espresso"]


def test_seed_catalog_empty_snapshot_keeps_lan_fallback():
    device = _lazy_device(d001_rec_espresso=_b64(D001_ESPRESSO))
    assert device.seed_catalog_from_properties({}) == 0
    assert device.seed_catalog_from_properties({"unrelated": "x"}) == 0
    # LAN fallback still works: lazy single-GET descriptor read.
    assert device.recipe_params(1).bounds[1] == (20, 40, 180)
    assert "d001_rec_espresso" in device.transport.requested_properties


def test_recipe_params_unknown_beverage_without_catalog():
    with pytest.raises(ValueError):
        _lazy_device().recipe_params(99)


def test_recipe_params_catalogue_gap_falls_back_to_live():
    """A (profile, beverage) the catalogue missed still reads live."""
    props = {"d060_2_rec_espresso": _b64(_a6_blob(2, 1, ESPRESSO_TLV))}
    device = _device(transport=FakeTransport(properties=props))
    catalog = _catalog(
        {
            1: ecam_catalog.Beverage(
                bev_id=1,
                kind="builtin",
                ranges={1: (30, 60, 120), 2: (1, 4, 5)},
                profiles={1: {1: 216, 2: 5}},  # profile 1 only
            )
        }
    )
    rp = device.recipe_params(1, profile_slot=2, catalog=catalog)
    assert rp.source == "profile"
    assert rp.values == {1: 80, 2: 4, 4: 1, 27: 1}
