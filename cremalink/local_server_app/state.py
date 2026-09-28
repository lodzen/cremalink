"""
This module defines the state management for the local server application.
It centralizes all runtime data, including device configuration, cryptographic
keys, command queues, and the latest received device data.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from cremalink.local_server_app import protocol
from cremalink.local_server_app.logging import redact

if TYPE_CHECKING:
    from cremalink.local_server_app.config import ServerSettings


class LocalServerState:
    """
    Manages the runtime state of the local server in a thread-safe manner.

    This class acts as a state machine, holding all information related to the
    device connection, including credentials, cryptographic session keys,
    pending commands, and the most recent data snapshots (monitor and properties).
    An asyncio.Lock is used to prevent race conditions when accessing state
    from different asynchronous tasks.
    """

    def __init__(self, settings: ServerSettings, logger):
        """
        Initializes the state with default values.

        Args:
            settings: The application's configuration settings.
            logger: The application's logger instance.
        """
        self.settings = settings
        self.logger = logger
        # --- Device Configuration ---
        self.dsn: str | None = None
        self.device_ip: str | None = None
        self.device_scheme: str = "https"
        self.lan_key: str | None = None
        # --- Session & Command State ---
        self.seq: int = 0
        self.command_queue: deque[str] = deque()
        self.command_payload: str = protocol.build_empty_payload(self.seq)
        self.last_command: str | None = None
        self.registered: bool = False

        # --- Cryptographic Keys & IVs ---
        self.app_sign_key: bytes | None = None
        self.app_crypto_key: bytes | None = None
        self.app_iv_seed: bytes | None = None
        self.dev_crypto_key: bytes | None = None
        self.dev_iv_seed: bytes | None = None

        # --- Key Exchange Parameters ---
        self.random_2: str = self._generate_random_2()
        self.time_2: str = self._generate_time_2()

        # --- Data Snapshots ---
        self.last_monitor: dict[str, Any] | dict = {}
        self.last_monitor_raw: dict[str, Any] = {}
        self.last_monitor_b64: str | None = None
        self.last_monitor_received_at: float | None = None
        self.last_monitor_command_sent_at: float | None = None
        self._monitor_command_payloads: set[str] = set()
        self.last_properties: dict[str, Any] = {}
        self.last_properties_received_at: float | None = None
        self._monitor_request_pending = False
        self._properties_request_pending = False
        self.monitor_property_name: str = None
        self.data_request_property_name: str = None

        # --- Concurrency Control ---
        self.lock = asyncio.Lock()

        self._load_server_settings()

    # --- helpers ---
    def _generate_random_2(self) -> str:
        """Generates the server-side random value for key exchange."""
        if self.settings.fixed_random_2:
            return self.settings.fixed_random_2
        return base64.b64encode(os.urandom(12)).decode("utf-8")

    def _generate_time_2(self) -> str:
        """Generates the server-side timestamp for key exchange."""
        if self.settings.fixed_time_2:
            return self.settings.fixed_time_2
        return str(int(time.time() * 1000))

    # --- lifecycle ---
    async def configure(
        self,
        dsn: str,
        device_ip: str,
        lan_key: str,
        device_scheme: str = "https",
        monitor_property_name: str | None = None,
        data_request_property_name: str | None = None,
    ) -> None:
        """
        Configures the state with new device details and resets the session.
        If the device details are unchanged, this is a no-op.
        """
        async with self.lock:
            # Check if configuration is for the same device and keys are already set up.
            same_device = (
                self.dsn == dsn
                and self.device_ip == device_ip
                and self.lan_key == lan_key
                and self.monitor_property_name == monitor_property_name
                and self.data_request_property_name == data_request_property_name
                and self.app_crypto_key
                and self.dev_crypto_key
            )
            if same_device:
                self.log("configure_noop", {"dsn": dsn, "device_ip": device_ip})
                return

            # Reset the entire state for the new device configuration.
            self.dsn = dsn
            self.device_ip = device_ip
            self.device_scheme = device_scheme or "https"
            self.lan_key = lan_key
            self.monitor_property_name = monitor_property_name
            self.data_request_property_name = data_request_property_name
            self.seq = 0
            self.command_queue = deque()
            self.command_payload = protocol.build_empty_payload(self.seq)
            self.app_sign_key = None
            self.app_crypto_key = None
            self.app_iv_seed = None
            self.dev_crypto_key = None
            self.dev_iv_seed = None
            self.random_2 = self._generate_random_2()
            self.time_2 = self._generate_time_2()
            self.registered = False
            self.last_monitor = {}
            self.last_monitor_raw = {}
            self.last_monitor_b64 = None
            self.last_monitor_received_at = None
            self.last_monitor_command_sent_at = None
            self._monitor_command_payloads.clear()
            self._monitor_request_pending = False
            self.last_properties = {}
            self.last_properties_received_at = None
            self._properties_request_pending = False
        self.log(
            "configured", {"dsn": dsn, "device_ip": device_ip, "scheme": device_scheme}
        )
        await self._save_server_settings(
            dsn=self.dsn,
            device_ip=self.device_ip,
            lan_key=self.lan_key,
            device_scheme=self.device_scheme,
            monitor_property_name=self.monitor_property_name,
            data_request_property_name=self.data_request_property_name,
        )

    async def _save_server_settings(
        self,
        dsn: str,
        device_ip: str,
        lan_key: str,
        device_scheme: str,
        monitor_property_name: str,
        data_request_property_name: str,
    ):
        if self.settings.server_settings_path == "":
            return
        data = {
            "dsn": dsn,
            "device_ip": device_ip,
            "lan_key": lan_key,
            "device_scheme": device_scheme,
            "monitor_property_name": monitor_property_name,
            "data_request_property_name": data_request_property_name,
        }
        try:
            with open(self.settings.server_settings_path, "w") as f:
                json.dump(data, f, indent=4)
                f.close()
            self.logger.info(f"State saved to {self.settings.server_settings_path}")
        except Exception as e:
            self.logger.error(f"Error saving state: {e}")

    def _load_server_settings(self):
        if self.settings.server_settings_path == "":
            return
        try:
            with open(self.settings.server_settings_path, "r") as f:
                data = json.load(f)
                f.close()
            self.dsn = data.get("dsn")
            self.device_ip = data.get("device_ip")
            self.lan_key = data.get("lan_key")
            self.device_scheme = data.get("device_scheme")
            self.monitor_property_name = data.get("monitor_property_name")
            self.data_request_property_name = data.get("data_request_property_name")

            self.logger.info(f"State loaded from {self.settings.server_settings_path}")
        except Exception as e:
            self.logger.error(f"Error loading state: {e}")

    async def rekey(self) -> None:
        """
        Resets cryptographic keys and session state to force a new key exchange.
        """
        async with self.lock:
            self.app_sign_key = None
            self.app_crypto_key = None
            self.app_iv_seed = None
            self.dev_crypto_key = None
            self.dev_iv_seed = None
            self.random_2 = self._generate_random_2()
            self.time_2 = self._generate_time_2()
            self.seq = 0
            self.command_queue = deque()
            self.command_payload = protocol.build_empty_payload(self.seq)
            self._monitor_request_pending = False
            self._properties_request_pending = False
            self.registered = False
        self.logger.info("rekey_reset")

    async def init_crypto(self, random_1: str, time_1: str | int) -> None:
        """
        Derives and initializes all session keys using values from the key exchange.
        """
        if not self.lan_key:
            raise ValueError("LAN key not set; configure server first")

        (
            app_sign_key,
            app_crypto_key,
            app_iv_seed,
            dev_crypto_key,
            dev_iv_seed,
        ) = protocol.derive_keys(
            self.lan_key, random_1, self.random_2, str(time_1), str(self.time_2)
        )

        async with self.lock:
            self.app_sign_key = app_sign_key
            self.app_crypto_key = app_crypto_key
            self.app_iv_seed = app_iv_seed
            self.dev_crypto_key = dev_crypto_key
            self.dev_iv_seed = dev_iv_seed
            self.seq = 0
            self.command_payload = protocol.build_empty_payload(self.seq)
        self.log("crypto_init", {"app_crypto_key": True, "dev_crypto_key": True})

    # --- state queries ---
    def is_configured(self) -> bool:
        """Returns True if the server has been configured with device details."""
        return bool(self.dsn and self.device_ip and self.lan_key)

    def keys_ready(self) -> bool:
        """Returns True if the cryptographic session keys have been derived."""
        return bool(self.app_crypto_key and self.app_iv_seed and self.app_sign_key)

    # --- command queue ---
    async def queue_command(self, command: str) -> None:
        """Adds a high-level device command to the outgoing queue."""
        if not self.is_configured():
            raise ValueError("Server not configured")
        payload = {
            "seq_no": protocol.pad_seq(self.seq),
            "data": {
                "properties": [
                    {
                        "property": {
                            "base_type": "string",
                            "dsn": self.dsn,
                            "name": self.data_request_property_name,
                            "value": f"{command}\n",
                        }
                    }
                ]
            },
        }
        async with self.lock:
            if len(self.command_queue) >= self.settings.queue_max_size:
                raise OverflowError("Command queue is full")
            payload_str = json.dumps(payload, separators=(",", ":"))
            self.command_queue.append(payload_str)
            self.last_command = command
        self.log("queue_command", {"command": command})

    async def queue_monitor(self) -> None:
        """Adds a request for the device's monitoring status to the queue."""
        if not self.is_configured():
            return
        monitor_cmd = {
            "cmds": [
                {
                    "cmd": {
                        "cmd_id": 1,
                        "data": "",
                        "method": "GET",
                        "resource": f"property.json?name={self.monitor_property_name}",
                        "uri": "/local_lan/property/datapoint.json",
                    }
                }
            ]
        }
        async with self.lock:
            if self._monitor_request_pending:
                return
            payload = json.dumps(
                {"seq_no": protocol.pad_seq(self.seq), "data": monitor_cmd},
                separators=(",", ":"),
            )
            self.command_queue.append(payload)
            self._monitor_command_payloads.add(payload)
            self._monitor_request_pending = True
        self.log("queue_monitor")

    async def queue_properties(self) -> None:
        """Adds a request for all device properties to the queue."""
        if not self.is_configured():
            return
        properties_cmd = {
            "cmds": [
                {
                    "cmd": {
                        "cmd_id": 1,
                        "data": "",
                        "method": "GET",
                        "resource": "property.json?name=''",
                        "uri": "/local_lan/property/datapoint.json",
                    }
                }
            ]
        }
        async with self.lock:
            if self._properties_request_pending:
                return
            self.command_queue.append(
                json.dumps(
                    {"seq_no": protocol.pad_seq(self.seq), "data": properties_cmd},
                    separators=(",", ":"),
                )
            )
            self._properties_request_pending = True
        self.log("queue_properties")

    async def next_command_payload(self) -> dict[str, Any]:
        """
        Retrieves the next command from the queue for sending to the device.
        If the queue is empty, it returns an empty "heartbeat" payload.
        """
        async with self.lock:
            if self.command_queue:
                payload = self.command_queue.popleft()
                if payload in self._monitor_command_payloads:
                    self._monitor_command_payloads.remove(payload)
                    self.last_monitor_command_sent_at = time.monotonic()
            else:
                payload = protocol.build_empty_payload(self.seq)
            current_seq = self.seq
            self.seq += 1
        return {"payload": payload, "seq": current_seq}

    async def set_registered(self, value: bool) -> None:
        async with self.lock:
            self.registered = value

    # --- datapoints ---
    async def handle_datapoint(self, decrypted_json: dict) -> None:
        """
        Processes a decrypted data payload from the device, updating the
        appropriate data snapshot (properties or monitor).
        """
        data_block = decrypted_json.get("data", {})
        async with self.lock:
            if "properties" in data_block:
                self.last_properties = data_block["properties"]
                self.last_properties_received_at = time.time()
                self._properties_request_pending = False
                self.log(
                    "properties_datapoint", {"count": len(data_block["properties"])}
                )
                return

            monitor_value = data_block.get("value")
            if monitor_value:
                self.last_monitor = {"raw_value_len": len(monitor_value)}
                self.last_monitor_b64 = monitor_value
                self.last_monitor_raw = decrypted_json
                self.last_monitor_received_at = time.time()
                self._monitor_request_pending = False
                self.log("monitor_datapoint", {"raw_value_len": len(monitor_value)})
            else:
                self.last_monitor = decrypted_json
                self.last_monitor_raw = decrypted_json
                self.last_monitor_b64 = None
                self.last_monitor_received_at = time.time()
                self._monitor_request_pending = False
                self.log("monitor_datapoint", {"monitor_keys": list(data_block.keys())})

    # --- snapshots ---
    async def snapshot_monitor(self) -> dict[str, Any]:
        """Returns the latest monitoring data snapshot."""
        async with self.lock:
            monitor_payload = self.last_monitor_raw or self.last_monitor or {}
            return {
                "monitor": monitor_payload,
                "monitor_b64": self.last_monitor_b64,
                "received_at": self.last_monitor_received_at,
            }

    async def snapshot_properties(self) -> dict[str, Any]:
        """Returns the latest properties data snapshot."""
        async with self.lock:
            return {
                "properties": self.last_properties,
                "received_at": self.last_properties_received_at,
            }

    async def get_property_value(self, property_name: str) -> Any | None:
        """Retrieves a single property value from the last known snapshot."""
        async with self.lock:
            if property_name in self.last_properties:
                return self.last_properties[property_name]
            for entry in self.last_properties.values():
                if (
                    isinstance(entry, dict)
                    and entry.get("property", {}).get("name") == property_name
                ):
                    return entry
        return None

    # --- logging helper ---
    def log(self, event: str, details: dict | None = None) -> None:
        """Convenience method for logging with redacted details."""
        safe_details = redact(details, (self.dsn, self.lan_key))
        self.logger.info(event, extra={"details": safe_details})

    def log_telemetry(self, event: str, details: dict) -> None:
        """Forward redacted telemetry without retaining it in diagnostics."""
        safe_details = redact(details, (self.dsn, self.lan_key))
        self.logger.info(
            event,
            extra={
                "details": safe_details,
                "exclude_from_diagnostics": True,
            },
        )
