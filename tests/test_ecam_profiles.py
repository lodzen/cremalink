"""US3 — a4f0 profile-name parsing and the 0xA9 select ack."""

import base64
import json
from pathlib import Path

import pytest
from cremalink.core.binary import crc16_ccitt
from cremalink.ecam.answers import parse_a9_ack
from cremalink.ecam.machine_profiles import NON_STRIKER, STRIKER
from cremalink.ecam.profiles import parse_profile_names

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ecam"


def _blob(name):
    fixture = json.loads((FIXTURES / f"{name}.json").read_text())
    return base64.b64decode("".join(fixture["data"]["value"].split()))


def test_parse_profiles_1_3_capture():
    slots = parse_profile_names(_blob("lan_d034_profiles_1_3"), NON_STRIKER)
    assert [(s.index, s.name, s.icon) for s in slots] == [
        (1, "Alice", 16),
        (2, "Bob", 5),
        (3, "Profil 3", 9),
    ]


def test_parse_profiles_4_5_capture():
    slots = parse_profile_names(_blob("lan_d035_profiles_4_5"), NON_STRIKER)
    assert [(s.index, s.name) for s in slots] == [
        (4, "Profil 4"),
        (5, "Profil 5"),
    ]
    # Icon 14 > 11 — passes through unmapped (no Eletta ÷3/%3 remap).
    assert slots[1].icon == 14
    # Non-striker entries carry no mug byte.
    assert all(s.mug is None for s in slots)


def test_empty_and_padding_slots_excluded():
    # Craft an a4f0 blob with one named and two empty/padded slots.
    def entry(name, icon):
        raw = name.encode("utf-16-be")[:20].ljust(20, b"\x00") + bytes([icon])
        return raw

    body = bytes([1, 3])
    body += entry("Ada", 4)
    body += b"\x00" * 21  # empty slot
    body += b"\xff" * 21  # all-padding slot (invalid utf16 -> excluded)
    frame = bytes([0xD0, 0x00, 0xA4, 0xF0]) + body
    frame = frame[:1] + bytes([len(frame) + 1]) + frame[2:]
    blob = frame + crc16_ccitt(frame)
    slots = parse_profile_names(blob, NON_STRIKER)
    assert [(s.index, s.name) for s in slots] == [(1, "Ada")]


def test_striker_22_byte_entries_with_mug():
    def entry(name, icon, mug):
        return name.encode("utf-16-be")[:20].ljust(20, b"\x00") + bytes([icon, mug])

    body = bytes([1, 2]) + entry("One", 2, 7) + entry("Two", 3, 0)
    frame = bytes([0xD0, 0x00, 0xA4, 0xF0]) + body
    frame = frame[:1] + bytes([len(frame) + 1]) + frame[2:]
    blob = frame + crc16_ccitt(frame)
    slots = parse_profile_names(blob, STRIKER)
    assert [(s.index, s.name, s.icon, s.mug) for s in slots] == [
        (1, "One", 2, 7),
        (2, "Two", 3, 0),
    ]


def test_malformed_blobs_yield_no_slots():
    assert parse_profile_names(b"", NON_STRIKER) == []
    assert parse_profile_names(b"\x00" * 20, NON_STRIKER) == []
    good = _blob("lan_d034_profiles_1_3")
    assert parse_profile_names(good[:-1], NON_STRIKER) == []  # truncated


def test_parse_a9_ack():
    # profile_switch_alice data_response: d0 07 a9 f0 01 00 <crc> (+ts trailer)
    blob = bytes.fromhex("d0 07 a9 f0 01 00 3b 3c")
    assert parse_a9_ack(blob) == (1, True)
    # trailer-tolerant
    with_trailer = blob + b"\xde\xad\xbe\xef"
    assert parse_a9_ack(with_trailer) == (1, True)


def test_parse_a9_ack_negative():
    body = bytes([0xD0, 0x07, 0xA9, 0xF0, 0x02, 0x01])
    blob = body + crc16_ccitt(body)
    assert parse_a9_ack(blob) == (2, False)
    with pytest.raises(ValueError):
        parse_a9_ack(b"\xd0\x07\x83\xf0\x01\x00\x00\x00")
