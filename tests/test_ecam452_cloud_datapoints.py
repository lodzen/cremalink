"""ECAM452 over the cloud: settings, profile names and the selected profile.

The cloud has no response mailbox and no ``request_property``. The machine
republishes its answers as named ``dNNN`` properties instead, and the
ECAM452 names differ from the Soul's (``d282_mach_sett_auto_off`` vs.
``d282_mchn_sett_aoff``).
"""

from __future__ import annotations

import base64
from pathlib import Path

from cremalink.core.binary import crc16_ccitt
from cremalink.domain.device import Device
from cremalink.ecam.settings import SETTING_OPTION_MAPS

DEVICES = Path(__file__).resolve().parents[1] / "cremalink" / "devices"


def _answer(content: bytes) -> bytes:
    frame = bytes([0xD0, len(content) + 3]) + content
    return frame + crc16_ccitt(frame)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _setting_blob(param_id: int, value: int, flag: int = 0x0F) -> bytes:
    return _answer(
        bytes([0x95, flag]) + param_id.to_bytes(2, "big") + value.to_bytes(4, "big")
    )


def _profile_names_blob(first: int, names: list[str]) -> bytes:
    body = bytes([first, first + len(names) - 1])
    for name in names:
        body += name.encode("utf-16-be")[:20].ljust(20, b"\x00") + bytes([1, 0])
    frame = bytes([0xD0, 0x00, 0xA4, 0xF0]) + body
    frame = frame[:1] + bytes([len(frame) + 1]) + frame[2:]
    return frame + crc16_ccitt(frame)


class CloudLikeTransport:
    """Mirrors CloudTransport: named properties only, no mailbox."""

    def __init__(self, properties: dict[str, str]):
        self.properties = properties
        self.sent: list[tuple[bytes, str | None]] = []

    def get_property(self, name):
        value = self.properties.get(name)
        return {"name": name, "value": value} if value is not None else None

    def send_command(self, command, alternative_property=None):
        self.sent.append((base64.b64decode(command), alternative_property))
        if alternative_property == "app_device_connected":
            self.properties["app_id"] = "-1056968978"  # C0FFEEEE registered
        return {}

    def request_property(self, name):
        raise NotImplementedError("request_property is LAN-only")

    def pop_response(self):
        raise NotImplementedError("pop_response is LAN-only")

    def configure(self):
        pass


def _device(properties: dict[str, str]) -> Device:
    # Our app id is already registered, so no test waits on a registration.
    properties = {"app_id": "-1056968978", **properties}
    dev = Device.from_map(
        CloudLikeTransport(properties), device_map_path=str(DEVICES / "ECAM452.json")
    )
    dev.app_id_confirm_seconds = 0.0
    dev.app_id_confirm_own_seconds = 0.0
    dev.app_id_confirm_poll = 0.0
    dev.response_poll = 0
    return dev


def test_auto_off_hint_matches_soul_and_ecam452_names():
    hint = SETTING_OPTION_MAPS["auto_off"].datapoint_hint
    assert hint in "d282_mchn_sett_aoff"
    assert hint in "d282_mach_sett_auto_off"


def test_settings_read_from_ecam452_named_properties():
    dev = _device(
        {
            "d282_mach_sett_auto_off": _b64(_setting_blob(0x003E, 3)),
            "d283_mach_sett_water_hard": _b64(_setting_blob(0x0032, 2)),
        }
    )
    settings = dev.get_settings()
    assert settings["auto_off"] == 3
    assert settings["water_hardness"] == 2


def test_profiles_read_from_ecam452_profile_name_properties():
    dev = _device(
        {
            "d051_profile_name1_3": _b64(_profile_names_blob(1, ["Ada", "Bo", "Cy"])),
            "d052_profile_name4": _b64(_profile_names_blob(4, ["Di"])),
        }
    )
    slots = dev.get_profiles()
    assert [(s.index, s.name) for s in slots] == [
        (1, "Ada"),
        (2, "Bo"),
        (3, "Cy"),
        (4, "Di"),
    ]


def test_current_profile_read_from_republished_property():
    dev = _device({"d286_mach_sett_profile": _b64(_setting_blob(0x03EE, 1, flag=0xF0))})
    assert dev.refresh_current_profile(force=True) == 2  # register is 0-based
    # A fresh 0x95 read for 0x03EE was requested.
    reads = [f for f, _ in dev.transport.sent if f[2] == 0x95]
    assert reads and reads[-1][4:6] == bytes([0x03, 0xEE])
