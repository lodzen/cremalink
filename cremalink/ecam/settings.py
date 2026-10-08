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


#: Verified option maps (auto-off labels from the machine display; water
#: hardness stores a 0-based index, displayed level = raw+1).
SETTING_OPTION_MAPS: dict[str, WritableSetting] = {
    "auto_off": WritableSetting(
        key="auto_off",
        param_id=0x003E,
        options={0: "15 min", 1: "30 min", 2: "1 h", 3: "3 h"},
        capability="auto_off_settings",
    ),
    "water_hardness": WritableSetting(
        key="water_hardness",
        param_id=0x0032,
        options={0: "level_1", 1: "level_2", 2: "level_3", 3: "level_4"},
        capability="water_hardness_settings",
    ),
}


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
