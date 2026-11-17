"""
This module provides the `CloudTransport` class, which handles communication
with a coffee machine via the manufacturer's cloud API (Ayla Networks).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests

from cremalink.parsing.monitor.decode import build_monitor_snapshot
from cremalink.resources import load_api_config
from cremalink.transports.base import DeviceTransport

#: (connect, read) seconds for every cloud call — a hung request must not
#: block the caller (Home Assistant runs these in a shared executor).
REQUEST_TIMEOUT = (5, 15)


def _to_epoch(value: Any) -> float:
    """Ayla timestamps (ISO-8601 like ``2026-11-15T07:42:01Z``) → epoch."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


class CloudTransport(DeviceTransport):
    """
    A transport for communicating with a device via the cloud API.

    This transport interacts directly with the Ayla cloud service endpoints,
    using a short-lived access token for authentication. Upon initialization,
    it fetches key device metadata from the cloud and stores it.
    """

    #: The cloud API has no response mailbox and cannot ask the device to
    #: republish a property; answers arrive as republished ``dNNN``
    #: properties instead. ``Device`` checks these flags before using
    #: ``pop_response`` / ``request_property``.
    supports_mailbox = False
    supports_property_requests = False

    def __init__(
        self,
        dsn: str,
        access_token: str,
        device_map_path: str | None = None,
        token_refresher: Callable[[], str] | None = None,
    ) -> None:
        """
        Initializes the CloudTransport.

        Args:
            dsn: The Device Serial Number.
            access_token: A valid OAuth access token for the cloud API.
            device_map_path: Optional path to a device-specific command map file.
            token_refresher: Returns a fresh access token. Called once when a
                request answers HTTP 401 (expired token); the request is then
                retried. Without it an expired token fails every call.
        """
        self.api_conf = load_api_config()
        self.gigya_api = self.api_conf.get("GIGYA")
        self.ayla_api = self.api_conf["AYLA"]

        self.api_agent = self.api_conf["USER_AGENT"]["API"]

        self.dsn = dsn
        self.access_token = access_token
        self.token_refresher = token_refresher
        self.device_map_path = device_map_path
        self.command_map: dict[str, Any] = {}
        self.property_map: dict[str, Any] = {}

        # Fetch device metadata from the cloud immediately upon initialization.
        device = self._get(".json").get("device", {})
        self.id = device.get("key")  # The Ayla internal device ID
        self.model = device.get("model")
        self.is_lan_enabled = device.get("lan_enabled", False)
        self.type = device.get("type")
        self.is_online = device.get("connection_status", False) == "Online"
        self.ip = device.get("lan_ip")
        # Firmware of the Wi-Fi module and when the cloud last updated it.
        self.sw_version = device.get("sw_version")
        self.oem_model = device.get("oem_model")
        self.module_updated_at = device.get("module_updated_at")

        # Fetch LAN key, which might be needed for other operations.
        try:
            lan = self._get("/lan.json") or {}
            self.lan_key = lan.get("lanip", {}).get("lanip_key")
        except requests.HTTPError:
            self.lan_key = None

    def configure(self) -> None:
        """Configuration is handled during __init__, so this is a no-op."""
        return

    # ---- helpers ----
    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Authenticated request with timeout; renews an expired token once."""
        call = requests.get if method == "GET" else requests.post
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(2):
            response = call(
                url=url,
                headers={
                    "User-Agent": self.api_agent,
                    "Authorization": f"auth_token {self.access_token}",
                    "Accept": "application/json",
                    **extra_headers,
                },
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
            if (
                response.status_code == 401
                and attempt == 0
                and self.token_refresher is not None
            ):
                self.access_token = self.token_refresher()
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable")

    def _get(self, path: str) -> dict:
        """Helper for making authenticated GET requests using the device DSN."""
        url = f"{self.ayla_api.get('API_URL')}/dsns/{self.dsn}{path}"
        return self._request("GET", url).json()

    def _get_by_id(self, path: str) -> dict:
        """Helper for making authenticated GET requests using the internal device ID."""
        url = f"{self.ayla_api.get('API_URL')}/devices/{self.id}{path}"
        return self._request("GET", url).json()

    def _post(self, path: str, data: dict) -> dict:
        """Helper for making authenticated POST requests."""
        url = f"{self.ayla_api.get('API_URL')}/dsns/{self.dsn}{path}"
        return self._request(
            "POST", url, headers={"Content-Type": "application/json"}, json=data
        ).json()

    # ---- DeviceTransport Implementation ----
    def send_command(
        self, command: str, alternative_property: str | None = None
    ) -> Any:
        """Sends a command to the device by creating a new 'datapoint' via the cloud API."""
        payload = {"datapoint": {"value": command}}
        data_request = alternative_property or self.property_map.get("data_request")
        return self._post(
            path=f"/properties/{data_request}/datapoints.json", data=payload
        )

    def set_mappings(
        self, command_map: dict[str, Any], property_map: dict[str, Any]
    ) -> None:
        """Stores the provided command and property maps on the instance."""
        self.command_map = command_map
        self.property_map = property_map

    def get_properties(self) -> Any:
        """Fetches all properties for the device from the cloud API."""
        return self._get("/properties.json")

    def get_property(self, name: str) -> Any:
        """Fetches a single, specific property by name."""
        props = self._get(f"/properties.json?names[]={name}")
        # The API returns a list, even for a single property.
        if props and isinstance(props, list):
            return props[0].get("property")
        return None

    def write_property(self, name: str, value: Any) -> Any:
        """Writes a single named property via a cloud datapoint."""
        payload = {"datapoint": {"value": value}}
        return self._post(path=f"/properties/{name}/datapoints.json", data=payload)

    def request_property(self, name: str) -> Any:
        """Not available over the cloud API — refreshes happen on the
        device's own republish cadence (native protocol features are
        LAN-only)."""
        raise NotImplementedError("request_property is LAN-only")

    def pop_response(self) -> bytes | None:
        """Not available over the cloud API — the `data_response` mailbox
        is LAN-only."""
        raise NotImplementedError("pop_response is LAN-only")

    def get_monitor(self) -> Any:
        """
        Fetches, parses, and returns the device's monitoring status.

        This works by fetching the specific 'monitor' property, extracting its
        base64 value, and then decoding it into a structured snapshot.
        """
        property_name = self.property_map.get("monitor", "monitor")
        prop = self.get_property(property_name) or {}
        raw_b64 = prop.get("value")
        # Ayla stores when the machine published the frame as ISO-8601
        # ``data_updated_at`` (there is no ``updated_at``).
        received_ts = _to_epoch(prop.get("data_updated_at") or prop.get("updated_at"))
        payload = {
            "monitor": {"data": {"value": raw_b64}},
            "monitor_b64": raw_b64,
            "received_at": received_ts,
        }
        return build_monitor_snapshot(
            payload, source="cloud", device_id=self.dsn or self.id
        )

    def refresh_monitor(self) -> Any:
        """
        The cloud API does not provide a direct way to force a monitor refresh.
        This method is a no-op.
        """
        return None

    def health(self) -> Any:
        """
        Returns the device's online status as determined during initialization.
        This does not perform a live health check.
        """
        return {"online": self.is_online}
