"""Regression tests for the LAN session state machine (lodzen/cremalink#4).

Measured on the module + verified against the Coffee Link app's
``AylaLanModule.sendLocalRegistration``:

- ``POST local_reg`` always starts a new session + key exchange.
- ``PUT local_reg`` on a live session is a keep-alive only — it never
  re-keys — so discarding our keys before a PUT produces a hollow
  session that only a machine restart heals.
- Machine-initiated key exchanges are accepted in place.
"""

import asyncio
import base64
import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cremalink.local_server_app import ServerSettings, create_app
from cremalink.local_server_app.api import LOCAL_STATE_KEY
from cremalink.local_server_app.device_adapter import DeviceAdapter
from cremalink.local_server_app.jobs import nudger_job, session_job
from cremalink.local_server_app.logging import create_logger
from cremalink.local_server_app.protocol import encrypt_payload
from cremalink.local_server_app.state import LocalServerState


def _state(**kwargs) -> LocalServerState:
    settings = ServerSettings(
        server_settings_path="",
        enable_device_register=False,
        fixed_random_2="a5rLvXXkl7CAH6db",
        fixed_time_2="446005717073803",
        **kwargs,
    )
    return LocalServerState(settings, logging.getLogger("test_session"))


class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None


class _SpyHttpClient:
    """Stands in for aiohttp.ClientSession; records method + url."""

    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return _FakeResp()

    async def close(self):
        return None


class SpyAdapter(DeviceAdapter):
    """Adapter with the device call stubbed; records the chosen method."""

    def __init__(self, settings=None, logger=None, *, fail=False):
        self.settings = settings or ServerSettings(
            server_settings_path="", enable_device_register=True
        )
        self.logger = logger or logging.getLogger("test_session")
        self.calls = []
        self.fail = fail
        self._client = _SpyHttpClient()

    async def close(self):
        return None


async def _configured_state(**kwargs) -> LocalServerState:
    state = _state(**kwargs)
    await state.configure(
        dsn="dsn-1",
        device_ip="1.2.3.4",
        lan_key="lan-key",
        device_scheme="http",
        monitor_property_name="d302_monitor",
        data_request_property_name="data_request",
    )
    return state


async def _live_state(**kwargs) -> LocalServerState:
    """Configured state with a completed key exchange (live session)."""
    state = await _configured_state(**kwargs)
    await state.init_crypto(random_1="r1", time_1="1")
    await state.set_registered(True)
    return state


@pytest.mark.asyncio
async def test_register_posts_for_dead_session_with_dsn_param():
    """No live session -> POST + ?dsn=, like the app's sendLocalRegistration."""
    state = await _configured_state()
    adapter = SpyAdapter()
    await adapter.register_with_device(state)
    method, url = adapter._client.calls[-1]
    assert method == "POST"
    assert url == "http://1.2.3.4/local_reg.json?dsn=dsn-1"


@pytest.mark.asyncio
async def test_register_puts_for_live_session():
    """Live session -> PUT keep-alive, never a re-keying POST."""
    state = await _live_state()
    adapter = SpyAdapter()
    await adapter.register_with_device(state)
    method, url = adapter._client.calls[-1]
    assert method == "PUT"
    assert url == "http://1.2.3.4/local_reg.json"


@pytest.mark.asyncio
async def test_rekey_keeps_keys_store_and_queue():
    """rekey() must not hollow the session: keys/store/mailbox/queue survive.

    The old implementation wiped everything and then PUT-registered, which
    the machine ignores — the hollow-session bug from the issue.
    """
    state = await _live_state()
    keys_before = (
        state.app_crypto_key,
        state.dev_crypto_key,
        state.dev_iv_seed,
    )
    await state.handle_datapoint(
        {"data": {"name": "d284_mchn_sett_user_conf", "value": "AAAA"}}
    )
    await state.queue_command("frame")

    await state.rekey()

    assert (
        state.app_crypto_key,
        state.dev_crypto_key,
        state.dev_iv_seed,
    ) == keys_before
    assert await state.get_property_value("d284_mchn_sett_user_conf") is not None
    assert len(state.command_queue) == 1
    assert state.registered is False
    assert not state.session_live()


@pytest.mark.asyncio
async def test_machine_initiated_key_exchange_replaces_keys_in_place():
    """A fresh exchange overwrites keys without wiping session data."""
    state = await _live_state()
    old_dev_key = state.dev_crypto_key
    await state.handle_datapoint(
        {"data": {"name": "d284_mchn_sett_user_conf", "value": "AAAA"}}
    )

    await state.init_crypto(random_1="r2", time_1="2")

    assert state.dev_crypto_key != old_dev_key
    assert state.keys_ready()
    assert state.registered
    assert state.session_live()
    assert state.last_key_exchange_at is not None
    # the datapoint store survives the re-exchange
    assert await state.get_property_value("d284_mchn_sett_user_conf") is not None


@pytest.mark.asyncio
async def test_session_job_posts_when_dead_then_keeps_alive_with_put():
    """dead -> POST; live + polls -> PUT; long silence -> dead -> POST."""
    state = await _configured_state()
    settings = ServerSettings(
        server_settings_path="",
        session_keepalive_interval_seconds=0.05,
        session_probe_timeout_seconds=0.15,
        session_silence_reset_seconds=0.4,
        session_register_timeout_seconds=0.15,
    )
    state.settings = settings
    adapter = SpyAdapter(settings=settings)
    stop = asyncio.Event()

    # Machine simulator: a real module answers a POST with a key exchange
    # and keeps polling commands.json. Flipping `machine_polling` off
    # simulates a dropped session.
    machine_polling = True

    class MachineAdapter(SpyAdapter):
        async def register_with_device(self, state, method=None):
            await super().register_with_device(state, method)
            if method == "POST":
                await state.init_crypto(random_1="r", time_1="9")

    adapter = MachineAdapter(settings=settings)

    async def machine_polls():
        while not stop.is_set():
            if machine_polling:
                state.mark_commands_poll()
            await asyncio.sleep(0.02)

    job = asyncio.create_task(session_job(state, adapter, settings, stop))
    poller = asyncio.create_task(machine_polls())
    try:
        await asyncio.sleep(0.25)
        methods = [m for m, _ in adapter._client.calls]
        assert "POST" in methods  # session establishment
        assert "PUT" in methods  # keep-alive once live
        assert state.session_live()

        # Machine drops the session: polls stop -> probe fails -> re-POST.
        machine_polling = False
        await asyncio.sleep(0.5)
        methods_after = [m for m, _ in adapter._client.calls]
        assert methods_after.count("POST") > methods.count("POST")
    finally:
        stop.set()
        await asyncio.gather(job, poller)


@pytest.mark.asyncio
async def test_session_job_tolerates_poll_silence_within_reset_window():
    """§14.9: a quiet machine is not dead — the app marks sessions dead on
    local_reg transport errors only. Silence below the reset fuse must keep
    the session live with PUT keepalives and never trigger a re-POST."""
    state = await _configured_state()
    settings = ServerSettings(
        server_settings_path="",
        session_keepalive_interval_seconds=0.05,
        session_probe_timeout_seconds=0.02,
        session_silence_reset_seconds=60.0,
        session_register_timeout_seconds=0.15,
    )
    state.settings = settings

    class MachineAdapter(SpyAdapter):
        async def register_with_device(self, state, method=None):
            await super().register_with_device(state, method)
            if method == "POST":
                await state.init_crypto(random_1="r", time_1="9")
                state.mark_commands_poll()  # session bootstraps, then goes quiet

    adapter = MachineAdapter(settings=settings)
    stop = asyncio.Event()
    job = asyncio.create_task(session_job(state, adapter, settings, stop))
    try:
        await asyncio.sleep(0.4)  # far beyond probe timeout, below the fuse
        methods = [m for m, _ in adapter._client.calls]
        assert methods.count("POST") == 1  # only the initial registration
        assert "PUT" in methods
        assert state.session_live()
    finally:
        stop.set()
        await job


@pytest.mark.asyncio
async def test_session_job_posts_again_on_transport_failure():
    """A keep-alive PUT transport error still marks the session dead and
    re-POSTs — matching the app's handleKeyExchangeError path."""
    state = await _configured_state()
    settings = ServerSettings(
        server_settings_path="",
        session_keepalive_interval_seconds=0.05,
        session_probe_timeout_seconds=0.15,
        session_silence_reset_seconds=60.0,
        session_register_timeout_seconds=0.15,
    )
    state.settings = settings
    put_fails = False

    class FlakyAdapter(SpyAdapter):
        async def register_with_device(self, state, method=None):
            await super().register_with_device(state, method)
            if method == "POST":
                await state.init_crypto(random_1="r", time_1="9")
                state.mark_commands_poll()
            elif put_fails:
                raise ConnectionError("device unreachable")

    adapter = FlakyAdapter(settings=settings)
    stop = asyncio.Event()
    job = asyncio.create_task(session_job(state, adapter, settings, stop))
    try:
        await asyncio.sleep(0.2)
        assert state.session_live()
        baseline = [m for m, _ in adapter._client.calls].count("POST")
        put_fails = True
        await asyncio.sleep(0.3)
        assert [m for m, _ in adapter._client.calls].count("POST") > baseline
    finally:
        stop.set()
        await job


@pytest.mark.asyncio
async def test_named_datapoint_survives_store_and_property_get():
    """Pushed dNNN datapoints must be readable via /properties/{name}.

    Regression for the observed 'properties endpoint 404 while datapoints
    flow': with the old rekey()-wiping jobs the store never accumulated.
    """
    settings = ServerSettings(
        server_settings_path="",
        enable_device_register=False,
        enable_nudger_job=False,
        enable_monitor_job=False,
        enable_session_job=False,
        fixed_random_2="a5rLvXXkl7CAH6db",
        fixed_time_2="446005717073803",
    )
    logger = create_logger("test_session_api", settings.log_ring_size)
    app = create_app(settings=settings, logger=logger)
    async with TestClient(TestServer(app)) as client:
        state = app[LOCAL_STATE_KEY]
        await client.post(
            "/configure",
            json={
                "dsn": "d",
                "device_ip": "1.2.3.4",
                "lan_key": "k",
                "device_scheme": "http",
                "monitor_property_name": "d302_monitor",
                "data_request_property_name": "data_request",
            },
        )
        await client.post(
            "/local_lan/key_exchange.json",
            json={"key_exchange": {"random_1": "r", "time_1": "1"}},
        )

        blob = base64.b64encode(b"\xd0\x0b\x95\x0f\x00\x3f\x00\x00\x00\x1d").decode()
        datapoint = json.dumps(
            {
                "data": {
                    "name": "d284_mchn_sett_user_conf",
                    "value": blob,
                    "base_type": "string",
                }
            },
            separators=(",", ":"),
        )
        enc, _ = encrypt_payload(datapoint, state.dev_crypto_key, state.dev_iv_seed)
        resp = await client.post(
            "/local_lan/property/datapoint.json", json={"enc": enc}
        )
        assert resp.status == 200

        resp = await client.get("/properties/d284_mchn_sett_user_conf")
        assert resp.status == 200
        assert (await resp.json())["value"]["value"] == blob


@pytest.mark.asyncio
async def test_nudger_notifies_live_session_and_throttles_dead_registers():
    """Queue pending + live -> PUT each tick; dead -> POST at register
    cadence, not every tick."""
    state = await _live_state()
    settings = ServerSettings(
        server_settings_path="",
        nudger_poll_interval=0.03,
        session_register_timeout_seconds=0.3,
    )
    state.settings = settings
    adapter = SpyAdapter(settings=settings)
    stop = asyncio.Event()
    job = asyncio.create_task(nudger_job(state, adapter, settings, stop))
    try:
        await state.queue_command("frame-1")
        await asyncio.sleep(0.12)
        methods = [m for m, _ in adapter._client.calls]
        assert methods and set(methods) == {"PUT"}

        # session dies -> registrations throttle to ~1 per register timeout
        await state.set_registered(False)
        state.app_crypto_key = None
        n_before = len(adapter._client.calls)
        await asyncio.sleep(0.12)
        posts = [m for m, _ in adapter._client.calls[n_before:] if m == "POST"]
        assert len(posts) <= 1
    finally:
        stop.set()
        await job


@pytest.mark.asyncio
async def test_commands_poll_marks_liveness():
    """commands.json polls advance last_commands_poll_at for the probe."""
    state = await _live_state()
    assert state.last_commands_poll_at is None
    assert await state.wait_for_commands_poll(0.05) is False
    state.mark_commands_poll()
    assert await state.wait_for_commands_poll(0.05) is True
    # a poll strictly *after* `since` is required for the keep-alive probe
    since = state.last_commands_poll_at
    assert await state.wait_for_commands_poll(0.05, since=since) is False
    state.mark_commands_poll()
    assert await state.wait_for_commands_poll(0.05, since=since) is True
