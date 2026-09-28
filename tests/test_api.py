import base64
import json
import logging
import uuid

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from cremalink.domain.device import Device
from cremalink.local_server_app import ServerSettings, create_app
from cremalink.local_server_app.api import LOCAL_STATE_KEY
from cremalink.local_server_app.device_adapter import DeviceAdapter
from cremalink.local_server_app.logging import create_logger
from cremalink.local_server_app.protocol import encrypt_payload
from cremalink.local_server_app.state import LocalServerState


class FakeAdapter(DeviceAdapter):
    async def register_with_device(self, state):
        await state.set_registered(True)

    async def close(self):
        return


@pytest_asyncio.fixture
async def app_client():
    settings = ServerSettings(
        server_ip="127.0.0.1",
        server_port=10800,
        enable_device_register=False,
        enable_nudger_job=False,
        enable_monitor_job=False,
        enable_rekey_job=False,
        fixed_random_2="a5rLvXXkl7CAH6db",
        fixed_time_2="446005717073803",
    )
    logger = create_logger("test_local_server", settings.log_ring_size)
    adapter = FakeAdapter(settings=settings, logger=logger)
    app = create_app(settings=settings, device_adapter=adapter, logger=logger)
    async with TestClient(TestServer(app)) as client:
        state = app[LOCAL_STATE_KEY]
        yield client, state


@pytest.mark.asyncio
async def test_full_flow(app_client):
    client, state = app_client
    configure_body = {
        "dsn": "dsn-1",
        "device_ip": "1.2.3.4",
        "lan_key": "lan-key",
        "device_scheme": "https",
    }
    resp = await client.post("/configure", json=configure_body)
    assert resp.status == 200

    key_exchange_body = {"key_exchange": {"random_1": "random-1", "time_1": "123456"}}
    resp = await client.post("/local_lan/key_exchange.json", json=key_exchange_body)
    assert resp.status == 202
    assert (await resp.json())["random_2"] == "a5rLvXXkl7CAH6db"

    resp = await client.post("/command", json={"command": "brew"})
    assert resp.status == 200

    resp = await client.get("/local_lan/commands.json")
    assert resp.status == 200
    poll_payload = await resp.json()
    assert poll_payload["seq"] == 0
    assert poll_payload["enc"]
    assert poll_payload["sign"]

    dev_key = state.dev_crypto_key
    dev_iv = state.dev_iv_seed
    assert dev_key and dev_iv

    monitor_value = base64.b64encode(b"monitor-bytes").decode("utf-8")
    monitor_datapoint = json.dumps(
        {"data": {"value": monitor_value}}, separators=(",", ":")
    )
    enc_monitor, _ = encrypt_payload(monitor_datapoint, dev_key, dev_iv)
    resp = await client.post(
        "/local_lan/property/datapoint.json", json={"enc": enc_monitor}
    )
    assert resp.status == 200

    resp = await client.get("/get_monitor")
    assert resp.status == 200
    monitor_payload = await resp.json()
    assert monitor_payload["monitor_b64"] == monitor_value
    assert monitor_payload["received_at"] is not None

    dev_iv_rotated = state.dev_iv_seed
    properties_payload = json.dumps(
        {
            "data": {
                "properties": {"prop1": {"property": {"name": "prop1", "value": "v"}}}
            }
        },
        separators=(",", ":"),
    )
    enc_props, _ = encrypt_payload(properties_payload, dev_key, dev_iv_rotated)
    resp = await client.post(
        "/local_lan/property/datapoint.json", json={"enc": enc_props}
    )
    assert resp.status == 200

    resp = await client.get("/get_properties")
    assert resp.status == 200
    assert (await resp.json())["properties"]["prop1"]["property"]["value"] == "v"

    resp = await client.get("/properties/prop1")
    assert resp.status == 200
    assert (await resp.json())["value"]["property"]["value"] == "v"

    resp = await client.get("/health")
    assert await resp.text() == "ok"


@pytest.mark.asyncio
async def test_server_events_are_redacted_and_forwarded(caplog):
    settings = ServerSettings(
        server_settings_path="",
        enable_device_register=False,
        enable_nudger_job=False,
        enable_monitor_job=False,
        enable_rekey_job=False,
    )
    ha_logger = logging.getLogger("test_cremalink_forwarded")
    caplog.set_level(logging.INFO, logger=ha_logger.name)
    logger = create_logger(
        f"test_local_server_{uuid.uuid4().hex}",
        settings.log_ring_size,
        forward_logger=ha_logger,
    )
    state = LocalServerState(settings, logger)

    await state.configure(
        dsn="secret-dsn",
        device_ip="192.0.2.20",
        lan_key="secret-lan-key",
    )

    handler = next(
        handler for handler in logger.handlers if hasattr(handler, "get_events")
    )
    configured_event = next(
        event for event in handler.get_events() if event["event"] == "configured"
    )
    assert configured_event["details"] == {
        "dsn": "***",
        "device_ip": "***",
        "scheme": "https",
    }
    forwarded_message = next(
        record.getMessage()
        for record in caplog.records
        if "configured details=" in record.getMessage()
    )
    assert "secret-dsn" in forwarded_message
    assert "192.0.2.20" in forwarded_message
    assert "secret-lan-key" not in forwarded_message

    state.log(
        "command_requested",
        {
            "dsn": "secret-dsn",
            "device_ip": "192.0.2.20",
            "command": "brew-coffee",
            "lan_key": "secret-lan-key",
            "session_key": "secret-session-key",
            "authorization": "Bearer secret-token",
        },
    )
    command_event = next(
        event for event in handler.get_events() if event["event"] == "command_requested"
    )
    assert command_event["details"] == {
        "dsn": "***",
        "device_ip": "***",
        "command": "***",
        "lan_key": "***",
        "session_key": "***",
        "authorization": "***",
    }
    command_message = next(
        record.getMessage()
        for record in caplog.records
        if "command_requested details=" in record.getMessage()
    )
    assert "secret-dsn" in command_message
    assert "192.0.2.20" in command_message
    assert "brew-coffee" in command_message
    assert "secret-lan-key" not in command_message
    assert "secret-session-key" not in command_message
    assert "secret-token" not in command_message

    state.log_telemetry(
        "device_datapoint_received",
        {
            "data": {"value": "clear-monitor-data"},
            "device_ip": "192.0.2.20",
            "lan_key": "secret-lan-key",
        },
    )
    telemetry_message = next(
        record.getMessage()
        for record in caplog.records
        if "device_datapoint_received details=" in record.getMessage()
    )
    assert "clear-monitor-data" in telemetry_message
    assert "192.0.2.20" in telemetry_message
    assert '"lan_key": "***"' in telemetry_message
    assert "secret-lan-key" not in telemetry_message
    assert not any(
        event["event"] == "device_datapoint_received" for event in handler.get_events()
    )


def test_device_map_warning_uses_injected_logger(tmp_path, caplog):
    device_map_path = tmp_path / "unsupported-local.json"
    device_map_path.write_text(json.dumps({"support": {"local": False}}))
    event_logger = logging.getLogger("test_cremalink_device_map")
    caplog.set_level(logging.WARNING, logger=event_logger.name)

    class LocalTransport:
        pass

    Device.from_map(
        transport=LocalTransport(),
        device_map_path=str(device_map_path),
        event_logger=event_logger,
    )

    assert (
        sum(
            record.name == event_logger.name
            and "device_map_transport_unsupported" in record.getMessage()
            for record in caplog.records
        )
        == 1
    )


def test_standalone_startup_logs_without_prints(capsys):
    settings = ServerSettings(
        server_ip="127.0.0.1",
        server_port=10280,
        advertised_ip="192.0.2.30",
        enable_nudger_job=False,
        enable_monitor_job=False,
        enable_rekey_job=False,
    )
    logger = create_logger(
        f"test_standalone_{uuid.uuid4().hex}",
        settings.log_ring_size,
        console=True,
    )

    create_app(settings=settings, logger=logger)

    output = capsys.readouterr().err
    assert "Starting cremalink local server" in output
    assert "192.0.2.30" in output
