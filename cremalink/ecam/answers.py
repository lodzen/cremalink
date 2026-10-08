"""
Dispatch and parse machine answer (``0xD0``) frames.

Every ``0xD0`` blob echoes the request id at ``byte[2]`` and the request's
flag at ``byte[3]`` — response routing keys on that echo, never on the
property the blob arrived under (APK ``b1()``/``L()`` dispatcher rule).

Special answer ids: ``0x90`` param-write ack, ``0x95`` param list,
``0xA1``/``0xA2`` 6-byte ``<id:2B><u32>`` records, ``0xA9`` profile-select
ack, ``0xE1`` sync-timeout/request-abort (the app retries after ~100 ms).
"""

from __future__ import annotations

from cremalink.core.binary import crc16_ccitt
from cremalink.ecam.builder import ANSWER_PREFIX, ANSWER_SYNC_TIMEOUT


def _payload(frame: bytes) -> bytes:
    """Validate an answer frame and return ``<id><flag><body…>`` content.

    Raises ``ValueError`` on a non-answer, truncated, length-mismatched or
    CRC-invalid blob — an unverified frame must never be dispatched.

    ``data_response`` datapoints append a 4-byte timestamp after the CRC;
    bytes beyond the declared frame length are ignored, not rejected.
    """
    if len(frame) < 5 or frame[0] != ANSWER_PREFIX:
        raise ValueError("not a 0xD0 answer frame")
    declared = frame[1] + 1
    if declared > len(frame):
        raise ValueError("answer frame length byte mismatch")
    frame = frame[:declared]
    if crc16_ccitt(frame[:-2]) != frame[-2:]:
        raise ValueError("answer frame CRC invalid")
    return frame[2:-2]


def answer_id(frame: bytes | bytearray) -> int:
    """The echoed request id — ``byte[2]`` of the ``0xD0`` blob."""
    return _payload(bytes(frame))[0]


def answer_flag(frame: bytes | bytearray) -> int:
    """The echoed request flag — ``byte[3]`` of the ``0xD0`` blob."""
    return _payload(bytes(frame))[1]


def is_sync_timeout(frame: bytes | bytearray) -> bool:
    """``0xE1`` — machine reports busy/sync-timeout; abort the pending read.

    Tolerant by design: a blob that does not parse cannot be an abort.
    """
    try:
        return answer_id(frame) == ANSWER_SYNC_TIMEOUT
    except ValueError:
        return False


def parse_write_ack(frame: bytes | bytearray) -> tuple[int, bool]:
    """``0x90`` ack → ``(param_id, ok)`` — payload ``<param:2B> <ok:1B>``.

    Ack byte ``0x00`` means the write committed (``b==0`` ⇒ success per
    the APK dispatcher).
    """
    content = _payload(bytes(frame))
    if content[0] != 0x90:
        raise ValueError(f"not a write ack: answer id 0x{content[0]:02x}")
    if len(content) < 5:
        raise ValueError("truncated write ack")
    param_id = int.from_bytes(content[2:4], "big")
    return param_id, content[4] == 0


def parse_a9_ack(frame: bytes | bytearray) -> tuple[int, bool]:
    """``0xA9`` profile-select ack → ``(profile, ok)`` — ``<prof> <ok=00>``."""
    content = _payload(bytes(frame))
    if content[0] != 0xA9:
        raise ValueError(f"not a profile-select ack: answer id 0x{content[0]:02x}")
    if len(content) < 4:
        raise ValueError("truncated profile-select ack")
    return content[2], content[3] == 0


def parse_simple_ack(frame: bytes | bytearray) -> tuple[int, bool]:
    """Generic ``<ok:1B>`` ack — used by ``0x83``/``0xA5``/``0xAB``/``0xB9``/
    ``0xBB``/``0xE2`` answers. Returns ``(answer_id, ok)``."""
    content = _payload(bytes(frame))
    if len(content) < 3:
        raise ValueError("truncated ack")
    return content[0], content[2] == 0


def decode_a2_page(frame: bytes | bytearray) -> list[tuple[int, int]]:
    """``0xA2``/``0xA1`` statistics page → ``[(id, raw_u32), …]``.

    Records are 6 bytes ``<id:2B> <u32:4B>`` starting at payload offset 2
    (APK ``h.u`` parser); trailing partial records are ignored.
    """
    content = _payload(bytes(frame))
    if content[0] not in (0xA1, 0xA2):
        raise ValueError(f"not a statistics page: answer id 0x{content[0]:02x}")
    body = content[2:]
    out: list[tuple[int, int]] = []
    for off in range(0, len(body) - 5, 6):
        stat_id = int.from_bytes(body[off : off + 2], "big")
        value = int.from_bytes(body[off + 2 : off + 6], "big")
        out.append((stat_id, value))
    return out
