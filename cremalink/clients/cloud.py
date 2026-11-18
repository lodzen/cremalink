from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
import time

import requests

from cremalink.domain import create_cloud_device
from cremalink.local_server_app.logging import log_event
from cremalink.resources import load_api_config

_LOGGER = logging.getLogger(__name__)

# Bounded retry for transient cloud API failures (timeouts, 5xx) during
# discovery/LAN-lookup, ported from delonghi_coffee's RETRY_COUNT/RETRY_DELAY
# convention (constitution Principle IV; spec FR-014).
RETRY_COUNT = 3
RETRY_DELAY = 2  # seconds; linear backoff between attempts
REQUEST_TIMEOUT = (5, 15)  # (connect, read) seconds; constitution Principle VI

# OEM identifier prefixes known to denote a *non*-coffee De'Longhi appliance
# on the same Ayla/cloud platform (e.g. the Pinguino air conditioner exposes
# oem_model "DL-pac" — confirmed in delonghi_coffee's is_coffee_oem_model).
# Unknown/empty models default to True — benefit of the doubt for a genuine
# but not-yet-mapped coffee maker (mirrors delonghi_coffee; spec FR-002).
NON_COFFEE_OEM_PREFIXES: tuple[str, ...] = ("DL-pac",)


def is_coffee_device(oem_model: str | None) -> bool:
    """Return True if the OEM model string denotes a coffee machine."""
    if not oem_model:
        return True
    return not oem_model.startswith(NON_COFFEE_OEM_PREFIXES)


def _property_family(name: str) -> str:
    """Coarse datapoint family for catalogue diagnostics (counts only).

    Buckets follow the ECAM610 map (research/cloud-properties.md):
    d0XX/d1XX recipes, d250+ bean adapt, d261+ menu order, d280+ machine
    settings, d5XX factory/statistics, d7XX beverage counters, d8XX
    water/descale.
    """
    if name.endswith("_rec_priority"):
        return "menu_order"
    if "beansystem" in name or "_bs_" in name:
        return "bean_adapt"
    if re.search(r"_\d+_rec_", name):
        return "profile_recipe"  # a6f0 stored values
    if "_rec_" in name:
        return "rec_descriptor"  # 0xB0 factory descriptors
    m = re.match(r"d(\d)\d\d_", name)
    if m:
        return {
            "2": "settings",
            "5": "factory_statistics",
            "7": "beverage_counters",
            "8": "water_descale",
        }.get(m.group(1), "other")
    if "monitor" in name:
        return "monitor"
    if "name" in name:
        return "names"
    if "profile" in name:
        return "profiles"
    return "other"


def _retry(func):
    """Retry decorator: bounded attempts with linear backoff on transient errors.

    Applied only to read-only discovery/LAN-lookup calls (FR-014). 4xx client
    errors other than 429 (rate limited) are not retried.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_err: Exception | None = None
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                return func(*args, **kwargs)
            except requests.RequestException as err:
                if isinstance(err, requests.HTTPError) and err.response is not None:
                    status = err.response.status_code
                    if 400 <= status < 500 and status != 429:
                        raise
                last_err = err
                if attempt < RETRY_COUNT:
                    owner = args[0]
                    log_event(
                        getattr(owner, "logger", _LOGGER),
                        "cloud_retry",
                        {
                            "attempt": attempt,
                            "attempt_limit": RETRY_COUNT,
                            "function": func.__name__,
                            "error": str(err),
                            "retry_delay_seconds": RETRY_DELAY,
                        },
                        diagnostic_sensitive_values=(
                            getattr(owner, "access_token", None),
                        ),
                        operational_sensitive_values=(
                            getattr(owner, "access_token", None),
                        ),
                        level=logging.DEBUG,
                    )
                    time.sleep(RETRY_DELAY)
        raise last_err

    return wrapper


class Client:
    """
    Client for interacting with the Ayla IoT cloud platform.
    Manages authentication (access and refresh tokens) and device discovery.
    """

    def __init__(self, token_path: str, *, logger: logging.Logger | None = None):
        self.logger = logger or _LOGGER
        # Ensure the token_path points to a JSON file.
        if not token_path.endswith(".json"):
            raise ValueError("token_path must point to a .json file")

        # Load API configuration from resources.
        self.api_conf = load_api_config()
        self.gigya_api = self.api_conf["GIGYA"]
        self.ayla_api = self.api_conf["AYLA"]

        self.token_agent = self.api_conf["USER_AGENT"]["TOKEN"]
        self.api_agent = self.api_conf["USER_AGENT"]["API"]

        self.token_path = token_path
        # Token refreshes rotate the refresh token in the file; serialise them.
        self._token_lock = threading.Lock()
        # Retrieve or refresh the access token upon initialization.
        self.access_token = self.__get_access_token()
        # Fetch the list of devices associated with the account.
        self.devices = requests.get(
            url=f"{self.ayla_api.get('API_URL')}/devices.json",
            headers={
                "User-Agent": self.api_agent,
                "Authorization": f"auth_token {self.access_token}",
                "Accept": "application/json",
            },
        ).json()

    def get_devices(self):
        """
        Retrieves a list of Device Serial Numbers (DSNs) for all registered devices.

        Returns:
            list[str]: A list of DSNs.
        """
        devices: list[str] = []
        for device in self.devices:
            devices.append(device["device"]["dsn"])
        return devices

    def get_device(self, dsn: str, device_map_path: str | None = None):
        """
        Retrieves a specific cloud device by its DSN.

        Args:
            dsn (str): The Device Serial Number of the desired device.
            device_map_path (str | None): Optional path to a device-specific
                command map file.

        Returns:
            CloudDevice | None: An instance of CloudDevice if found, otherwise None.
        """
        for device_dsn in self.get_devices():
            if device_dsn == dsn:
                return create_cloud_device(
                    device_dsn,
                    self.access_token,
                    device_map_path,
                    event_logger=self.logger,
                    token_refresher=self.refresh_access_token,
                )
        return None

    @_retry
    def list_account_devices(self) -> list[dict]:
        """Discover coffee-machine devices on the account (cloud-assisted onboarding).

        Unlike :meth:`get_devices` (kept unchanged for backward compatibility),
        this re-fetches ``/devices.json`` and returns one enriched dict per
        *coffee-machine* device only — non-coffee appliances (e.g. a Pinguino
        A/C on the same account) are excluded, not just flagged (FR-002).

        Returns:
            list[dict]: Each with ``dsn``, ``product_name``, ``oem_model``,
            ``lan_enabled``, ``connection_status``.
        """
        resp = requests.get(
            url=f"{self.ayla_api.get('API_URL')}/devices.json",
            headers={
                "User-Agent": self.api_agent,
                "Authorization": f"auth_token {self.access_token}",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        raw_devices = [d["device"] for d in resp.json()]
        self.devices = [{"device": d} for d in raw_devices]

        discovered: list[dict] = []
        for device in raw_devices:
            oem_model = device.get("oem_model") or device.get("model")
            if not is_coffee_device(oem_model):
                continue
            discovered.append(
                {
                    "dsn": device.get("dsn"),
                    "product_name": device.get("product_name"),
                    "oem_model": oem_model,
                    "lan_enabled": bool(device.get("lan_enabled", False)),
                    "connection_status": device.get("connection_status"),
                }
            )
        return discovered

    @_retry
    def get_serial_number(self, dsn: str) -> str | None:
        """Fetch the raw ``d270_serialnumber`` property for a device.

        Used as an input to model detection (FR-004); returns ``None`` if the
        property is absent rather than raising, so callers can safely fall
        through to the next detection tier.
        """
        resp = requests.get(
            url=f"{self.ayla_api.get('API_URL')}/dsns/{dsn}/properties/d270_serialnumber.json",
            headers={
                "User-Agent": self.api_agent,
                "Authorization": f"auth_token {self.access_token}",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("property", {}).get("value") or None

    @_retry
    def fetch_properties(self, dsn: str) -> dict[str, str | None]:
        """Every datapoint of a device in ONE request — ``{name: value}``.

        This is the Coffee Link app's property-store sync (``syncProperties``
        fetch-all; research.md R11, ``captures/cloud_fetch.json``). Values
        are the cloud's *last-published* copies (protocol.md §13 staleness
        lesson) — fine for factory descriptors and menu order, not for a
        brew's live recipe values.
        """
        resp = requests.get(
            url=f"{self.ayla_api.get('API_URL')}/dsns/{dsn}/properties.json",
            headers={
                "User-Agent": self.api_agent,
                "Authorization": f"auth_token {self.access_token}",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        entries = [
            entry["property"]
            for entry in resp.json()
            if isinstance(entry.get("property"), dict) and entry["property"].get("name")
        ]
        properties = {p["name"]: p.get("value") for p in entries}
        timestamps = [p["data_updated_at"] for p in entries if p.get("data_updated_at")]
        families: dict[str, int] = {}
        for name in properties:
            families.setdefault(_property_family(name), 0)
            families[_property_family(name)] += 1
        log_event(
            self.logger,
            "cloud_properties_fetched",
            {
                "dsn": dsn,
                "status_code": resp.status_code,
                "total": len(properties),
                "with_value": sum(1 for v in properties.values() if v),
                "empty": sum(1 for v in properties.values() if not v),
                "families": families,
                "oldest_update": min(timestamps) if timestamps else None,
                "newest_update": max(timestamps) if timestamps else None,
            },
            level=logging.DEBUG,
        )
        return properties

    @_retry
    def create_datapoint(self, dsn: str, name: str, value) -> dict:
        """Write one datapoint via the cloud channel — the app's announce path.

        Verified endpoint (``AylaProperty.createDatapointCloud`` /
        ``createDatapointEndpoint``, Coffee Link 4.9.6; DECODED.md):
        ``POST apiv1/dsns/{dsn}/properties/{name}/datapoints.json`` with
        ``{"datapoint": {"value": ...}}`` and ``x-ayla-source: Mobile``.
        This is the channel that carries ``device_connected`` to the
        machine's cloud uplink — independent of the LAN ``local_reg``
        module, so it is the recovery lever when the LAN session is dead
        (research.md R17). Whether a wedged datapoint engine processes it
        is unverified — callers should treat it as a probe, not a fix.
        """
        resp = requests.post(
            url=f"{self.ayla_api.get('API_URL')}/dsns/{dsn}/properties/{name}/datapoints.json",
            headers={
                "User-Agent": self.api_agent,
                "Authorization": f"auth_token {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-ayla-source": "Mobile",
            },
            json={"datapoint": {"value": value}},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        log_event(
            self.logger,
            "cloud_datapoint_written",
            {
                "dsn": dsn,
                "property": name,
                "status_code": resp.status_code,
                "echo": body.get("datapoint", {}).get("echo"),
            },
            level=logging.DEBUG,
        )
        return body

    @_retry
    def get_lan_config(self, dsn: str) -> dict:
        """Fetch LAN connectivity details for a device (FR-006).

        Ported from ``delonghi_coffee``'s ``api.get_lan_config`` — same Ayla
        platform, same account. Never raises just because LAN is disabled;
        returns ``lan_enabled: False`` with the other fields ``None`` instead
        (FR-008).
        """
        result: dict = {
            "lan_enabled": False,
            "lanip_key": None,
            "lan_ip": None,
            "status": None,
        }

        resp = requests.get(
            url=f"{self.ayla_api.get('API_URL')}/dsns/{dsn}.json",
            headers={
                "User-Agent": self.api_agent,
                "Authorization": f"auth_token {self.access_token}",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        device = resp.json().get("device", {})
        result["lan_enabled"] = bool(device.get("lan_enabled", False))
        result["lan_ip"] = device.get("lan_ip")
        result["status"] = device.get("connection_status")

        if result["lan_enabled"]:
            lan_resp = requests.get(
                url=f"{self.ayla_api.get('API_URL')}/devices/{dsn}/lan.json",
                headers={
                    "User-Agent": self.api_agent,
                    "Authorization": f"auth_token {self.access_token}",
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if lan_resp.status_code != 404:
                lan_resp.raise_for_status()
                lanip = lan_resp.json().get("lanip", {})
                result["lanip_key"] = lanip.get("lanip_key")

            if not result["lanip_key"]:
                # Fallback endpoint — some accounts/devices expose the LAN
                # key here instead (ported from delonghi_coffee's
                # get_lan_config alternate-endpoint fallback).
                alt_resp = requests.get(
                    url=f"{self.ayla_api.get('API_URL')}/devices/{dsn}/connection_config.json",
                    headers={
                        "User-Agent": self.api_agent,
                        "Authorization": f"auth_token {self.access_token}",
                        "Accept": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if alt_resp.status_code != 404:
                    alt_resp.raise_for_status()
                    result["lanip_key"] = alt_resp.json().get("local_key")

        return result

    def refresh_access_token(self) -> str:
        """Fetch a new access token with the stored refresh token.

        The refresh token rotates and is written back to ``token_path``.
        Handed to cloud transports, which call it on HTTP 401.
        """
        with self._token_lock:
            self.access_token = self.__get_access_token()
            return self.access_token

    def __get_access_token(self):
        """
        Retrieves a valid access token, refreshing it if necessary using the refresh token.
        """
        refresh_token = self.__get_refresh_token()
        # If no refresh token is found, prompt the user to provide one.
        if not refresh_token or refresh_token == "":
            self.__set_refresh_token("")
            raise ValueError(
                f"No refresh token found. Open {self.token_path} and add a valid refresh token."
            )
        response = requests.post(
            url=f"{self.ayla_api.get('OAUTH_URL')}/users/refresh_token.json",
            headers={
                "User-Agent": self.token_agent,
                "Content-Type": "application/json",
            },
            json={"user": {"refresh_token": refresh_token}},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            # If successful, extract new access and refresh tokens.
            data = response.json()
            new_access_token = data["access_token"]
            new_refresh_token = data["refresh_token"]
            # Update the stored refresh token.
            self.__set_refresh_token(new_refresh_token)
            return new_access_token
        else:
            # Raise an error if access token retrieval fails.
            raise ValueError(
                f"Failed to get access token: {response.status_code} {response.text}"
            )

    def __get_refresh_token(self):
        """
        Reads the refresh token from the token file.

        Returns:
            str | None: The refresh token if found, otherwise None.
        """
        if os.path.exists(self.token_path):
            with open(self.token_path, "r") as f:
                data = f.read()
                f.close()
                if data:
                    token_data = json.loads(data)
                    return token_data.get("refresh_token", None)
        return None

    def __set_refresh_token(self, refresh_token: str):
        """
        Writes the provided refresh token to the token file.

        Args:
            refresh_token (str): The new refresh token to store.
        """
        with open(self.token_path, "w+") as f:
            # Read existing data to preserve other potential keys.
            data = f.read()
            token_data = json.loads(data) if data else {}
            token_data["refresh_token"] = refresh_token
            f.write(json.dumps(token_data, indent=2))
            f.close()
