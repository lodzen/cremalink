"""
This module defines the aiohttp application for the local proxy server.
It creates all the API endpoints, manages application state, and handles the
startup and shutdown of background services.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web
from pydantic import ValidationError

from cremalink.local_server_app import protocol
from cremalink.local_server_app.config import ServerSettings, get_settings
from cremalink.local_server_app.device_adapter import DeviceAdapter
from cremalink.local_server_app.jobs import (
    JobManager,
    monitor_job,
    nudger_job,
    rekey_job,
)
from cremalink.local_server_app.logging import create_logger
from cremalink.local_server_app.models import (
    CommandPollResponse,
    CommandRequest,
    ConfigureRequest,
    EncPayload,
    KeyExchangeRequest,
    MonitorResponse,
    PropertiesResponse,
)
from cremalink.local_server_app.state import LocalServerState

#: Typed keys for app-level storage (tests/introspection), mirroring the
#: previous FastAPI app.state.* pattern.
LOCAL_STATE_KEY: web.AppKey[LocalServerState] = web.AppKey("local_state")
SETTINGS_KEY: web.AppKey[ServerSettings] = web.AppKey("settings")
ADAPTER_KEY: web.AppKey[DeviceAdapter] = web.AppKey("adapter")
JOBS_KEY: web.AppKey[JobManager] = web.AppKey("jobs")
STOP_EVENT_KEY: web.AppKey[asyncio.Event] = web.AppKey("stop_event")
LOGGER_KEY: web.AppKey = web.AppKey("logger")


async def _parse_json_model(request: web.Request, model):
    """Parses the request body and validates it against a pydantic model."""
    try:
        body = await request.json()
        return model.model_validate(body)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc


def create_app(
    settings: ServerSettings | None = None,
    device_adapter: DeviceAdapter | None = None,
    logger=None,
) -> web.Application:
    """
    Application factory for the aiohttp server.

    Initializes all components (state, settings, adapter, jobs) and wires up
    the API routes and startup/cleanup handlers.

    Returns:
        A configured aiohttp `Application` instance.
    """
    # Initialize core components, allowing for dependency injection in tests.
    settings = settings or get_settings()
    logger = logger or create_logger("local_server", settings.log_ring_size)
    state = LocalServerState(settings, logger)
    adapter = device_adapter or DeviceAdapter(settings, logger)
    stop_event = asyncio.Event()
    jobs = JobManager()

    print(f"Starting cremalink local server on http://{settings.server_ip}:{settings.server_port}...")
    print(f"IP address advertised to the coffee machine: {settings.advertised_ip}")

    app = web.Application()
    # Exposed for tests/introspection, mirroring the previous FastAPI app.state.* pattern.
    app[LOCAL_STATE_KEY] = state
    app[SETTINGS_KEY] = settings
    app[ADAPTER_KEY] = adapter
    app[JOBS_KEY] = jobs
    app[STOP_EVENT_KEY] = stop_event
    app[LOGGER_KEY] = logger

    # --- Client-Facing API Endpoints (called by cremalink_ha / LocalTransport) ---

    async def configure(request: web.Request) -> web.Response:
        """Configures the server with device connection details."""
        req = await _parse_json_model(request, ConfigureRequest)
        await state.configure(
            dsn=req.dsn,
            device_ip=req.device_ip,
            lan_key=req.lan_key,
            device_scheme=req.device_scheme,
            monitor_property_name=req.monitor_property_name,
            data_request_property_name=req.data_request_property_name,
        )
        # Attempt an initial registration with the device.
        try:
            await adapter.register_with_device(state)
        except Exception as exc:
            state.log("local_reg_initial_failed", {"error": str(exc)})
        return web.json_response({"status": "configured", "dsn": req.dsn, "device_scheme": req.device_scheme})

    async def command(request: web.Request) -> web.Response:
        """Queues a command to be sent to the device."""
        if not state.is_configured():
            raise web.HTTPBadRequest(text="Server not configured")
        req = await _parse_json_model(request, CommandRequest)
        try:
            await adapter.register_with_device(state)
            await state.queue_command(req.command)
        except OverflowError as exc:
            raise web.HTTPTooManyRequests(text=str(exc)) from exc
        except ConnectionError as exc:
            raise web.HTTPBadGateway(text=str(exc)) from exc
        return web.json_response({"status": "queued", "seq": state.seq})

    async def get_monitor(request: web.Request) -> web.Response:
        """Gets the last known monitor status."""
        snapshot = await state.snapshot_monitor()
        return web.json_response(MonitorResponse.model_validate(snapshot).model_dump())

    async def refresh_monitor(request: web.Request) -> web.Response:
        """Queues a request to refresh the monitor status."""
        try:
            await adapter.register_with_device(state)
            await state.queue_monitor()
        except ConnectionError as exc:
            raise web.HTTPBadGateway(text=str(exc)) from exc
        return web.Response(text="queued monitor refresh")

    async def get_properties(request: web.Request) -> web.Response:
        """Gets the last known device properties."""
        try:
            await adapter.register_with_device(state)
            await state.queue_properties()
        except ConnectionError as exc:
            raise web.HTTPBadGateway(text=str(exc)) from exc
        snapshot = await state.snapshot_properties()
        return web.json_response(PropertiesResponse.model_validate(snapshot).model_dump())

    async def get_property(request: web.Request) -> web.Response:
        """Gets a single property value from the last known snapshot."""
        property_name = request.match_info["property_name"]
        value = await state.get_property_value(property_name)
        if value is None:
            raise web.HTTPNotFound(text="Property not found")
        return web.json_response({"name": property_name, "value": value})

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def logs(request: web.Request) -> web.Response:
        ring_handler = next((h for h in logger.handlers if hasattr(h, "get_events")), None)
        events = ring_handler.get_events() if ring_handler else []
        return web.json_response({"events": events, "last_command": state.last_command})

    async def debug_queue(request: web.Request) -> web.Response:
        async with state.lock:  # type: ignore[attr-defined]
            next_payload = state.command_queue[0] if state.command_queue else None
            queued = len(state.command_queue)
            seq = state.seq
        return web.json_response({"queued": queued, "next_payload": next_payload, "seq": seq})

    async def monitor(request: web.Request) -> web.Response:
        async with state.lock:  # type: ignore[attr-defined]
            return web.json_response(state.last_monitor)

    # --- Device-Facing API Endpoints (called by the coffee machine) ---

    async def key_exchange(request: web.Request) -> web.Response:
        """Handles the cryptographic key exchange request from the device."""
        if not state.lan_key:
            raise web.HTTPBadRequest(text="Server not configured")
        req = await _parse_json_model(request, KeyExchangeRequest)
        exchange = req.key_exchange
        await state.init_crypto(random_1=exchange.random_1, time_1=exchange.time_1)
        state.log("key_exchange", {"random_1": exchange.random_1, "time_1": exchange.time_1})
        return web.json_response(
            {"random_2": state.random_2, "time_2": int(state.time_2)}, status=web.HTTPAccepted.status_code
        )

    async def serve_command_poll() -> CommandPollResponse:
        """Shared logic for serving the next command to the device."""
        if not state.keys_ready():
            state.log("command_poll_no_keys", {"queued": len(state.command_queue)})
            return CommandPollResponse(enc="", sign="", seq=state.seq)

        next_item = await state.next_command_payload()
        payload, current_seq = next_item["payload"], next_item["seq"]

        enc, new_iv = protocol.encrypt_payload(payload, state.app_crypto_key, state.app_iv_seed)
        state.app_iv_seed = new_iv
        sign = protocol.sign_payload(payload, state.app_sign_key)
        async with state.lock:
            state.command_payload = protocol.build_empty_payload(state.seq)
        state.log(
            "command_served",
            {"seq": current_seq, "queued_remaining": len(state.command_queue), "payload_size": len(payload)},
        )
        return CommandPollResponse(enc=enc, sign=sign, seq=current_seq)

    async def poll_commands(request: web.Request) -> web.Response:
        """Endpoint for the device to poll for commands (GET/POST)."""
        result = await serve_command_poll()
        return web.json_response(result.model_dump())

    async def datapoint(request: web.Request) -> web.Response:
        """Endpoint for the device to push encrypted data to."""
        if not state.dev_crypto_key or not state.dev_iv_seed:
            raise web.HTTPServiceUnavailable(text="Keys not initialized")

        payload = await _parse_json_model(request, EncPayload)
        decrypted_bytes, new_iv = protocol.decrypt_payload(payload.enc, state.dev_crypto_key, state.dev_iv_seed)
        state.dev_iv_seed = new_iv

        try:
            decoded = decrypted_bytes.decode("utf-8")
            decoded_json = json.loads(decoded)
        except UnicodeDecodeError:
            state.log("datapoint_decode_failed_utf8", {"cipher": payload.enc[:32]})
            async with state.lock:
                state._monitor_request_pending = False
                state._properties_request_pending = False
            return web.Response(status=200)
        except json.JSONDecodeError:
            state.log("datapoint_decode_failed_json", {"decoded_prefix": decrypted_bytes[:64].decode("utf-8", "ignore")})
            async with state.lock:
                state._monitor_request_pending = False
                state._properties_request_pending = False
            return web.Response(status=200)

        await state.handle_datapoint(decoded_json)
        return web.json_response({})

    async def register(request: web.Request) -> web.Response:
        try:
            await adapter.register_with_device(state)
        except Exception as exc:
            state.log("internal_server_error", {"status_code": 500, "error": str(exc)})
            return web.Response(status=500)
        return web.Response(text="registered")

    app.router.add_post("/configure", configure)
    app.router.add_post("/command", command)
    app.router.add_get("/get_monitor", get_monitor)
    app.router.add_get("/refresh_monitor", refresh_monitor)
    app.router.add_get("/get_properties", get_properties)
    app.router.add_get("/properties/{property_name}", get_property)
    app.router.add_get("/health", health)
    app.router.add_get("/logs", logs)
    app.router.add_get("/debug_queue", debug_queue)
    app.router.add_get("/monitor", monitor)
    app.router.add_post("/local_lan/key_exchange.json", key_exchange)
    app.router.add_get("/local_lan/commands.json", poll_commands)
    app.router.add_post("/local_lan/commands.json", poll_commands)
    app.router.add_post("/local_lan/property/datapoint.json", datapoint)
    app.router.add_get("/register", register)

    async def on_startup(app_: web.Application) -> None:
        if settings.enable_nudger_job:
            jobs.start(nudger_job(state, adapter, settings, stop_event), name="nudger")
        if settings.enable_monitor_job:
            jobs.start(monitor_job(state, settings, stop_event), name="monitor")
        if settings.enable_rekey_job:
            jobs.start(rekey_job(state, adapter, settings, stop_event), name="rekey")

    async def on_cleanup(app_: web.Application) -> None:
        stop_event.set()
        await jobs.stop()
        await adapter.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app
