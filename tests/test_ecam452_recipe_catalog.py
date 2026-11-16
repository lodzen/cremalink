"""ECAM452 recipe catalogue from one cloud snapshot.

The Eletta publishes its factory descriptors bundled as JSON
(``d002_rec_default_1`` … ``{"<label>": "<base64 blob>"}``) and its profile
recipes under names without a formula (``d102_rec_1_a_cb_coffee`` and
``d102_rec_2_espresso`` share a number). The catalogue has to read both
from the blobs themselves.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cremalink.core.binary import crc16_ccitt
from cremalink.domain.device import Device
from cremalink.ecam import catalog as ecam_catalog

DEVICES = Path(__file__).resolve().parents[1] / "cremalink" / "devices"
COLD_BREW = 120
CUSTOM_1, CUSTOM_6 = 230, 235


def _answer(content: bytes) -> bytes:
    frame = bytes([0xD0, len(content) + 3]) + content
    return frame + crc16_ccitt(frame)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _descriptor(bev: int, triples: bytes) -> bytes:
    return _answer(b"\xb0\xf0" + bytes([bev]) + triples)


def _recipe(profile: int, bev: int, tlv: bytes) -> bytes:
    return _answer(b"\xa6\xf0" + bytes([profile, bev]) + tlv)


def _custom_names(first: int, names: list[tuple[str, int]]) -> bytes:
    body = bytes([first, first + len(names) - 1])
    for name, icon in names:
        body += name.encode("utf-16-be")[:20].ljust(20, b"\x00") + bytes([icon])
    return _answer(b"\xaa\xf0" + body)


def _priority(profile: int, bevs: list[int]) -> bytes:
    return _answer(b"\xa8\xf0" + bytes([profile, *bevs]))


# Espresso: coffee ml (wide tag 0x01) 20/40/180, taste (0x02) 0/4/5.
ESPRESSO_BOUNDS = b"\x01\x00\x14\x00\x28\x00\xb4" + b"\x02\x00\x04\x05"
COLD_BREW_BOUNDS = b"\x01\x00\x50\x00\x78\x00\xa0"


def _snapshot(*, bundle_b64: bool = False) -> dict[str, str]:
    bundle = json.dumps(
        {
            "espresso": _b64(_descriptor(1, ESPRESSO_BOUNDS)),
            "a_cb_coffee": _b64(_descriptor(COLD_BREW, COLD_BREW_BOUNDS)),
        }
    )
    return {
        "d002_rec_default_1": _b64(bundle.encode()) if bundle_b64 else bundle,
        "d059_rec_1_espresso": _b64(_recipe(1, 1, b"\x01\x00\x32\x02\x04")),
        "d102_rec_1_a_cb_coffee": _b64(_recipe(1, COLD_BREW, b"\x01\x00\x78")),
        "d102_rec_2_espresso": _b64(_recipe(2, 1, b"\x01\x00\x28\x02\x04")),
        "d240_rec_custom_1": _b64(_recipe(1, CUSTOM_1, b"\x01\x00\x37\x02\x03")),
        "d245_rec_custom_6": _b64(_recipe(1, CUSTOM_6, b"\x01\x00\x00\x02\x00")),
        "d261_recipe_priority_1": _b64(_priority(1, [2, 1, COLD_BREW])),
        "d053_custom_name_13": _b64(
            _custom_names(1, [("Latte XL", 2), ("Matcha", 0x13), ("wärmen", 0x12)])
        ),
        # A 10-character name fills all 20 bytes, no terminator.
        "d054_custom_name_46": _b64(
            _custom_names(4, [("FlatWh. gr", 0x0B), ("kl. FlatWh", 0), ("", 0)])
        ),
    }


class CloudLikeTransport:
    """Named cloud properties, no mailbox; records sent frames."""

    def __init__(self, properties: dict[str, str]):
        self.properties = properties
        self.sent: list[tuple[bytes, str | None]] = []

    def get_property(self, name):
        value = self.properties.get(name)
        return {"name": name, "value": value} if value is not None else None

    def send_command(self, command, alternative_property=None):
        self.sent.append((base64.b64decode(command), alternative_property))
        return {}

    def request_property(self, name):
        raise NotImplementedError("request_property is LAN-only")

    def pop_response(self):
        raise NotImplementedError("pop_response is LAN-only")

    def configure(self):
        pass


def _device(properties: dict[str, str]) -> Device:
    properties = {"app_id": "-1056968978", **properties}  # C0FFEEEE registered
    dev = Device.from_map(
        CloudLikeTransport(properties), device_map_path=str(DEVICES / "ECAM452.json")
    )
    dev.response_poll = 0
    dev.response_timeout = 0.05
    return dev


def test_json_bundle_descriptors_are_parsed():
    for bundle_b64 in (False, True):
        catalog = ecam_catalog.build_catalog(_snapshot(bundle_b64=bundle_b64))
        assert catalog.beverages[1].ranges[1] == (20, 40, 180)
        assert catalog.beverages[1].ranges[2] == (0, 4, 5)
        assert catalog.beverages[COLD_BREW].ranges[1] == (80, 120, 160)


def test_non_json_values_are_not_mistaken_for_bundles():
    catalog = ecam_catalog.build_catalog({"x": "plain text", "y": _b64(b"{oops")})
    assert catalog.beverages == {}


def test_seed_fills_bounds_and_menu_order_for_ecam452():
    snap = _snapshot()
    dev = _device(dict(snap))

    assert dev.seed_catalog_from_properties(snap) == 2
    assert dev.recipe_priority_loaded(1)


def test_beverage_catalog_includes_cold_brew_and_defined_custom_slots():
    snap = _snapshot()
    dev = _device(dict(snap))
    dev.seed_catalog_from_properties(snap)

    ids = [b.bev_id for b in dev.beverage_catalog(fetch_priority=False)]

    assert COLD_BREW in ids
    assert CUSTOM_1 in ids
    assert CUSTOM_6 not in ids  # empty slot


def test_recipe_params_read_the_eletta_recipe_datapoints():
    snap = _snapshot()
    dev = _device(dict(snap))
    dev.seed_catalog_from_properties(snap)

    p1 = dev.recipe_params(1, profile_slot=1)
    p2 = dev.recipe_params(1, profile_slot=2)
    custom = dev.recipe_params(CUSTOM_1, profile_slot=2)
    cold = dev.recipe_params(COLD_BREW, profile_slot=1)

    assert (p1.source, p1.values[1], p1.bounds[1]) == ("profile", 50, (20, 40, 180))
    assert (p2.source, p2.values[1]) == ("profile", 40)
    assert (custom.source, custom.values[1]) == ("profile", 55)
    assert (cold.source, cold.values[1], cold.bounds[1]) == (
        "profile",
        120,
        (80, 120, 160),
    )


def test_seed_keeps_recipe_values_out_of_the_cache():
    # Values stay a live read: changing the cloud copy after the seed is
    # what recipe_params returns.
    snap = _snapshot()
    dev = _device(dict(snap))
    dev.seed_catalog_from_properties(snap)
    dev.transport.properties["d059_rec_1_espresso"] = _b64(
        _recipe(1, 1, b"\x01\x00\x3c\x02\x04")
    )

    assert dev.recipe_params(1, profile_slot=1).values[1] == 60


def test_custom_recipe_names_come_from_the_snapshot():
    snap = _snapshot()
    dev = _device(dict(snap))
    dev.seed_catalog_from_properties(snap)

    assert dev.custom_recipe_names() == {
        230: "Latte XL",
        231: "Matcha",
        232: "wärmen",
        233: "FlatWh. gr",
        234: "kl. FlatWh",
    }
