"""In-process hosting for the cremalink local LAN protocol server.

Lets a consumer (e.g. ``cremalink_ha``) run the exact same aiohttp
application used by the standalone ``cremalink-server`` process, in-process
on its own asyncio event loop -- no separate OS process, no Supervisor
add-on required (spec: 002-embedded-local-server, FR-001/FR-002).

Nothing about the local LAN protocol itself changes here: ``create_app()``
already builds a fully asyncio-native aiohttp application (state, protocol,
device adapter, background jobs all reuse existing code unchanged). This
module only changes *how* that application is hosted -- via
``aiohttp.web.AppRunner``/``SockSite`` bound to a pre-bound socket, rather
than a separate OS process.

Migrated from a programmatically driven ``uvicorn.Server`` (spec 002's
original implementation) to ``aiohttp`` (specs/002-embedded-local-server
research.md #1 "Revision"): ``aiohttp`` is already a mandatory Home
Assistant dependency, whereas ``fastapi``/``starlette``/``uvicorn`` were
three extra packages pulled in solely for local mode. Unlike uvicorn's
``Server.serve()``, aiohttp's ``AppRunner``/``SockSite`` do not run as a
single cancellable coroutine task we own -- connections are accepted
directly by the asyncio event loop's own server machinery. There is
therefore no equivalent "serve task exited unexpectedly" signal to
monitor; ``state`` only becomes ``"failed"`` from a `start()`-time error
(e.g. the port-fallback range being exhausted), not from a later runtime
fault in the HTTP layer itself.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import uuid

from aiohttp import web

from cremalink.local_server_app.api import create_app
from cremalink.local_server_app.config import ServerSettings
from cremalink.local_server_app.logging import (
    RingBufferHandler,
    create_logger,
    log_event,
)

#: Default port, matching the standalone server/add-on's historical default.
DEFAULT_PORT = 10280
DEFAULT_REKEY_INTERVAL_SECONDS = 60.0
#: How many ports past the preferred one to try before giving up (FR-007).
DEFAULT_PORT_FALLBACK_RANGE = 50
#: Bound on how long stop() can take waiting for in-flight connections to
#: close (aiohttp's own default is 60s) -- a stuck device connection must
#: never make a config-entry reload/unload/removal hang for that long
#: (research.md #7, tasks.md Phase 9).
SITE_SHUTDOWN_TIMEOUT = 5.0


def _bind_socket(bind_host: str, port: int) -> socket.socket:
    """Bind (but do not listen on) a TCP socket.

    ``aiohttp.web.SockSite`` hands this straight to
    ``loop.create_server(sock=...)``, which performs the ``listen()`` call
    itself, so a socket bound here behaves exactly as a socket
    ``AppRunner``/``TCPSite`` would have bound on their own. Raises
    ``OSError`` if the port is unavailable.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, port))
    sock.set_inheritable(True)
    return sock


def detect_advertised_ip(device_ip: str) -> str:
    """Return the local IP the OS would route through to reach ``device_ip``.

    Uses the "UDP connect" trick: connecting a UDP socket never actually
    sends a packet, it only asks the OS to resolve the outbound route --
    fast and side-effect free (research.md #3, FR-013).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((device_ip, 1))
        return sock.getsockname()[0]
    finally:
        sock.close()


class EmbeddedLocalServer:
    """In-process host for a single device's cremalink local LAN server.

    See ``specs/002-embedded-local-server/contracts/embedded-server-api.md``
    for the full behavioral contract.
    """

    def __init__(
        self,
        dsn: str,
        device_ip: str,
        lan_key: str,
        device_map_path: str | None = None,
        *,
        advertised_ip: str | None = None,
        monitor_poll_interval: float = 5.0,
        nudger_poll_interval: float = 1.0,
        event_logger: logging.Logger | None = None,
        preferred_port: int = DEFAULT_PORT,
        port_fallback_range: int = DEFAULT_PORT_FALLBACK_RANGE,
        bind_host: str = "0.0.0.0",
    ) -> None:
        self.dsn = dsn
        self.device_ip = device_ip
        self.lan_key = lan_key
        self.device_map_path = device_map_path
        self.monitor_poll_interval = monitor_poll_interval
        self.nudger_poll_interval = nudger_poll_interval
        self.rekey_interval_seconds = DEFAULT_REKEY_INTERVAL_SECONDS
        self._event_logger = create_logger(
            f"cremalink.embedded.{uuid.uuid4().hex}",
            200,
            forward_logger=event_logger,
            diagnostic_sensitive_values=(dsn, device_ip, lan_key, bind_host),
            operational_sensitive_values=(lan_key,),
        )
        self._event_handler = next(
            handler
            for handler in self._event_logger.handlers
            if isinstance(handler, RingBufferHandler)
        )
        self.preferred_port = preferred_port
        self.port_fallback_range = port_fallback_range
        self.bind_host = bind_host

        #: Resolved once start() runs; a supplied value is used as-is.
        self.advertised_ip: str | None = advertised_ip
        self.bound_port: int | None = None
        self.state: str = "stopped"

        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._sock: socket.socket | None = None

    async def start(self) -> None:
        """Resolve advertised IP + a free port, then start serving.

        Raises:
            RuntimeError: if already starting/running.
            OSError: if no port in the fallback range could be bound.
        """
        if self.state in ("starting", "running"):
            raise RuntimeError("EmbeddedLocalServer is already starting/running")
        self.state = "starting"

        if self.advertised_ip is None:
            self.advertised_ip = detect_advertised_ip(self.device_ip)
        self._event_handler.diagnostic_sensitive_values += (self.advertised_ip,)

        sock, port = self._bind_with_fallback()
        self._sock = sock
        self.bound_port = port

        runner: web.AppRunner | None = None
        try:
            # Never use the cached process-global settings; each server owns
            # an independent job configuration and lifecycle.
            settings = ServerSettings(
                server_ip=self.bind_host,
                server_port=port,
                advertised_ip=self.advertised_ip,
                monitor_poll_interval=self.monitor_poll_interval,
                nudger_poll_interval=self.nudger_poll_interval,
                rekey_interval_seconds=self.rekey_interval_seconds,
            )
            app = create_app(settings=settings, logger=self._event_logger)
            runner = web.AppRunner(app, shutdown_timeout=SITE_SHUTDOWN_TIMEOUT)
            await runner.setup()
            site = web.SockSite(runner, sock)
            await site.start()
        except BaseException:
            self.state = "failed"
            if runner is not None:
                with contextlib.suppress(Exception):
                    await runner.cleanup()
            with contextlib.suppress(OSError):
                sock.close()
            self._sock = None
            raise

        self._runner = runner
        self._site = site
        self.state = "running"

    def get_recent_events(self) -> list[dict]:
        """Return a snapshot of this server's sanitized event buffer."""
        return self._event_handler.get_events()

    @property
    def event_logger(self) -> logging.Logger:
        """Return this instance's isolated library event logger."""
        return self._event_logger

    def log(
        self,
        event: str,
        details: dict | None = None,
        *,
        level: int = logging.INFO,
    ) -> None:
        """Route an integration-owned event through this server's log path."""
        log_event(
            self._event_logger,
            event,
            details,
            diagnostic_sensitive_values=(self.dsn, self.device_ip, self.lan_key),
            operational_sensitive_values=(self.lan_key,),
            level=level,
        )

    def log_telemetry(self, event: str, details: dict) -> None:
        """Forward telemetry through the HA logger without buffering it."""
        log_event(
            self._event_logger,
            event,
            details,
            diagnostic_sensitive_values=(self.dsn, self.device_ip, self.lan_key),
            operational_sensitive_values=(self.lan_key,),
            telemetry=True,
        )

    def _bind_with_fallback(self) -> tuple[socket.socket, int]:
        """Try `preferred_port`, then increment on conflict (FR-007/FR-008)."""
        last_error: OSError | None = None
        max_port = self.preferred_port + self.port_fallback_range
        for port in range(self.preferred_port, max_port + 1):
            try:
                sock = _bind_socket(self.bind_host, port)
            except OSError as exc:
                last_error = exc
                self.log(
                    "embedded_server_port_unavailable",
                    {
                        "port": port,
                        "error": str(exc),
                        "trying_next_port": port < max_port,
                    },
                    level=logging.WARNING,
                )
                continue
            if port != self.preferred_port:
                self.log(
                    "embedded_server_port_fallback",
                    {"port": port, "preferred_port": self.preferred_port},
                    level=logging.WARNING,
                )
            return sock, port
        self.state = "failed"
        raise OSError(
            "cremalink embedded server: could not bind any port "
            f"in {self.preferred_port}-{max_port}: {last_error}"
        )

    async def stop(self) -> None:
        """Stop serving and release the listening socket (FR-005).

        Safe to call multiple times.
        """
        if self.state == "stopped":
            return
        self.state = "stopping"
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self.state = "stopped"
        self._runner = None
        self._site = None
        self._sock = None
