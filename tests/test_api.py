import base64
import json

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from cremalink.local_server_app import ServerSettings, create_app
from cremalink.local_server_app.api import LOCAL_STATE_KEY
from cremalink.local_server_app.device_adapter import DeviceAdapter
from cremalink.local_server_app.logging import create_logger
from cremalink.local_server_app.protocol import encrypt_payload


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
    configure_body = {"dsn": "dsn-1", "device_ip": "1.2.3.4", "lan_key": "lan-key", "device_scheme": "https"}
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
    monitor_datapoint = json.dumps({"data": {"value": monitor_value}}, separators=(",", ":"))
    enc_monitor, _ = encrypt_payload(monitor_datapoint, dev_key, dev_iv)
    resp = await client.post("/local_lan/property/datapoint.json", json={"enc": enc_monitor})
    assert resp.status == 200

    resp = await client.get("/get_monitor")
    assert resp.status == 200
    monitor_payload = await resp.json()
    assert monitor_payload["monitor_b64"] == monitor_value
    assert monitor_payload["received_at"] is not None

    dev_iv_rotated = state.dev_iv_seed
    properties_payload = json.dumps(
        {"data": {"properties": {"prop1": {"property": {"name": "prop1", "value": "v"}}}}}, separators=(",", ":")
    )
    enc_props, _ = encrypt_payload(properties_payload, dev_key, dev_iv_rotated)
    resp = await client.post("/local_lan/property/datapoint.json", json={"enc": enc_props})
    assert resp.status == 200

    resp = await client.get("/get_properties")
    assert resp.status == 200
    assert (await resp.json())["properties"]["prop1"]["property"]["value"] == "v"

    resp = await client.get("/properties/prop1")
    assert resp.status == 200
    assert (await resp.json())["value"]["property"]["value"] == "v"

    resp = await client.get("/health")
    assert await resp.text() == "ok"
