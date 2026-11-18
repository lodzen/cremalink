"""
This module provides an adapter for communicating directly with the coffee
machine device on the local network. Its main purpose is to handle the
device registration process.
"""

import asyncio
import logging
import ssl
import time

import aiohttp

from cremalink.local_server_app.config import ServerSettings
from cremalink.local_server_app.state import LocalServerState


class DeviceAdapter:
    """
    Handles direct HTTP communication with the physical coffee machine.

    This class is responsible for the 'registration' step, where this local
    proxy server informs the coffee machine of its presence, telling it where
    to send data pushes.
    """

    def __init__(self, settings: ServerSettings, logger):
        """
        Initializes the DeviceAdapter.

        Args:
            settings: The application's configuration settings.
            logger: The application's logger instance.
        """
        self.settings = settings
        self.logger = logger
        self._client: aiohttp.ClientSession | None = None
        #: Serializes registration attempts — the nudger, session job and
        #: API endpoints can all decide to register at once (observed
        #: 2026-11-12: 3 POSTs in <1 s after a session reset, the same
        #: mini-storm shape that once wedged the datapoint engine). The
        #: method is resolved *inside* the lock so a loser of the race
        #: sees the session the winner just opened.
        self._register_lock = asyncio.Lock()
        #: Last session-opening POST — the storm guard; PUT keep-alives
        #: must not count or a dead session could never re-POST while
        #: keep-alives flow.
        self._last_post_attempt_at: float | None = None
        #: Consecutive local_reg failures — the app's MAX_CONNECTION_ATTEMPTS
        #: trip point (AylaLanModule$9: >2 failures marks the session
        #: unrecoverable client-side). Ours triggers the cloud wake probe.
        self._reg_failures = 0
        self._last_cloud_wake_at: float | None = None
        self._cloud_client = None

    def _build_ssl_context(self) -> ssl.SSLContext | bool:
        """
        Translates the CA-path/verify settings into an aiohttp `ssl=` value.

        A configured CA path always wins (custom trust root); otherwise
        `device_register_verify` toggles default certificate verification.
        `True` keeps aiohttp's default verified context (ssl=None is not a
        valid aiohttp value anymore).
        """
        ca_path = self.settings.device_register_ca_path
        if ca_path:
            return ssl.create_default_context(cafile=ca_path)
        return self.settings.device_register_verify

    async def _get_client(self) -> aiohttp.ClientSession:
        """
        Provides a singleton instance of an `aiohttp.ClientSession`.

        The session is configured with timeout and SSL verification settings
        from the application configuration.
        """
        if self._client is None:
            self._client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=self.settings.device_register_timeout
                ),
            )
        return self._client

    async def register_with_device(
        self, state: LocalServerState, method: str | None = None
    ) -> None:
        """Sends a ``local_reg`` request to the coffee machine.

        This tells the device to send its data updates (like monitor status)
        to this server's ``/local_lan`` endpoint. The HTTP method is
        load-bearing (lodzen/cremalink#4, verified against the Coffee Link
        app's ``AylaLanModule.sendLocalRegistration``): a live session gets
        a keep-alive ``PUT`` (never re-keys), while starting a session needs
        ``POST``, which always triggers a key exchange. When ``method`` is
        omitted it is chosen from :meth:`LocalServerState.session_live`.

        Args:
            state: The current server state, containing device IP and scheme.
            method: ``"PUT"`` or ``"POST"``; ``None`` picks automatically.

        Raises:
            ValueError: If the device IP is not configured in the state.
            ConnectionError: If the HTTP request to the device fails.
        """
        if not self.settings.enable_device_register:
            state.log("register_skipped", {"reason": "disabled"})
            return

        if not state.device_ip:
            raise ValueError("Device IP not configured")

        if not self._register_lock.locked():
            state.log("register_lock_wait", {"method": method}, level=logging.DEBUG)
        async with self._register_lock:
            if method is None:
                # Resolved inside the lock — a raced caller whose turn came
                # after a successful POST sees the live session and PUTs a
                # keep-alive instead of forcing a second key exchange.
                method = "PUT" if state.session_live() else "POST"
            if method == "POST":
                if self._last_post_attempt_at is not None:
                    in_flight = time.monotonic() - self._last_post_attempt_at
                    if in_flight < self.settings.session_register_timeout_seconds:
                        # A session-opening POST is still in flight or
                        # awaiting its key exchange — a second one would
                        # restart rekey mid-exchange (the mini-storm shape
                        # that once wedged the datapoint engine). Defer to
                        # the in-flight attempt.
                        state.log(
                            "register_deferred",
                            {"in_flight_seconds": round(in_flight, 1)},
                            level=logging.DEBUG,
                        )
                        return
                self._last_post_attempt_at = time.monotonic()
            api_url = f"{state.device_scheme}://{state.device_ip}/local_reg.json"
            if method == "POST" and state.dsn:
                # The app appends the DSN when opening a session.
                api_url += f"?dsn={state.dsn}"
            # The payload tells the device this server's IP, port, and notification endpoint.
            # `notify` mirrors the app: 1 only while commands wait in the queue
            # (`_pendingLanCommands.isEmpty() ? 0 : 1`), not a constant flag.
            payload = {
                "local_reg": {
                    "ip": self.settings.advertised_ip or self.settings.server_ip,
                    "notify": 1 if state.command_queue else 0,
                    "port": self.settings.server_port,
                    "uri": "/local_lan",
                }
            }
            client = await self._get_client()
            state.last_register_attempt_at = time.monotonic()
            try:
                resp = await client.request(
                    method, api_url, json=payload, ssl=self._build_ssl_context()
                )
                async with resp:
                    resp.raise_for_status()
            except (aiohttp.ClientError, TimeoutError) as exc:
                # TimeoutError is NOT an aiohttp.ClientError — a machine that
                # hangs the request mid-standby surfaced as an unhandled
                # exception with an empty str() (observed 2026-11-12).
                await state.set_registered(False)
                state.log(
                    "local_reg_failed",
                    {"method": method, "error": f"{type(exc).__name__}: {exc}"},
                )
                await self._maybe_cloud_wake(state)
                raise ConnectionError(f"local_reg failed: {exc}") from exc
            else:
                self._reg_failures = 0
                await state.set_registered(True)
                state.log(
                    "local_reg_ok",
                    {
                        "device_ip": state.device_ip,
                        "scheme": state.device_scheme,
                        "method": method,
                    },
                    level=logging.DEBUG,
                )

    async def _maybe_cloud_wake(self, state: LocalServerState) -> None:
        """Fire a cloud ``device_connected`` announce after repeated failures.

        When ``local_reg`` keeps failing — a 404 means the machine's LAN
        module is down (module-off state; observed 2026-11-12 persisting
        ~3.5 min until a power cycle) — the only remaining channel is the
        machine's cloud uplink, which the app uses for the same announce
        (``AylaProperty.createDatapointCloud``; research.md R17). A
        device-echoed datapoint afterwards proves the engine is alive.

        Trigger mirrors ``AylaLanModule$9``: every local_reg error counts
        toward the threshold regardless of caller (nudger/session job/API);
        the announce itself is throttled to ``cloud_wake_interval_seconds``.
        Harmless when no cloud token is configured — the write is skipped
        and only LAN retries continue.
        """
        self._reg_failures += 1
        if (
            self._reg_failures < self.settings.cloud_wake_threshold
            or not self.settings.cloud_token_path
            or not state.dsn
        ):
            return
        now = time.monotonic()
        if (
            self._last_cloud_wake_at is not None
            and now - self._last_cloud_wake_at
            < self.settings.cloud_wake_interval_seconds
        ):
            return
        self._last_cloud_wake_at = now
        try:
            await asyncio.to_thread(self._cloud_announce_sync, state.dsn)
        except Exception as exc:
            state.log(
                "cloud_announce_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
        else:
            state.log(
                "cloud_announce_sent",
                {"consecutive_failures": self._reg_failures},
            )

    def _cloud_announce_sync(self, dsn: str) -> None:
        """Synchronous cloud announce — runs via ``asyncio.to_thread``.

        The ``Client`` constructor refreshes the token and lists devices
        (two HTTPS calls); it is cached so only the first wake pays that.
        Imported lazily: ``clients.cloud`` is not needed by LAN-only
        deployments.
        """
        from cremalink.clients.cloud import Client

        if self._cloud_client is None:
            token_path = self.settings.cloud_token_path
            if token_path is None:
                return
            self._cloud_client = Client(token_path, logger=self.logger)
        self._cloud_client.create_datapoint(
            dsn, "device_connected", str(int(time.time()))
        )

    async def close(self) -> None:
        """Closes the underlying aiohttp session if it exists."""
        if self._client:
            await self._client.close()
            self._client = None
