"""
The ECAM write-gate session.

Display-verified on Soul LAN: ``0x90``/``0xA9`` writes only commit when a
session has been announced by writing the machine's ``device_connected``
property to the current unix timestamp (same ``properties`` payload
mechanism as ``data_request``). The session lives ~300 s; writes past
~240 s are re-announced. Without the announce, writes stage transiently
(``0xF0``) or are ignored (``0x0F``).

On striker machines the session instead uses the ``0xE8`` handshake plus
the ``84 0f 03 02`` app-id registration, after which the registered
4-byte app id rides as the transport trailer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from cremalink.ecam.machine_profiles import (
    SESSION_DEVICE_CONNECTED_TS,
    MachineProfile,
)

#: Refresh well inside the ~300 s observed TTL (the app uses 240 s).
DEFAULT_REFRESH_SECONDS = 240


class SessionGate:
    """Tracks session freshness and performs announces on demand.

    Constructed per device/transport — per-entry state only. ``announce``
    is the profile-appropriate way to (re)open the session:

    - non-striker: ``write_property(connected_property, unix_ts)``
    - striker: the ``0xE8`` handshake + ``84 0f 03 02`` registration
      callback (run once), returning the registered app-id bytes.
    """

    def __init__(
        self,
        profile: MachineProfile,
        announce: Callable[[], Any],
        *,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a gate.

        Args:
            profile: bound machine profile; decides the session shape.
            announce: zero-arg callable performing one announce/refresh.
                For ``DEVICE_CONNECTED_TS`` profiles this is expected to
                write ``device_connected = int(time.time())``; for
                ``E8_APPID`` profiles the striker handshake+registration.
            refresh_seconds: announce again once the session is this old.
            clock: injectable clock for tests.
        """
        self.profile = profile
        self.refresh_seconds = refresh_seconds
        self._announce = announce
        self._clock = clock
        #: unix time of the last successful announce (0 = never).
        self.announced_at = 0.0

    @property
    def uses_timestamp_announce(self) -> bool:
        """Non-striker machines announce via the ``device_connected`` ts."""
        return self.profile.session == SESSION_DEVICE_CONNECTED_TS

    def is_fresh(self, now: float | None = None) -> bool:
        """Whether the current session is still inside the refresh window."""
        if not self.announced_at:
            return False
        now = self._clock() if now is None else now
        return now - self.announced_at < self.refresh_seconds

    def ensure_fresh(self, now: float | None = None) -> bool:
        """Announce the session when stale. Returns True iff an announce ran."""
        now = self._clock() if now is None else now
        if self.is_fresh(now):
            return False
        self._announce()
        self.announced_at = now
        return True

    def invalidate(self) -> None:
        """Force the next gated write to re-announce (e.g. after an ack
        indicated the write was ignored)."""
        self.announced_at = 0.0
