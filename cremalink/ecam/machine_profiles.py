"""
Machine dialect profiles for the ECAM protocol layer.

Different De'Longhi machine families speak slightly different dialects of the
same Ayla/ECAM protocol. Rather than scattering ``if model == ...`` checks
throughout the code, each family's differences live in one frozen
:class:`MachineProfile` here, bound explicitly via the ``machine_profile``
field of the device map (FR-006).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Transport trailer appended after the 4-byte big-endian timestamp.
TRAILER_NONE = "none"
TRAILER_APP_ID = "app_id"

# How the profile establishes the session that gates writes.
SESSION_DEVICE_CONNECTED_TS = "device_connected_ts"
SESSION_E8_APPID = "e8_appid"


@dataclass(frozen=True)
class MachineProfile:
    """Dialect declaration a device binds to via ``machine_profile``.

    Fields:

    - ``key``: stable identifier (``"non_striker"`` / ``"striker"``).
    - ``command_property``: property frames are written to
      (``data_request`` vs ``app_data_request``).
    - ``trailer``: transport suffix after the timestamp
      (``NONE`` timestamp only, ``APP_ID`` 4-byte registered app id).
    - ``session``: write-gate mechanism (plain unix timestamp write to
      ``device_connected`` vs ``0xE8`` handshake + ``84 0f 03 02``
      registration).
    - ``monitor_request_ids``: request ids that carry monitor frames
      (the device map's ``monitor_profile`` stays authoritative per
      data-model.md; this is informational).
    - ``profile_entry_size``: bytes per profile entry in ``0xA4`` blobs
      (21 non-striker / 22 striker incl. mug byte).
    - ``grinder_scale``: multiplier applied to bean-system grinder values
      on striker (write ``*2``, read ``/2`` per APK ``O0``/``G0``).
    """

    key: str
    command_property: str
    trailer: str
    session: str
    monitor_request_ids: frozenset[int]
    profile_entry_size: int
    grinder_scale: float


NON_STRIKER = MachineProfile(
    key="non_striker",
    command_property="data_request",
    trailer=TRAILER_NONE,
    session=SESSION_DEVICE_CONNECTED_TS,
    monitor_request_ids=frozenset({0x75}),
    profile_entry_size=21,
    grinder_scale=1.0,
)

STRIKER = MachineProfile(
    key="striker",
    command_property="app_data_request",
    trailer=TRAILER_APP_ID,
    session=SESSION_E8_APPID,
    monitor_request_ids=frozenset({0x60, 0x70}),
    profile_entry_size=22,
    grinder_scale=2.0,
)

_PROFILES: dict[str, MachineProfile] = {
    NON_STRIKER.key: NON_STRIKER,
    STRIKER.key: STRIKER,
}


def profile_from_map(
    map_data: dict[str, Any], *, logger_: logging.Logger | None = None
) -> MachineProfile:
    """Bind the device map's ``machine_profile`` field to a preset.

    A missing field binds :data:`NON_STRIKER` with a logged warning (the
    verified Soul behaviour). An unrecognized value raises ``ValueError``
    — failing closed is required because command construction on the
    wrong dialect produces frames the machine ignores (FR-006).
    """
    log = logger_ or logger
    key = map_data.get("machine_profile") if isinstance(map_data, dict) else None
    if key is None:
        log.warning(
            "device map carries no 'machine_profile'; defaulting to "
            "'non_striker' (Soul-style dialect)"
        )
        return NON_STRIKER
    profile = _PROFILES.get(str(key).strip())
    if profile is None:
        raise ValueError(
            f"unknown machine_profile {key!r}; expected one of "
            f"{sorted(_PROFILES)} — refusing to construct commands"
        )
    return profile
