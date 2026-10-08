"""
Parametric ECAM command-frame construction.

Wire frame layout (verified against the decompiled Coffee Link builders
and live LAN captures)::

    <0x0D> <len:1B> <family:2B> <payload…> <crc16:2B>

``len`` counts the bytes from the length byte itself through the CRC —
i.e. ``len == total_frame_length - 1``; it is derived from emitted
content so variable-length payloads are supported (FR-003). The CRC is
``crc16_ccitt`` (init ``0x1D0F``, poly ``0x1021``) over every byte
preceding it (FR-004/008).

Transport encoding appends a 4-byte big-endian unix timestamp (never
CRC-covered) and, on striker machines, the registered 4-byte app-id
trailer (FR-005/007).

All functions are pure: no I/O, no network, no Home Assistant imports.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Iterable
from enum import Enum

from cremalink.core.binary import crc16_ccitt
from cremalink.ecam.machine_profiles import (
    TRAILER_APP_ID,
    MachineProfile,
)

CMD_PREFIX = 0x0D
ANSWER_PREFIX = 0xD0

# Request ids (the high byte of the family pair).
REQ_MONITOR = 0x75
REQ_BREW = 0x83
REQ_POWER = 0x84
REQ_WRITE_PARAM = 0x90
REQ_READ_PARAM = 0x95
REQ_READ_EXTENDED = 0xA1
REQ_STATISTICS = 0xA2
REQ_PROFILE_NAMES = 0xA4
REQ_PROFILE_NAMES_WRITE = 0xA5
REQ_RECIPE_VALUES = 0xA6
REQ_PRIORITY_LIST = 0xA8
REQ_PROFILE_SELECT = 0xA9
REQ_RECIPE_NAMES = 0xAA
REQ_FACTORY_DESCRIPTOR = 0xB0
REQ_BEAN_SYSTEM = 0xBA
REQ_STRIKER_HANDSHAKE = 0xE8

ANSWER_SYNC_TIMEOUT = 0xE1

#: Human-readable labels for request ids (diagnostics/logging).
REQUEST_NAMES = {
    REQ_MONITOR: "monitor",
    REQ_BREW: "brew",
    REQ_POWER: "power",
    REQ_WRITE_PARAM: "write_param",
    REQ_READ_PARAM: "read_param",
    REQ_READ_EXTENDED: "read_extended",
    REQ_STATISTICS: "statistics",
    REQ_PROFILE_NAMES: "profile_names",
    REQ_PROFILE_NAMES_WRITE: "profile_names_write",
    REQ_RECIPE_VALUES: "recipe_values",
    REQ_PRIORITY_LIST: "priority_list",
    REQ_PROFILE_SELECT: "profile_select",
    REQ_RECIPE_NAMES: "recipe_names",
    REQ_FACTORY_DESCRIPTOR: "factory_descriptor",
    REQ_BEAN_SYSTEM: "bean_system",
    REQ_STRIKER_HANDSHAKE: "striker_handshake",
}

#: Param ids excluded from a brew recipe by the app's ``O()`` builder:
#: param 8 (``DUExPER``) is always dropped; the ``E0`` predicate then keeps
#: only ids < 23 or in {27, 28, 31, 33, 38, 39} (INDEX_LENGTH, ACCESSORIO,
#: ICED, MUG_ADJUST, INTENSITY, RINSE). On the verified Soul corpus the two
#: rules together remove tags {0x08, 0x19}.
BREW_DROP_TAGS = frozenset({0x08})
BREW_KEEP_EXTENDED = frozenset({27, 28, 31, 33, 38, 39})

#: Default recipe TLVs (captured hot-water command: 250 ml, temp index 1).
#: The trailing brew trailer byte is NOT part of the recipe — ``build_brew``
#: appends ``(profile_slot<<2)|accessory`` itself. Opaque bytes — brews are
#: normally built from the machine's own ``0xA6`` recipe or a command-map
#: entry; this is only a safe fallback.
DEFAULT_RECIPE = bytes([0x0F, 0x00, 0xFA, 0x1B, 0x01])


def brew_param_included(param_id: int) -> bool:
    """The app-side ``E0`` predicate: which recipe params go on the wire."""
    if param_id in BREW_DROP_TAGS:
        return False
    return param_id < 23 or param_id in BREW_KEEP_EXTENDED


class BrewAction(int, Enum):
    """Action/strength byte of a ``0x83`` brew frame."""

    START = 0x01
    STOP = 0x02


class PowerCommand(bytes, Enum):
    """Payloads of the ``0x84 0x0F`` power/session family (verified)."""

    STANDBY = b"\x01\x01"
    WAKE = b"\x02\x01"
    SESSION_REFRESH = b"\x03\x02"


def _require_profile(profile: MachineProfile | None) -> MachineProfile:
    if not isinstance(profile, MachineProfile):
        raise TypeError(
            "a bound MachineProfile is required to construct frames "
            "(fail closed per FR-006)"
        )
    return profile


def _frame(request_id: int, flag: int, payload: bytes) -> bytes:
    """Assemble ``<0x0D> <len> <request_id> <flag> <payload> <crc16>``."""
    body = bytes([request_id & 0xFF, flag & 0xFF]) + bytes(payload)
    length = len(body) + 3  # len byte + body + 2 CRC bytes
    head = bytes([CMD_PREFIX, length]) + body
    return head + crc16_ccitt(head)


def trim_frame(blob: bytes | bytearray) -> bytes:
    """Cut a received blob to its declared frame, dropping any trailer.

    ``data_response``/republished datapoints append a 4-byte timestamp
    after the CRC — the trailing bytes are transport metadata, not frame.
    """
    b = bytes(blob)
    if len(b) < 2:
        return b
    declared = b[1] + 1
    return b[:declared] if declared <= len(b) else b


def verify_frame(frame: bytes | bytearray) -> bool:
    """Validate a wire frame's length byte and CRC.

    Bytes beyond the declared length (e.g. the datapoint timestamp
    trailer) are ignored. Returns ``False`` on any structural violation
    — never raises.
    """
    frame = trim_frame(frame)
    if len(frame) < 5 or frame[0] not in (CMD_PREFIX, ANSWER_PREFIX):
        return False
    if frame[1] != len(frame) - 1:
        return False
    return crc16_ccitt(frame[:-2]) == frame[-2:]


def build_brew(
    beverage_id: int,
    action: int | BrewAction,
    recipe: bytes | bytearray | Iterable[int],
    profile: MachineProfile,
    *,
    profile_slot: int = 0,
    accessory: int = 0,
) -> bytes:
    """Build a ``0x83`` brew frame.

    ``0D LEN 83 F0 <bev> <action> <recipe…> <(profile_slot<<2)|accessory>``
    — the frame names its profile in the trailer byte; the recipe bytes
    must be that same profile's values (``0xA6`` reads are profile-scoped).
    """
    _require_profile(profile)
    if not 0 <= beverage_id <= 0xFF:
        raise ValueError(f"beverage_id out of range: {beverage_id!r}")
    action_i = int(action)
    if not 0 <= action_i <= 0xFF:
        raise ValueError(f"action out of range: {action_i!r}")
    recipe_b = bytes(recipe)
    if len(recipe_b) > 200:
        raise ValueError(f"recipe payload too large: {len(recipe_b)} bytes")
    if not 0 <= profile_slot <= 0x3F:
        raise ValueError(f"profile_slot out of range: {profile_slot!r}")
    tail = (profile_slot << 2) | (accessory & 0x03)
    return _frame(
        REQ_BREW, 0xF0, bytes([beverage_id, action_i]) + recipe_b + bytes([tail])
    )


def build_power(kind: PowerCommand, profile: MachineProfile) -> bytes:
    """Build an ``0x84 0x0F`` power/session frame."""
    _require_profile(profile)
    if not isinstance(kind, PowerCommand):
        raise TypeError(f"unknown power command: {kind!r}")
    return _frame(REQ_POWER, 0x0F, kind.value)


def build_write_param(
    param_id: int, value: int, profile: MachineProfile, *, flag: int = 0xF0
) -> bytes:
    """Build a ``0x90`` parameter-write frame.

    ``0D 0B 90 <flag> <param:2B> <u32:4B>`` — the live-verified write used
    flag ``0xF0`` for a <1000 param (the documented ``param>=1000 ? 0xF0 :
    0x0F`` rule from the decompiled notes does not match the capture; the
    capture wins). ``flag`` stays overridable for params that prove to
    need the stored-value form.
    """
    _require_profile(profile)
    if not 0 <= param_id <= 0xFFFF:
        raise ValueError(f"param_id out of range: {param_id!r}")
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"value out of range: {value!r}")
    payload = param_id.to_bytes(2, "big") + value.to_bytes(4, "big")
    return _frame(REQ_WRITE_PARAM, flag, payload)


def build_read(
    request_id: int,
    args: bytes | bytearray | Iterable[int] = b"",
    profile: MachineProfile | None = None,
    *,
    flag: int | None = None,
) -> bytes:
    """Build a read-style request frame for the given request id.

    ``args`` is the raw payload after the flag byte. Flags: ``0x0F`` for
    the statistics family, ``0xF0`` (answer wanted) elsewhere — both
    verified on live LAN. Use the ``build_read_*`` helpers below for the
    structured payloads.
    """
    _require_profile(profile)
    if not 0 <= request_id <= 0xFF:
        raise ValueError(f"request_id out of range: {request_id!r}")
    if flag is None:
        flag = 0x0F if request_id == REQ_STATISTICS else 0xF0
    return _frame(request_id, flag, bytes(args))


def build_read_param(param_id: int, profile: MachineProfile) -> bytes:
    """``0D 07 95 F0 <param:2B>`` — single parameter read (captured)."""
    if not 0 <= param_id <= 0xFFFF:
        raise ValueError(f"param_id out of range: {param_id!r}")
    return build_read(REQ_READ_PARAM, param_id.to_bytes(2, "big"), profile)


def build_read_params(
    first_param: int, last_param: int, profile: MachineProfile
) -> bytes:
    """``0D 09 95 F0 <first:2B> <last:2B>`` — param range read (captured)."""
    for p in (first_param, last_param):
        if not 0 <= p <= 0xFFFF:
            raise ValueError(f"param id out of range: {p!r}")
    return build_read(
        REQ_READ_PARAM,
        first_param.to_bytes(2, "big") + last_param.to_bytes(2, "big"),
        profile,
    )


def build_read_block(param_id: int, count: int, profile: MachineProfile) -> bytes:
    """``0D 08 <0x95|0xA1> <flag> <param:2B> <count>`` — APK ``r0`` block read.

    ``count > 4`` goes out as the ``0xA1`` extended read; the machine
    normalises the answer back to a ``0x95`` param list.
    """
    if not 0 <= param_id <= 0xFFFF:
        raise ValueError(f"param_id out of range: {param_id!r}")
    if not 1 <= count <= 10:
        raise ValueError(f"count out of range: {count!r}")
    request_id = REQ_READ_PARAM if count <= 4 else REQ_READ_EXTENDED
    flag = 0xF0 if param_id >= 1000 else 0x0F
    return build_read(
        request_id, param_id.to_bytes(2, "big") + bytes([count]), profile, flag=flag
    )


def build_statistics_page(start_id: int, count: int, profile: MachineProfile) -> bytes:
    """``0D 08 A2 0F <start_id:2B> <count:1B>`` — statistics page read."""
    if not 0 <= start_id <= 0xFFFF:
        raise ValueError(f"start_id out of range: {start_id!r}")
    if not 1 <= count <= 10:
        raise ValueError(f"count out of range: {count!r}")
    return build_read(
        REQ_STATISTICS, start_id.to_bytes(2, "big") + bytes([count]), profile
    )


def build_profile_names(first: int, count: int, profile: MachineProfile) -> bytes:
    """``0D 07 A4 F0 <first> <count>`` — profile-name read (APK ``t0``)."""
    if not 0 <= first <= 0xFF or not 0 <= count <= 0xFF:
        raise ValueError("profile-name range out of bounds")
    return build_read(REQ_PROFILE_NAMES, bytes([first, count]), profile)


def build_recipe_values(
    profile_slot: int, beverage_id: int, profile: MachineProfile
) -> bytes:
    """``0D 07 A6 F0 <prof> <bev>`` — profile-scoped recipe read (APK ``M0``)."""
    if not 0 <= profile_slot <= 0xFF or not 0 <= beverage_id <= 0xFF:
        raise ValueError("recipe read args out of bounds")
    return build_read(REQ_RECIPE_VALUES, bytes([profile_slot, beverage_id]), profile)


def build_recipe_names(first: int, count: int, profile: MachineProfile) -> bytes:
    """``0D 07 AA F0 <first> <count>`` — custom recipe-name read (APK ``v0``)."""
    if not 0 <= first <= 0xFF or not 0 <= count <= 0xFF:
        raise ValueError("recipe-name range out of bounds")
    return build_read(REQ_RECIPE_NAMES, bytes([first, count]), profile)


def build_factory_descriptor(beverage_id: int, profile: MachineProfile) -> bytes:
    """``0D 06 B0 F0 <bev>`` — min/default/max descriptor read (APK ``Y0``)."""
    if not 0 <= beverage_id <= 0xFF:
        raise ValueError(f"beverage_id out of range: {beverage_id!r}")
    return build_read(REQ_FACTORY_DESCRIPTOR, bytes([beverage_id]), profile)


def build_priority_list(profile_slot: int, profile: MachineProfile) -> bytes:
    """``0D 06 A8 F0 <prof>`` — per-profile ordered beverage list."""
    if not 0 <= profile_slot <= 0xFF:
        raise ValueError(f"profile_slot out of range: {profile_slot!r}")
    return build_read(REQ_PRIORITY_LIST, bytes([profile_slot]), profile)


def build_bean_system(index: int, profile: MachineProfile) -> bytes:
    """``0D 06 BA F0 <index>`` — bean-system slot read (APK ``U``)."""
    if not 0 <= index <= 0xFF:
        raise ValueError(f"bean-system index out of range: {index!r}")
    return build_read(REQ_BEAN_SYSTEM, bytes([index]), profile)


def build_profile_select(profile_slot: int, profile: MachineProfile) -> bytes:
    """``0D 06 A9 F0 <prof>`` — profile select (verified live).

    This is a *gated write*: the session must be announced first.
    """
    _require_profile(profile)
    if not 0 <= profile_slot <= 0xFF:
        raise ValueError(f"profile_slot out of range: {profile_slot!r}")
    return _frame(REQ_PROFILE_SELECT, 0xF0, bytes([profile_slot]))


def build_striker_handshake(profile: MachineProfile) -> bytes:
    """``0D 06 E8 F0 00`` — striker session handshake (APK ``z0``)."""
    _require_profile(profile)
    return _frame(REQ_STRIKER_HANDSHAKE, 0xF0, b"\x00")


def build_monitor_request(profile: MachineProfile) -> bytes:
    """``0D 05 <monitor_id> 0F`` — monitor snapshot request (APK ``V``)."""
    _require_profile(profile)
    request_id = min(profile.monitor_request_ids)
    return _frame(request_id, 0x0F, b"")


def describe_request_frame(blob: bytes | bytearray) -> dict:
    """Summarize an outbound (``0x0D``) request frame for logging.

    Best-effort and never raises: the returned dict always has a ``frame``
    hex string; a well-formed request adds ``request_id``, ``request``
    (label from :data:`REQUEST_NAMES`), ``flag``, ``params`` (hex) and
    ``crc_ok``. Transport trailers (timestamp/app-id) are trimmed first.
    """
    frame = trim_frame(bytes(blob))
    out: dict = {"frame": frame.hex()}
    if len(frame) >= 5 and frame[0] == CMD_PREFIX and frame[1] == len(frame) - 1:
        out.update(
            {
                "request_id": frame[2],
                "request": REQUEST_NAMES.get(frame[2], f"0x{frame[2]:02x}"),
                "flag": frame[3],
                "params": frame[4:-2].hex(),
                "crc_ok": verify_frame(frame),
            }
        )
    else:
        out["request"] = None
    return out


def encode_for_transport(
    frame: bytes | bytearray,
    profile: MachineProfile,
    *,
    timestamp: int | None = None,
    app_id: bytes | bytearray | None = None,
) -> str:
    """Base64-encode ``frame + <ts:4B BE> [+ <app_id:4B>]`` for the wire.

    The timestamp is never CRC-covered; the striker app-id trailer follows
    it (FR-004/005/007).
    """
    _require_profile(profile)
    frame_b = bytes(frame)
    if not verify_frame(frame_b):
        raise ValueError("encode_for_transport given an invalid frame")
    ts = int(time.time()) if timestamp is None else int(timestamp)
    payload = frame_b + ts.to_bytes(4, "big")
    if profile.trailer == TRAILER_APP_ID:
        if app_id is None:
            raise ValueError("striker profile requires an app_id trailer")
        app_id_b = bytes(app_id)
        if len(app_id_b) != 4:
            raise ValueError("striker app_id trailer must be 4 bytes")
        payload += app_id_b
    return base64.b64encode(payload).decode("utf-8")
