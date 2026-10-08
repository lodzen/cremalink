"""US1 — parametric frame builders byte-compared to captured commands."""

import base64
import json
import time
from pathlib import Path

import pytest
from cremalink.core.binary import crc16_ccitt
from cremalink.ecam.builder import (
    BrewAction,
    PowerCommand,
    build_brew,
    build_power,
    build_profile_select,
    build_read_param,
    build_statistics_page,
    build_write_param,
    encode_for_transport,
    trim_frame,
    verify_frame,
)
from cremalink.ecam.machine_profiles import NON_STRIKER, STRIKER

DEVICES = Path(__file__).resolve().parents[1] / "cremalink" / "devices"
APP_ID = bytes.fromhex("C0FFEEEE")


CAPTURED_FRAMES = json.loads(
    (Path(__file__).parent / "fixtures" / "ecam" / "command_frames.json").read_text()
)


def _rebuild_entry(entry: dict, captured: bytes) -> bytes:
    """Rebuild a decoded map entry; the captured frame supplies the recipe
    for entries that fetch it live (``recipe_datapoint``)."""
    if "power" in entry:
        return build_power(PowerCommand[entry["power"].upper()], NON_STRIKER)
    recipe = bytes.fromhex(entry["recipe"]) if "recipe" in entry else captured[6:-3]
    return build_brew(
        entry["beverage_id"],
        entry["action"],
        recipe,
        NON_STRIKER,
        profile_slot=entry["profile_slot"],
        accessory=entry["accessory"],
    )


@pytest.mark.parametrize("map_name", ["ECAM610.json", "ECAM612.json", "ECAM452.json"])
def test_every_map_entry_rebuilds_captured_frame(map_name):
    device_map = json.loads((DEVICES / map_name).read_text())
    catalog = set(device_map.get("catalog_datapoints") or [])
    for name, entry in device_map["command_map"].items():
        if name not in CAPTURED_FRAMES:
            assert "command" in entry  # verbatim passthrough (e.g. refresh)
            continue
        assert "command" not in entry
        if "recipe_datapoint" in entry:
            assert entry["recipe_datapoint"] in catalog
        captured = bytes.fromhex(CAPTURED_FRAMES[name])
        rebuilt = _rebuild_entry(entry, captured)
        assert rebuilt == captured, (
            f"{map_name}:{name} rebuilt {rebuilt.hex()} != captured {captured.hex()}"
        )


def test_power_frames_match_captures():
    # Live-captured ECAM610 frames.
    assert build_power(PowerCommand.WAKE, NON_STRIKER).hex() == "0d07840f02015512"
    assert build_power(PowerCommand.STANDBY, NON_STRIKER).hex() == "0d07840f01010041"
    # Live-captured ECAM452 striker refresh frame.
    assert build_power(PowerCommand.SESSION_REFRESH, NON_STRIKER).hex() == (
        "0d07840f03025640"
    )


def test_write_and_select_frames_match_captures():
    # write_aoff_test: 0x90 write of param 0x3E := 2.
    assert build_write_param(0x003E, 2, NON_STRIKER).hex() == "0d0b90f0003e0000000204d0"
    # profile_switch_alice: A9 select profile 1.
    assert build_profile_select(1, NON_STRIKER).hex() == "0d06a9f001d7c0"
    # write_aoff_test read-back: 0x95 read of param 0x3E.
    assert build_read_param(0x003E, NON_STRIKER).hex() == "0d0795f0003e56bc"


def test_length_byte_counts_through_crc():
    frame = build_brew(
        1, BrewAction.START, b"\x01\x00\x7e\x1b\x03\x02\x02\x04\x00", NON_STRIKER
    )
    assert frame[0] == 0x0D
    assert frame[1] == len(frame) - 1
    short = build_power(PowerCommand.WAKE, NON_STRIKER)
    assert short[1] == len(short) - 1 == 0x07


def test_crc_covers_everything_before_it():
    for frame in (
        build_brew(7, BrewAction.START, b"\x01\x00\x28\x1b\x01", NON_STRIKER),
        build_write_param(0x003E, 2, NON_STRIKER),
        build_statistics_page(0, 10, NON_STRIKER),
    ):
        assert crc16_ccitt(frame[:-2]) == frame[-2:]
        assert verify_frame(frame)


def test_timestamp_never_crc_covered_and_always_4_bytes():
    frame = build_power(PowerCommand.WAKE, NON_STRIKER)
    encoded = encode_for_transport(frame, NON_STRIKER, timestamp=1_700_000_000)
    payload = base64.b64decode(encoded)
    assert payload[: len(frame)] == frame
    assert payload[len(frame) :] == (1_700_000_000).to_bytes(4, "big")
    # The stored frame's own CRC must not cover the timestamp.
    assert frame[-2:] == crc16_ccitt(frame[:-2])


def test_striker_transport_carries_app_id_after_timestamp():
    frame = build_power(PowerCommand.WAKE, STRIKER)
    encoded = encode_for_transport(
        frame, STRIKER, timestamp=1_700_000_000, app_id=APP_ID
    )
    payload = base64.b64decode(encoded)
    assert payload[: len(frame)] == frame
    assert payload[len(frame) : len(frame) + 4] == (1_700_000_000).to_bytes(4, "big")
    assert payload[len(frame) + 4 :] == APP_ID
    # Non-striker carries no app-id trailer.
    plain = base64.b64decode(
        encode_for_transport(frame, NON_STRIKER, timestamp=1_700_000_000)
    )
    assert len(plain) == len(frame) + 4


def test_striker_transport_requires_app_id():
    with pytest.raises(ValueError):
        encode_for_transport(
            build_power(PowerCommand.WAKE, STRIKER), STRIKER, app_id=None
        )
    with pytest.raises(ValueError):
        encode_for_transport(
            build_power(PowerCommand.WAKE, STRIKER), STRIKER, app_id=b"\x01\x02"
        )


def test_verify_frame_rejects_bit_flips_and_garbage():
    frame = build_power(PowerCommand.WAKE, NON_STRIKER)
    for i in range(len(frame)):
        flipped = bytearray(frame)
        flipped[i] ^= 0xFF
        assert not verify_frame(bytes(flipped)), f"bit flip at {i} accepted"
    assert not verify_frame(b"")
    assert not verify_frame(b"\x0d")
    assert not verify_frame(b"\x0d\x07\x84")
    assert not verify_frame(b"\x00" + frame.hex().encode())


def test_verify_frame_tolerates_datapoint_timestamp_trailer():
    frame = build_power(PowerCommand.WAKE, NON_STRIKER)
    trailer = frame + int(time.time()).to_bytes(4, "big")
    assert verify_frame(trailer)
    assert trim_frame(trailer) == frame


def test_builder_validation():
    with pytest.raises(ValueError):
        build_brew(0x100, BrewAction.START, b"", NON_STRIKER)
    with pytest.raises(ValueError):
        build_brew(1, BrewAction.START, b"\x00" * 201, NON_STRIKER)
    with pytest.raises(ValueError):
        build_write_param(0x10000, 1, NON_STRIKER)
    with pytest.raises(ValueError):
        build_write_param(0x10, -1, NON_STRIKER)
    with pytest.raises(ValueError):
        build_statistics_page(0, 11, NON_STRIKER)
    with pytest.raises(TypeError):
        build_profile_select(1, None)
