"""Tests for the embedded local server hosting API.

Covers spec 002-embedded-local-server: FR-001/002 (protocol parity),
FR-005 (lifecycle), FR-006 (isolation), FR-007/008 (port fallback),
FR-012 (crash surfaces as failed state), FR-013 (advertised-IP detection).
"""

import asyncio
import logging
import socket
import uuid

import aiohttp
import pytest
from cremalink.local_server_app import embedded as embedded_mod
from cremalink.local_server_app.config import ServerSettings
from cremalink.local_server_app.embedded import (
    EmbeddedLocalServer,
    detect_advertised_ip,
)
from pydantic import ValidationError


def _free_port() -> int:
    """Ask the OS for a currently-unused TCP port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def test_server_settings_reject_non_positive_job_intervals():
    with pytest.raises(ValidationError):
        ServerSettings(nudger_poll_interval=0)


@pytest.mark.asyncio
async def test_start_stop_lifecycle_and_protocol_roundtrip():
    """start() serves the real local LAN protocol over a real TCP socket."""
    port = _free_port()
    server = EmbeddedLocalServer(
        dsn="dsn1",
        device_ip="127.0.0.1",
        lan_key="testlankey1234567890",
        preferred_port=port,
        bind_host="127.0.0.1",
    )
    await server.start()
    try:
        assert server.state == "running"
        assert server.bound_port == port
        assert server.advertised_ip  # auto-detected, non-empty

        async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.get("/health")
            assert resp.status == 200

            # Mirrors what `LocalTransport.configure()` does automatically
            # via `create_local_device(..., auto_configure=True)`.
            resp = await client.post(
                "/configure",
                json={
                    "dsn": "dsn1",
                    "device_ip": "127.0.0.1",
                    "lan_key": "testlankey1234567890",
                },
            )
            assert resp.status == 200

            resp = await client.post(
                "/local_lan/key_exchange.json",
                json={"key_exchange": {"random_1": "r1", "time_1": 123}},
            )
            assert resp.status == 202
            body = await resp.json()
            assert "random_2" in body and "time_2" in body
    finally:
        await server.stop()

    assert server.state == "stopped"


@pytest.mark.asyncio
async def test_stop_releases_the_port_immediately():
    """A stopped server's port can be immediately rebound (SC-002)."""
    port = _free_port()
    server = EmbeddedLocalServer(
        dsn="dsn2", device_ip="127.0.0.1", lan_key="key", preferred_port=port
    )
    await server.start()
    await server.stop()

    # If the port were still held, this bind would raise OSError.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", port))
    probe.close()


@pytest.mark.asyncio
async def test_monitor_interval_is_passed_to_server_settings(monkeypatch):
    port = _free_port()
    created_settings = []
    create_app = embedded_mod.create_app

    def _capture_settings(*, settings, logger=None):
        created_settings.append(settings)
        return create_app(settings=settings, logger=logger)

    monkeypatch.setattr(embedded_mod, "create_app", _capture_settings)
    server = EmbeddedLocalServer(
        dsn="dsn-interval",
        device_ip="127.0.0.1",
        lan_key="key",
        preferred_port=port,
        bind_host="127.0.0.1",
        monitor_poll_interval=12.0,
        nudger_poll_interval=0.5,
    )
    await server.start()
    try:
        assert created_settings[0].monitor_poll_interval == 12.0
        assert created_settings[0].nudger_poll_interval == 0.5
        assert created_settings[0].rekey_interval_seconds == 60.0
        assert server.rekey_interval_seconds == 60.0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_invalid_interval_releases_bound_socket():
    port = _free_port()
    server = EmbeddedLocalServer(
        dsn="dsn-invalid-interval",
        device_ip="127.0.0.1",
        lan_key="key",
        preferred_port=port,
        bind_host="127.0.0.1",
        nudger_poll_interval=0,
    )

    with pytest.raises(ValidationError):
        await server.start()

    assert server.state == "failed"
    assert server._sock is None
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", port))
    probe.close()


@pytest.mark.asyncio
async def test_two_instances_are_fully_independent():
    """Two simultaneous instances get distinct ports and independent state (FR-006)."""
    port1, port2 = _free_port(), _free_port()
    server1 = EmbeddedLocalServer(
        dsn="dsnA", device_ip="127.0.0.1", lan_key="keyA", preferred_port=port1
    )
    server2 = EmbeddedLocalServer(
        dsn="dsnB", device_ip="127.0.0.1", lan_key="keyB", preferred_port=port2
    )
    await server1.start()
    await server2.start()
    try:
        assert server1.bound_port != server2.bound_port
        assert server1.state == server2.state == "running"
        server1._event_logger.info("server one event")
        assert any(
            event["event"] == "server one event"
            for event in server1.get_recent_events()
        )
        assert not any(
            event["event"] == "server one event"
            for event in server2.get_recent_events()
        )
    finally:
        await server1.stop()
        await server2.stop()


@pytest.mark.asyncio
async def test_port_conflict_falls_back_to_next_free_port():
    """Occupying the default port makes start() bind the next one (FR-007/008)."""
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    try:
        server = EmbeddedLocalServer(
            dsn="dsn3",
            device_ip="127.0.0.1",
            lan_key="key",
            preferred_port=port,
            port_fallback_range=5,
        )
        await server.start()
        try:
            assert server.bound_port != port
            assert server.bound_port <= port + 5
        finally:
            await server.stop()
    finally:
        blocker.close()


@pytest.mark.asyncio
async def test_port_fallback_warning_uses_per_server_logger(caplog):
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    ha_logger = logging.getLogger(f"test_embedded_forward_{uuid.uuid4().hex}")
    caplog.set_level(logging.WARNING, logger=ha_logger.name)
    server = EmbeddedLocalServer(
        dsn="dsn-warning",
        device_ip="127.0.0.1",
        lan_key="secret-key",
        preferred_port=port,
        port_fallback_range=5,
        bind_host="127.0.0.1",
        event_logger=ha_logger,
    )
    try:
        await server.start()
        forwarded = [
            record
            for record in caplog.records
            if record.name == ha_logger.name
            and "embedded_server_port_fallback" in record.getMessage()
        ]
        buffered = [
            event
            for event in server.get_recent_events()
            if event["event"] == "embedded_server_port_fallback"
        ]
        assert len(forwarded) == 1
        assert len(buffered) == 1
    finally:
        await server.stop()
        blocker.close()


def test_embedded_logger_redacts_plain_message_from_buffer(caplog):
    ha_logger = logging.getLogger(f"test_embedded_message_{uuid.uuid4().hex}")
    caplog.set_level(logging.INFO, logger=ha_logger.name)
    server = EmbeddedLocalServer(
        dsn="secret-dsn",
        device_ip="192.0.2.40",
        lan_key="secret-lan-key",
        advertised_ip="192.0.2.41",
        event_logger=ha_logger,
    )

    server.event_logger.info(
        "device record dsn=secret-dsn device_ip=192.0.2.40 lan_key=secret-lan-key"
    )

    event = server.get_recent_events()[-1]
    assert "secret-dsn" not in event["event"]
    assert "192.0.2.40" not in event["event"]
    assert "secret-lan-key" not in event["event"]
    forwarded = next(
        record.getMessage()
        for record in caplog.records
        if record.name == ha_logger.name and "device record" in record.getMessage()
    )
    assert "secret-dsn" in forwarded
    assert "192.0.2.40" in forwarded
    assert "secret-lan-key" not in forwarded


@pytest.mark.asyncio
async def test_port_fallback_range_exhausted_raises_clear_error():
    """Exhausting the fallback range raises OSError, not a hang or crash."""
    port = _free_port()
    blockers = []
    try:
        for offset in range(3):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", port + offset))
            sock.listen()
            blockers.append(sock)

        server = EmbeddedLocalServer(
            dsn="dsn4",
            device_ip="127.0.0.1",
            lan_key="key",
            preferred_port=port,
            port_fallback_range=2,  # only covers the 3 blocked ports
        )
        with pytest.raises(OSError):
            await server.start()
        assert server.state == "failed"
    finally:
        for sock in blockers:
            sock.close()


def test_detect_advertised_ip_returns_a_routable_local_address():
    ip = detect_advertised_ip("127.0.0.1")
    assert ip
    assert isinstance(ip, str)


@pytest.mark.asyncio
async def test_start_twice_without_stop_raises_runtime_error():
    port = _free_port()
    server = EmbeddedLocalServer(
        dsn="dsn5", device_ip="127.0.0.1", lan_key="key", preferred_port=port
    )
    await server.start()
    try:
        with pytest.raises(RuntimeError):
            await server.start()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stop_is_bounded_even_with_a_stuck_connection(monkeypatch):
    """A stuck device connection must not block stop() for long (research.md #7)."""
    monkeypatch.setattr(embedded_mod, "SITE_SHUTDOWN_TIMEOUT", 0.2)
    port = _free_port()
    server = EmbeddedLocalServer(
        dsn="dsn6",
        device_ip="127.0.0.1",
        lan_key="key",
        preferred_port=port,
        bind_host="127.0.0.1",
    )
    await server.start()

    # Open a connection and never close it, simulating a stuck device.
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await server.stop()
        elapsed = loop.time() - started
        # Bounded by the (monkeypatched) SITE_SHUTDOWN_TIMEOUT, nowhere near
        # aiohttp's real 60s default.
        assert elapsed < 2.0
        assert server.state == "stopped"
    finally:
        writer.close()
