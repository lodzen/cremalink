"""US4 — 0x95 settings parsing (all four observed shapes) + option maps."""

import base64
import json
from pathlib import Path

from cremalink.core.binary import crc16_ccitt
from cremalink.ecam.settings import SETTING_OPTION_MAPS, parse_settings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ecam"


def _blob(name):
    fixture = json.loads((FIXTURES / f"{name}.json").read_text())
    return base64.b64decode("".join(fixture["data"]["value"].split()))


def _frame(content: bytes) -> bytes:
    frame = bytes([0xD0, len(content) + 3]) + content
    return frame + crc16_ccitt(frame)


def test_stored_setting_blobs():
    assert parse_settings(_blob("lan_d281_mchn_sett_temp")) == {0x3D: 1}
    assert parse_settings(_blob("lan_d282_mchn_sett_aoff")) == {0x3E: 2}
    assert parse_settings(_blob("lan_d283_mchn_sett_water")) == {0x32: 1}
    assert parse_settings(_blob("lan_d284_mchn_sett_user_conf")) == {0x3F: 0x1D}
    assert parse_settings(_blob("lan_d285_mchn_sett_radio_conf")) == {0x2D: 3}


def test_full_republish_shape():
    # write_aoff_test d282_after: 95 f0 + param + u32 + descriptor tail.
    value = json.loads((FIXTURES / "write_aoff_test.json").read_text())["d282_after"][
        "value"
    ]
    blob = base64.b64decode(value)
    parsed = parse_settings(blob)
    assert parsed[0x3E] == 2


def test_ack_echo_shape_no_value():
    # `95 f0 <param:2B>` — 2-byte payload, produces no entry.
    blob = _frame(bytes([0x95, 0xF0, 0x00, 0x3E]))
    assert parse_settings(blob) == {}


def test_unknown_param_36_zeros():
    blob = _frame(bytes([0x95, 0x0F]) + b"\x00" * 36)
    assert parse_settings(blob) == {}


def test_trailer_tolerated():
    blob = _blob("lan_d282_mchn_sett_aoff") + b"\x11\x22\x33\x44"
    assert parse_settings(blob) == {0x3E: 2}


def test_malformed_blobs_yield_empty():
    assert parse_settings(b"") == {}
    assert parse_settings(b"\x00" * 30) == {}
    good = _blob("lan_d282_mchn_sett_aoff")
    assert parse_settings(good[:-1]) == {}  # truncated


def test_option_maps():
    aoff = SETTING_OPTION_MAPS["auto_off"]
    assert aoff.param_id == 0x3E
    assert aoff.options == {0: "15 min", 1: "30 min", 2: "1 h", 3: "3 h"}
    hardness = SETTING_OPTION_MAPS["water_hardness"]
    assert hardness.param_id == 0x32
    # stored 0-based index; displayed level = raw + 1
    assert set(hardness.options) == {0, 1, 2, 3}
    assert hardness.options[1] == "level_2"


def test_write_ack_shape():
    # write_aoff_test data_response: ack for param 0x3E, ok=0.
    datapoints = json.loads((FIXTURES / "write_aoff_test.json").read_text())[
        "datapoints"
    ]
    raw = base64.b64decode(datapoints[0]["data"]["value"])
    from cremalink.ecam.answers import parse_write_ack

    assert parse_write_ack(raw) == (0x3E, True)
