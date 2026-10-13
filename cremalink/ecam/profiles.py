"""
Profile-name (``0xA4``) blob parsing and the ``0xA9`` select path.

``a4f0`` envelope (verified on ``d034``/``d035`` Soul blobs)::

    D0 <len> A4 F0 <first_slot:1B> <last_slot:1B> <entries…> <trailing:1B> <crc>

Each entry is 21 bytes on non-striker machines (``<name:20B UTF-16BE>
<icon:1B>``) and 22 on striker (trailing mug byte). Empty/all-padding
slots are excluded from the option list; factory names remain occupied
(FR-018). Icon values >11 exist on Soul — they pass through unmapped,
never through the Eletta ÷3-color/%3-figure scheme.
"""

from __future__ import annotations

from dataclasses import dataclass

from cremalink.ecam.builder import trim_frame, verify_frame
from cremalink.ecam.machine_profiles import MachineProfile


@dataclass(frozen=True)
class ProfileSlot:
    """One occupied machine profile slot."""

    index: int  # 1-based profile number
    name: str
    icon: int
    mug: int | None = None  # striker 22B layout only


def _decode_name(raw: bytes) -> str | None:
    """UTF-16BE fixed-slot decode; ``None`` for empty/all-padding slots."""
    import unicodedata

    try:
        text = raw.decode("utf-16-be", errors="ignore")
    except (UnicodeDecodeError, ValueError):
        return None
    name = text.split("\x00", 1)[0].strip()
    # Slots padded with 0x00 or 0xff decode to control/noncharacters —
    # a name that contains nothing printable is not a name.
    if not name or not any(not unicodedata.category(ch).startswith("C") for ch in name):
        return None
    return name


def parse_profile_names(
    blob: bytes | bytearray, profile: MachineProfile
) -> list[ProfileSlot]:
    """Parse one ``a4f0`` envelope into occupied :class:`ProfileSlot`\\ s.

    Never raises on malformed input — unparseable content yields ``[]``
    (callers treat a missing page as "no slots", never a crash).
    """
    frame = trim_frame(blob)
    if not verify_frame(frame) or frame[0] != 0xD0 or frame[2] != 0xA4:
        return []
    payload = frame[4:-2]
    if len(payload) < 2:
        return []
    first, last = payload[0], payload[1]
    entry_size = profile.profile_entry_size
    slots: list[ProfileSlot] = []
    body = payload[2:]
    for i in range(len(body) // entry_size):
        entry = body[i * entry_size : (i + 1) * entry_size]
        name = _decode_name(entry[:20])
        if name is None:
            continue
        index = first + i
        if index > last:
            break
        slots.append(
            ProfileSlot(
                index=index,
                name=name,
                icon=entry[20],
                mug=entry[21] if entry_size == 22 else None,
            )
        )
    return slots
