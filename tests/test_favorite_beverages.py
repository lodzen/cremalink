"""Favourite drinks per profile (``0xAC``) from a cloud snapshot."""

from __future__ import annotations

import base64

from cremalink.ecam import catalog as ecam_catalog
from tests.test_ecam452_recipe_catalog import _device, _snapshot

# As an Eletta publishes it: profile 1, eleven drinks, one zero padding byte.
D265 = bytes.fromhex("d012acf001080a10e8e6e7e90416ea0200342a")
# Profile 2 has no favourites: all zero padding.
D266 = bytes.fromhex("d012acf0020000000000000000000000009ec1")
FAVORITES_1 = [8, 10, 16, 232, 230, 231, 233, 4, 22, 234, 2]


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_catalogue_parses_favourites_without_padding():
    catalog = ecam_catalog.build_catalog(
        {"d265_favorite_priority_1": _b64(D265), "d266_favorite_priority_2": _b64(D266)}
    )
    assert catalog.favorite_lists == {1: FAVORITES_1, 2: []}
    assert catalog.stats["families"]["favorites"] == 2


def test_seed_makes_the_favourites_available_without_a_machine_read():
    snap = {
        **_snapshot(),
        "d265_favorite_priority_1": _b64(D265),
        "d266_favorite_priority_2": _b64(D266),
    }
    dev = _device(dict(snap))
    assert dev.favorite_beverages(1) is None  # nothing seeded yet
    dev.seed_catalog_from_properties(snap)
    assert dev.favorite_beverages(1) == FAVORITES_1
    assert dev.favorite_beverages(2) == []  # known: no favourites
    assert dev.favorite_beverages(3) is None  # not published
    assert dev.transport.sent == []


def test_favourites_follow_the_active_profile():
    snap = {**_snapshot(), "d266_favorite_priority_2": _b64(D266)}
    dev = _device(dict(snap))
    dev.seed_catalog_from_properties(snap)
    dev.current_profile = 2
    assert dev.favorite_beverages() == []


def test_the_eletta_map_lists_the_favourite_datapoints():
    dev = _device({})
    for n in range(1, 5):
        assert f"d26{4 + n}_favorite_priority_{n}" in dev.catalog_datapoints
