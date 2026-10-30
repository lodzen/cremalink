"""
Writable machine settings — ``0x95`` reads and ``0x90`` writes.

Four ``0x95`` answer shapes were observed on Soul LAN (§3):

- short stored-value blob: ``95 0f <param:2B> <u32>`` (the republished
  ``dNNN`` backing property, e.g. ``d282_mchn_sett_aoff``);
- full republish: ``95 f0 <param:2B> <u32> <descriptor tail…>``;
- ack echo: ``95 f0 <param:2B>`` (2-byte payload, no value);
- unknown parameter: 36 zero bytes (no entry produced).

The APK ``q0()`` list parser (``count=(len-7)/4`` consecutive u32s from
``start=u16@[4,5]``) covers the value-bearing shapes uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass

from cremalink.ecam.builder import trim_frame, verify_frame


@dataclass(frozen=True)
class WritableSetting:
    """A capability-gated machine setting writable via ``0x90``."""

    key: str
    param_id: int
    options: dict[int, str]  # raw index -> label
    capability: str  # device-map `capabilities` flag gating the HA select
    datapoint_hint: str = ""  # substring of the republished dNNN backing property


#: Verified option maps (auto-off labels from the machine display; water
#: hardness stores a 0-based index, displayed level = raw+1).
SETTING_OPTION_MAPS: dict[str, WritableSetting] = {
    "auto_off": WritableSetting(
        key="auto_off",
        param_id=0x003E,
        options={0: "15 min", 1: "30 min", 2: "1 h", 3: "3 h"},
        capability="auto_off_settings",
        datapoint_hint="aoff",
    ),
    "water_hardness": WritableSetting(
        key="water_hardness",
        param_id=0x0032,
        options={0: "level_1", 1: "level_2", 2: "level_3", 3: "level_4"},
        capability="water_hardness_settings",
        datapoint_hint="water",
    ),
}


@dataclass(frozen=True)
class BitSetting:
    """A boolean flag inside a shared parameter word (``0x90`` write).

    ``inverted=True`` means the set bit carries the OFF semantics (the
    APK's ``Parameter.f()`` autostart: bit set = autostart disabled).
    """

    key: str
    param_id: int
    mask: int
    capability: str  # device-map `capabilities` flag gating the HA entity
    datapoint_hint: str = ""  # substring of the republished backing property
    inverted: bool = False


#: Param ``0x003F`` (``d284_mchn_sett_user_conf``) bits — decoded from the
#: APK's ``it/delonghi/ecam/model/Parameter.java`` (protocol.md §14.8):
#: ``f()`` autostart ``0x01`` (inverted), ``g()`` beep ``0x04``,
#: ``h()`` cup light ``0x08``, ``j()`` energy saving ``0x10``,
#: ``i()`` cup warmer ``0x20``. Live value ``0x1D`` = beep+energy ON.
SETTING_BIT_MAPS: dict[str, BitSetting] = {
    "beep": BitSetting(
        key="beep",
        param_id=0x003F,
        mask=0x04,
        capability="buzzer_settings",
        datapoint_hint="user_conf",
    ),
    "energy_saving": BitSetting(
        key="energy_saving",
        param_id=0x003F,
        mask=0x10,
        capability="energy_saving_settings",
        datapoint_hint="user_conf",
    ),
    "cup_light": BitSetting(
        key="cup_light",
        param_id=0x003F,
        mask=0x08,
        capability="cup_light_settings",
        datapoint_hint="user_conf",
    ),
    "cup_warmer": BitSetting(
        key="cup_warmer",
        param_id=0x003F,
        mask=0x20,
        capability="cup_warmer_settings",
        datapoint_hint="user_conf",
    ),
    "autostart": BitSetting(
        key="autostart",
        param_id=0x003F,
        mask=0x01,
        capability="auto_start_settings",
        datapoint_hint="user_conf",
        inverted=True,
    ),
}


def settings_bits(param_id: int, param_raw: int) -> dict[str, bool]:
    """Decode a param word into ``{key: on/off}`` for its bit settings.

    Only settings backed by ``param_id`` are decoded — inversion-aware
    (autostart's set bit means OFF).
    """
    out: dict[str, bool] = {}
    for key, setting in SETTING_BIT_MAPS.items():
        if setting.param_id != param_id:
            continue
        on = bool(param_raw & setting.mask)
        out[key] = not on if setting.inverted else on
    return out


def merge_setting_bit(param_raw: int, setting: BitSetting, on: bool) -> int:
    """Return ``param_raw`` with ``setting``'s flag set to ``on``.

    ``on`` is the logical state (autostart ``on=True`` clears its inverted
    bit); every unrelated bit is preserved — the merged u32 is what goes
    on the wire in the ``0x90`` write.
    """
    target = on != setting.inverted
    if target:
        return (param_raw | setting.mask) & 0xFFFFFFFF
    return (param_raw & ~setting.mask) & 0xFFFFFFFF


def parse_settings(blob: bytes | bytearray) -> dict[int, int]:
    """Parse a ``0x95`` answer/stored blob into ``{param_id: raw_u32}``.

    Shape-tolerant: short stored values, full republish blobs (descriptor
    tail ignored), ack echoes (no values) and all-zero unknown-parameter
    blobs are all handled without raising.
    """
    frame = trim_frame(blob)
    if not verify_frame(frame) or frame[0] != 0xD0 or frame[2] != 0x95:
        return {}
    payload = frame[4:-2]
    if len(payload) < 6 or not any(payload):
        # ack echo without value (`95 f0 <param:2B>`) or the all-zero
        # unknown-parameter answer — neither carries an entry.
        return {}
    start = int.from_bytes(payload[0:2], "big")
    values_blob = payload[2:]
    out: dict[int, int] = {}
    for off in range(0, len(values_blob) - 3, 4):
        out[start + off // 4] = int.from_bytes(values_blob[off : off + 4], "big")
    return out
