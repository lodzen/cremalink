"""
This module defines the core `Device` class, which represents a physical coffee
machine. It serves as the primary high-level interface for interacting with a
device, abstracting away the underlying transport and command details.
"""

from __future__ import annotations

import json
import logging
import time
from base64 import b64decode, b64encode
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, ClassVar

from cremalink.core.binary import hex_to_signed_decimal, signed_decimal_to_hex
from cremalink.devices import device_map
from cremalink.ecam import answers as ecam_answers
from cremalink.ecam import builder as ecam_builder
from cremalink.ecam import catalog as ecam_catalog
from cremalink.ecam import machine_profiles as ecam_profiles_mod
from cremalink.ecam import profiles as ecam_profile_parser
from cremalink.ecam import settings as ecam_settings
from cremalink.ecam import statistics as ecam_statistics
from cremalink.ecam.session import SessionGate
from cremalink.local_server_app.logging import log_event
from cremalink.parsing.monitor.frame import MonitorFrame
from cremalink.parsing.monitor.model import MonitorSnapshot
from cremalink.parsing.monitor.profile import MonitorProfile
from cremalink.parsing.monitor.view import MonitorView
from cremalink.transports.base import DeviceTransport

logger = logging.getLogger(__name__)

#: Monitor ``status`` enum names of the standby family (device-map enums).
_STANDBY_STATUS_NAMES = frozenset(
    {"in_standby", "going_to_sleep", "sleeping", "deep_sleep"}
)


class MachineStandbyError(RuntimeError):
    """The requested operation needs a machine that is switched on."""


APP_ID_HEX = "C0FFEEEE"


def _load_device_map(device_map_path: str | None) -> dict[str, Any]:
    """Loads a JSON device map from the given file path."""
    if not device_map_path:
        return {}
    with open(device_map_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _encode_command(hex_command: str, app_id: str | None = None) -> str:
    """
    Encodes a hexadecimal command string into the base64 format expected by the device.
    It prepends the command bytes with a current timestamp.
    """
    head = bytearray.fromhex(hex_command)
    timestamp = bytearray.fromhex(hex(int(time.time()))[2:])
    app_id_bytes = bytearray.fromhex(app_id) if app_id else b""
    return b64encode(head + timestamp + app_id_bytes).decode("utf-8")


@dataclass
class Device:
    """
    Represents a coffee machine, providing methods to control and monitor it.

    This class holds the device's state (e.g., IP, model) and uses a `DeviceTransport`
    object to handle the actual communication. Device-specific capabilities are
    loaded from a "device map" file.

    Attributes:
        transport: The transport object responsible for communication.
        dsn: Device Serial Number.
        model: The model identifier of the device.
        nickname: A user-defined name for the device.
        ip: The local IP address of the device.
        lan_key: The key used for LAN-based authentication.
        scheme: The communication scheme (e.g., 'http', 'mqtt').
        is_online: Boolean indicating if the device is currently reachable.
        last_seen: Timestamp of the last communication.
        firmware: The device's firmware version.
        serial: The device's serial number.
        coffee_count: The total number of coffees made.
        command_map: A mapping of command aliases to their hex codes.
        property_map: A mapping of property aliases to their technical names.
        monitor_profile: Configuration for parsing monitor data.
        extra: A dictionary for any other miscellaneous data.
    """

    transport: DeviceTransport
    dsn: str | None = None
    model: str | None = None
    nickname: str | None = None
    ip: str | None = None
    lan_key: str | None = None
    scheme: str | None = None
    is_online: bool | None = None
    last_seen: str | None = None
    firmware: str | None = None
    serial: str | None = None
    coffee_count: int | None = None
    command_map: dict[str, Any] = field(default_factory=dict)
    property_map: dict[str, Any] = field(default_factory=dict)
    monitor_profile: MonitorProfile = field(default_factory=MonitorProfile)
    extra: dict[str, Any] = field(default_factory=dict)
    # --- ECAM protocol metadata (populated from the device map) ---
    machine_profile: ecam_profiles_mod.MachineProfile | None = None
    n_profiles: int = 1
    capabilities: dict[str, Any] = field(default_factory=dict)
    statistics_source: str = "native"
    statistics_datapoints: list[str] = field(default_factory=list)
    settings_datapoints: list[str] = field(default_factory=list)
    catalog_datapoints: list[str] = field(default_factory=list)
    model_ref: dict[str, Any] = field(default_factory=dict)
    # Recipe index -> beverage id, in machine order (verified Soul table).
    recipe_beverage_order: list[int] = field(default_factory=list)
    # Tunables for the data_response mailbox polling loop.
    response_timeout: float = 8.0
    response_poll: float = 0.25
    #: Post-write confirmation window — longer than ``response_timeout``
    #: because the machine can commit ``0x90`` writes sluggishly under
    #: load (the app's own re-read waits ~2 s; we budget generously and
    #: poll the republished property meanwhile).
    write_confirm_seconds: float = 15.0
    # Internal per-device session/response state (never shared).
    _session_gate: SessionGate | None = field(default=None, init=False, repr=False)
    # Last 0xA9-acknowledged profile index (0 = none acked yet).
    current_profile: int = field(default=0, init=False)
    #: Min seconds between param-0x03EE current-profile reads — callers
    #: may hook this to a ~1 s monitor cycle, far too chatty for a
    #: register that only changes on a profile switch.
    profile_poll_seconds: float = 30.0
    _profile_polled_at: float = field(default=0.0, init=False, repr=False)

    @classmethod
    def from_map(
        cls,
        transport: DeviceTransport,
        device_map_path: str | None = None,
        *,
        event_logger: logging.Logger | None = None,
        **kwargs,
    ) -> Device:
        """
        Factory method to create a Device instance with a loaded device map.

        If `device_map_path` is not provided, it attempts to find one using the
        device's model.

        Args:
            transport: The communication transport to use.
            device_map_path: Optional path to the device map JSON file.
            **kwargs: Additional attributes to set on the Device instance.

        Returns:
            A configured Device instance.
        """
        if not device_map_path:
            device_map_path = device_map(cls.model) if cls.model else None

        map_data = _load_device_map(device_map_path)

        support = map_data.get("support", {})
        transport_name = transport.__class__.__name__
        warning_logger = event_logger or logger

        if (
            transport_name == "CloudTransport"
            and support.get("cloud") is False
            or transport_name == "LocalTransport"
            and support.get("local") is False
        ):
            log_event(
                warning_logger,
                "device_map_transport_unsupported",
                {"transport": transport_name},
                level=logging.WARNING,
            )

        command_map = (
            map_data.get("command_map", {}) if isinstance(map_data, dict) else {}
        )
        property_map = (
            map_data.get("property_map", {}) if isinstance(map_data, dict) else {}
        )
        monitor_profile_data = (
            map_data.get("monitor_profile", {}) if isinstance(map_data, dict) else {}
        )
        monitor_profile = MonitorProfile.from_dict(monitor_profile_data)
        machine_profile = ecam_profiles_mod.profile_from_map(
            map_data, logger_=warning_logger
        )

        # If the transport supports it, pass the mappings to it.
        if hasattr(transport, "set_mappings"):
            try:
                transport.set_mappings(command_map, property_map)
            except (OSError, ValueError, RuntimeError) as err:
                log_event(
                    warning_logger,
                    "transport_set_mappings_failed",
                    {"error_type": type(err).__name__},
                    level=logging.DEBUG,
                )

        return cls(
            transport=transport,
            command_map=command_map,
            property_map=property_map,
            monitor_profile=monitor_profile,
            machine_profile=machine_profile,
            n_profiles=int(map_data.get("n_profiles") or 1),
            capabilities=map_data.get("capabilities") or {},
            statistics_source=map_data.get("statistics_source") or "native",
            statistics_datapoints=list(map_data.get("statistics_datapoints") or []),
            settings_datapoints=list(map_data.get("settings_datapoints") or []),
            catalog_datapoints=list(map_data.get("catalog_datapoints") or []),
            model_ref=map_data.get("model_ref") or {},
            recipe_beverage_order=list(map_data.get("recipe_beverage_order") or []),
            **kwargs,
        )

    # --- Transport delegations ---
    def configure(self) -> None:
        """Configures the underlying transport."""
        self.transport.configure()

    def send_command(self, command: str) -> Any:
        """
        Encodes and sends a raw hex command to the device via the transport.

        Args:
            command: The hex command string to send.

        Returns:
            The response from the transport.
        """
        if self.property_map.get("app_id", None):
            app_id_hex = self._ensure_app_id()
            if not app_id_hex:
                raise ConnectionError("Could not set app_id, so cannot send command.")
            encoded = _encode_command(command, app_id_hex)
        else:
            encoded = _encode_command(command)

        return self.transport.send_command(encoded)

    def refresh_monitor(self) -> Any:
        """Requests a refresh of the device's monitoring data."""
        return self.transport.refresh_monitor()

    def get_properties(self) -> Any:
        """Fetches all available properties from the device."""
        return self.transport.get_properties()

    def get_property_aliases(self) -> list[str]:
        """Returns a list of all available property aliases from the device map."""
        return list(self.property_map.keys())

    def get_property(self, name: str) -> Any:
        """
        Fetches a single property by its alias or technical name.

        Args:
            name: The alias or name of the property to fetch.

        Returns:
            The value of the requested property.
        """
        actual_name = self.resolve_property(name, default=name)
        return self.transport.get_property(actual_name)

    def health(self) -> Any:
        """Checks the health of the device connection."""
        return self.transport.health()

    # --- Command map helpers ---
    def do(self, drink_name: str) -> Any:
        """
        Executes a command by its friendly name (e.g., 'espresso').

        Map entries are decoded fields, not captured frames:

        * ``power`` (``standby``/``wake``/``session_refresh``) builds via
          ``build_power``.
        * ``beverage_id`` entries build via ``build_brew``. With a
          ``recipe_datapoint`` the recipe is fetched live from the
          machine's republished profile-scoped property (``0xA6``) and the
          brew fails closed if it is unavailable; entries without one use
          their stored ``recipe`` hex (models with no recipe catalogue),
          and a recipe-less stop frame uses the default recipe.
        * Entries holding only a raw ``command`` hex are sent verbatim.

        Built frames go through the session gate.

        Args:
            drink_name: The name of the command to execute, as defined in the command_map.

        Returns:
            The response from the transport.

        Raises:
            ValueError: If the command name is not found in the device map.
            ConnectionError: If no recipe is available for a brew entry.
        """
        key = drink_name.lower().strip()
        entry = self.command_map.get(key)
        if not entry:
            raise ValueError(f"Command '{key}' not implemented; check device_map.")
        if "power" in entry:
            kind = ecam_builder.PowerCommand[str(entry["power"]).upper()]
            return self._send_gated(ecam_builder.build_power(kind, self._profile()))
        if "beverage_id" not in entry:
            hex_command = entry.get("command")
            if not hex_command:
                raise ValueError(f"Command '{key}' not implemented; check device_map.")
            return self.send_command(hex_command)
        beverage_id = int(entry["beverage_id"])
        action = int(entry.get("action", ecam_builder.BrewAction.START))
        entry_slot = int(entry.get("profile_slot", 1))
        slot = self.current_profile or entry_slot
        recipe = None
        datapoint = entry.get("recipe_datapoint")
        if datapoint:
            recipe = self._fetch_profile_recipe(
                slot, beverage_id, datapoint=datapoint if slot == entry_slot else None
            )
        elif entry.get("recipe") is not None:
            recipe = bytes.fromhex(entry["recipe"])
        elif action == ecam_builder.BrewAction.STOP:
            recipe = ecam_builder.DEFAULT_RECIPE
        if recipe is None:
            raise ConnectionError(
                f"no recipe available for '{key}' "
                f"(profile={slot}, beverage={beverage_id})"
            )
        frame = ecam_builder.build_brew(
            beverage_id,
            action,
            recipe,
            self._profile(),
            profile_slot=slot,
            accessory=int(entry.get("accessory", 0)),
        )
        return self._send_gated(frame)

    def get_commands(self) -> list[str]:
        """Returns a list of all available command names from the device map."""
        return list(self.command_map.keys())

    # --- Property map helpers ---
    def resolve_property(self, alias: str, default: str | None = None) -> str:
        """
        Translates a property alias to its technical name using the property_map.

        Args:
            alias: The property alias to resolve.
            default: A default value to return if the alias is not found.

        Returns:
            The resolved technical name, or the alias/default if not found.
        """
        return self.property_map.get(alias, default or alias)

    # --- Monitor helpers ---
    def get_monitor_snapshot(self) -> MonitorSnapshot:
        """
        Retrieves the latest raw monitoring data from the transport.
        """
        return self.transport.get_monitor()

    def get_monitor(self) -> MonitorView:
        """
        Retrieves and parses monitoring data into a structured, human-readable view.

        Returns:
            A `MonitorView` instance containing parsed status information.
        """
        snapshot = self.get_monitor_snapshot()
        return MonitorView(snapshot=snapshot, profile=self.monitor_profile)

    def get_monitor_frame(self) -> MonitorFrame | None:
        """
        Decodes the raw monitor data into a `MonitorFrame` for low-level analysis.

        Returns:
            A `MonitorFrame` if decoding is successful, otherwise None.
        """
        snapshot = self.get_monitor_snapshot()
        if not snapshot.raw_b64:
            return None
        try:
            return MonitorFrame.from_b64(snapshot.raw_b64)
        except (ValueError, TypeError):
            return None

    def _ensure_app_id(self) -> str | None:
        """Check the app_id matches the cremalink app id and try to set it"""
        app_id = self.get_property(self.property_map.get("app_id", "app_id")) or {}
        value = app_id.get("value")
        if value == "0":
            self._register_app_id(APP_ID_HEX)
            # sleep for a bit to allow the app id to get set
            time.sleep(7)
            return APP_ID_HEX
        elif value == hex_to_signed_decimal(APP_ID_HEX):
            self._refresh_app_id()
            return APP_ID_HEX
        if not isinstance(value, str):
            return None
        return signed_decimal_to_hex(value)

    def _register_app_id(self, app_id_hex: str) -> Any:
        """
        Sends a command to register the app id with the cloud.
        The register command is just the timestamp + app_id.
        """
        command = _encode_command("", app_id_hex)
        return self.transport.send_command(
            command, self.property_map.get("device_connected", "app_device_connected")
        )

    def _refresh_app_id(self) -> Any:
        """Sends a command to refresh the registered app id with the cloud."""
        hex_command = self.command_map.get("refresh", {}).get("command")
        command = _encode_command(hex_command, APP_ID_HEX)
        return self.transport.send_command(command)

    # ===== ECAM protocol layer ===========================================
    # All protocol-aware paths below build frames through `cremalink.ecam`
    # and send them via the existing transport pipeline. Session gating
    # stays internal — callers never announce the session themselves.

    def _profile(self) -> ecam_profiles_mod.MachineProfile:
        """The bound dialect; devices without a map get the Soul dialect."""
        if self.machine_profile is not None:
            return self.machine_profile
        return ecam_profiles_mod.NON_STRIKER

    # --- session gate ---
    def _session(self) -> SessionGate:
        if self._session_gate is None:
            self._session_gate = SessionGate(self._profile(), self._announce_session)
        return self._session_gate

    def _announce_session(self) -> None:
        """One session announce/refresh, shaped by the machine profile."""
        profile = self._profile()
        if profile.session == ecam_profiles_mod.SESSION_DEVICE_CONNECTED_TS:
            write = getattr(self.transport, "write_property", None)
            if write is None:
                raise ConnectionError(
                    "transport cannot write properties; session announce impossible"
                )
            write(
                self.resolve_property("device_connected", "device_connected"),
                int(time.time()),
            )
            # §14.8/DECODED.md: the app's connect sequence is
            # ``device_connected`` → ``s0`` handshake blob → traffic.
            # The announce alone left session commands unprocessed
            # (0x95 silent, 0x90 acked-but-not-committed, 0xA9 unacked
            # — 2026-10-14 and 2026-10-25 live runs); the handshake is
            # the missing session opener, re-sent on every refresh.
            self._send_blob(ecam_builder.build_handshake_blob())
            return
        # striker: 0xE8 handshake, then the app-id registration path.
        self.send_frame(ecam_builder.build_striker_handshake(profile))
        self._ensure_app_id()

    def send_frame(self, frame: bytes) -> Any:
        """Encode a protocol frame (timestamp + optional app-id trailer)
        and send it via the transport's command channel."""
        profile = self._profile()
        app_id = None
        if profile.trailer == ecam_profiles_mod.TRAILER_APP_ID:
            hex_id = self._ensure_app_id()
            if not hex_id:
                raise ConnectionError("Could not set app_id, so cannot send command.")
            app_id = int(str(hex_id), 16).to_bytes(4, "big")
        encoded = ecam_builder.encode_for_transport(frame, profile, app_id=app_id)
        try:
            return self.transport.send_command(
                encoded,
                alternative_property=self.resolve_property(
                    profile.command_property, profile.command_property
                ),
            )
        except TypeError:
            return self.transport.send_command(encoded)

    def _send_blob(self, blob: bytes) -> Any:
        """Send a non-frame payload (the ``s0`` handshake) — timestamp
        trailer like a frame but no ``0x0D`` structure (§14.8)."""
        encoded = ecam_builder.encode_for_transport(blob, self._profile(), raw=True)
        try:
            return self.transport.send_command(
                encoded,
                alternative_property=self.resolve_property(
                    self._profile().command_property,
                    self._profile().command_property,
                ),
            )
        except TypeError:
            return self.transport.send_command(encoded)

    def _send_gated(self, frame: bytes) -> Any:
        """Session-gated write path (0x83/0x84/0x90/0xA9 frames)."""
        self._session().ensure_fresh()
        return self.send_frame(frame)

    # --- response mailbox ---
    def _pop_response_frame(self) -> bytes | None:
        pop = getattr(self.transport, "pop_response", None)
        if pop is None:
            return None
        return pop()

    def _wait_answer(
        self, request_id: int, *, timeout: float | None = None
    ) -> bytes | None:
        """Poll the response mailbox for a ``0xD0`` frame echoing ``request_id``.

        Frames echoing a different request id are stale and dropped; a
        ``0xE1`` frame raises ``TimeoutError`` (machine-side abort).
        """
        deadline = time.monotonic() + (
            self.response_timeout if timeout is None else timeout
        )
        while True:
            frame = self._pop_response_frame()
            if frame is not None:
                if ecam_answers.is_sync_timeout(frame):
                    raise TimeoutError(
                        f"machine aborted request 0x{request_id:02x} (0xE1)"
                    )
                try:
                    if ecam_answers.answer_id(frame) == request_id:
                        return frame
                except ValueError:
                    logger.debug("dropping unparseable mailbox frame")
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.response_poll)

    # --- named-property helpers ---
    @staticmethod
    def _entry_value(entry: Any) -> Any:
        """Normalize a `get_property` result to the raw stored value."""
        seen = 0
        while isinstance(entry, dict) and seen < 4:
            if "value" in entry:
                entry = entry["value"]
            elif isinstance(entry.get("raw"), dict) and "value" in entry["raw"]:
                entry = entry["raw"]["value"]
            elif isinstance(entry.get("property"), dict):
                entry = entry["property"]
            else:
                return entry
            seen += 1
        return entry

    def _named_value(self, name: str) -> Any | None:
        """Latest stored value for a datapoint name, or ``None``."""
        try:
            entry = self.transport.get_property(name)
        except (OSError, ValueError, RuntimeError) as err:
            logger.debug("get_property(%s) failed: %s", name, err)
            return None
        if entry is None:
            return None
        return self._entry_value(entry)

    def _collect_named_properties(
        self, names: list[str], *, settle: float = 0.5, retries: int = 1
    ) -> dict[str, Any]:
        """Request each named datapoint, then read back what arrived.

        Returns ``{name: raw_value}`` — only names the machine actually
        republished appear in the result.
        """
        request = getattr(self.transport, "request_property", None)
        props: dict[str, Any] = {}
        missing = list(names)
        if request is None:
            for name in missing:
                value = self._named_value(name)
                if value is not None:
                    props[name] = value
            return props
        for _ in range(1 + retries):
            for name in missing:
                try:
                    request(name)
                except (OSError, ValueError, RuntimeError) as err:
                    logger.debug("request_property(%s) failed: %s", name, err)
            # Harvest whatever already landed, then wait on the head of
            # the queue — the machine serves ~one GET per poll cycle, so
            # republished answers arrive in ~request order. The budget
            # scales with the remaining queue depth; settle=0 stays a
            # single read pass.
            still_missing = []
            for name in missing:
                value = self._named_value(name)
                if value is None:
                    still_missing.append(name)
                else:
                    props[name] = value
            missing = still_missing
            deadline = time.monotonic() + settle * len(missing)
            while missing and time.monotonic() < deadline:
                name = missing[0]
                value = self._named_value(name)
                if value is None:
                    time.sleep(self.response_poll)
                    continue
                props[name] = value
                missing.pop(0)
            if not missing:
                break
        return props

    def _properties_by_name(self) -> dict[str, Any]:
        """Flatten the transport's property snapshot to ``{name: value}``."""
        props = self.transport.get_properties()
        raw = getattr(props, "raw", props)
        out: dict[str, Any] = {}
        if isinstance(raw, dict):
            for key, entry in raw.items():
                prop = entry.get("property") if isinstance(entry, dict) else None
                if isinstance(prop, dict):
                    out[prop.get("name") or key] = prop.get("value")
                elif isinstance(entry, dict) and "name" in entry:
                    out[entry["name"]] = entry.get("value")
                elif isinstance(entry, dict) and "value" in entry:
                    out[key] = entry["value"]
                else:
                    out[key] = entry
        elif isinstance(raw, list):
            for entry in raw:
                prop = entry.get("property") if isinstance(entry, dict) else None
                if isinstance(prop, dict) and prop.get("name"):
                    out[prop["name"]] = prop.get("value")
        return out

    def _recipe_property_name(self, profile_slot: int, beverage_id: int) -> str | None:
        """The datapoint holding the profile-scoped recipe, or ``None``.

        Live-verified on Soul LAN: ``0xA6`` requests do not answer on
        ``data_response`` — the recipe arrives on the republished
        ``dNNN_<prof>_rec_<label>`` property with
        ``NN = 38 + (prof-1)*21 + recipe_index``.
        """
        if not self.recipe_beverage_order or not self.catalog_datapoints:
            return None
        try:
            index = self.recipe_beverage_order.index(beverage_id) + 1
        except ValueError:
            return None
        number = 38 + (profile_slot - 1) * 21 + index
        prefix = f"d{number:03d}_{profile_slot}_rec_"
        for name in self.catalog_datapoints:
            if name.startswith(prefix):
                return name
        return None

    @staticmethod
    def _brew_recipe_bytes(blob: bytes) -> bytes | None:
        """Extract the brewable recipe TLVs from an ``a6f0`` blob,
        applying the app's drop filter (``brew_param_included``)."""
        if len(blob) < 8 or blob[0] != 0xD0 or blob[2:4] != b"\xa6\xf0":
            return None
        body = ecam_builder.trim_frame(blob)[6:-2]
        out = bytearray()
        i = 0
        while i < len(body):
            tag = body[i]
            width = 2 if tag in ecam_catalog.WIDE_TAGS else 1
            if i + 1 + width > len(body):
                break
            if ecam_builder.brew_param_included(tag):
                out += body[i : i + 1 + width]
            i += 1 + width
        return bytes(out) or None

    def _fetch_profile_recipe(
        self, profile_slot: int, beverage_id: int, datapoint: str | None = None
    ) -> bytes | None:
        """Fetch the profile-scoped recipe for ``(profile, beverage)``.

        Sends the ``0xA6`` trigger (republish hint) then reads the
        republished ``dNNN`` property back. ``datapoint`` names the
        property explicitly (command-map entries); otherwise it is
        derived from the catalogue. Returns the brew-ready recipe bytes
        or ``None`` when the machine didn't provide them.
        """
        name = datapoint or self._recipe_property_name(profile_slot, beverage_id)
        if name is None:
            return None
        # Trigger republish (0xA6 frame) — ignored if unsupported.
        try:
            self.send_frame(
                ecam_builder.build_recipe_values(
                    profile_slot, beverage_id, self._profile()
                )
            )
        except (OSError, ValueError, RuntimeError) as err:
            logger.debug("recipe republish trigger failed: %s", err)
        request = getattr(self.transport, "request_property", None)
        if request is not None:
            try:
                request(name)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("request_property(%s) failed: %s", name, err)
        # The republish lands one machine poll cycle (~1.3 s) after the
        # queued GET drains — and the trigger+GET may sit behind several
        # other commands first. Observed 2026-10-30: 'coffee' failed with
        # "no recipe" while d040_1_rec_* republished ~5 s later, still
        # behind the settings-lane backlog on an empty post-restart
        # store. Budget the full response_timeout like _confirm_param_write
        # and re-request every ~3 s to cover a dropped GET.
        deadline = time.monotonic() + self.response_timeout
        last_request = time.monotonic()
        while True:
            value = self._named_value(name)
            raw = self._as_blob_bytes(value) if value is not None else None
            if raw is not None:
                recipe = self._brew_recipe_bytes(raw)
                if recipe is not None:
                    return recipe
            now = time.monotonic()
            if now >= deadline:
                return None
            if request is not None and now - last_request >= 3.0:
                last_request = now
                try:
                    request(name)
                except (OSError, ValueError, RuntimeError) as err:
                    logger.debug("request_property(%s) failed: %s", name, err)
            time.sleep(self.response_poll)

    # --- brew / power ---
    def brew(
        self,
        beverage_id: int,
        recipe: bytes | None = None,
        profile_slot: int | None = None,
        accessory: int = 0,
    ) -> Any:
        """Start brewing ``beverage_id``.

        ``profile_slot=None`` resolves to the last 0xA9-acked profile
        (``current_profile``); ``recipe=None`` fetches the profile-scoped
        recipe bytes from the republished ``a6f0`` property — both per
        the FR-001 coherence rule (frame names its profile, recipe must
        be that profile's). :meth:`do` uses the same live-recipe path,
        taking beverage id, action and trailer from the stored frame and
        falling back to the captured recipe when the machine does not
        republish one.
        """
        slot = self.current_profile or 1 if profile_slot is None else profile_slot
        if recipe is None:
            recipe = self._fetch_profile_recipe(slot, beverage_id)
            if recipe is None:
                raise ConnectionError(
                    f"no profile-scoped recipe available for "
                    f"(profile={slot}, beverage={beverage_id})"
                )
        frame = ecam_builder.build_brew(
            beverage_id,
            ecam_builder.BrewAction.START,
            recipe,
            self._profile(),
            profile_slot=slot,
            accessory=accessory,
        )
        return self._send_gated(frame)

    def stop_brew(self) -> Any:
        """Stop the running brew (the verified ``83 f0 .. 02`` frame)."""
        frame = ecam_builder.build_brew(
            0x10,
            ecam_builder.BrewAction.STOP,
            ecam_builder.DEFAULT_RECIPE,
            self._profile(),
            profile_slot=1,
            accessory=2,
        )
        return self._send_gated(frame)

    def wake(self) -> Any:
        """Wake the machine (``84 0f 02 01``)."""
        return self._send_gated(
            ecam_builder.build_power(ecam_builder.PowerCommand.WAKE, self._profile())
        )

    def standby(self) -> Any:
        """Send the machine to standby (``84 0f 01 01``)."""
        return self._send_gated(
            ecam_builder.build_power(ecam_builder.PowerCommand.STANDBY, self._profile())
        )

    def session_refresh(self) -> Any:
        """Refresh the write-gate session.

        Non-striker machines re-announce ``device_connected``; striker
        machines send the ``84 0f 03 02`` refresh frame.
        """
        if self._profile().session == ecam_profiles_mod.SESSION_DEVICE_CONNECTED_TS:
            self._announce_session()
            self._session().announced_at = time.time()
            return None
        return self._send_gated(
            ecam_builder.build_power(
                ecam_builder.PowerCommand.SESSION_REFRESH, self._profile()
            )
        )

    # --- statistics ---
    def get_statistics(self) -> ecam_statistics.StatisticsReport:
        """Fetch the machine's usage statistics.

        ``statistics_source == "native"`` maps page the ``0xA2`` table via
        the response mailbox; ``"cloud_counters"`` maps resolve logical
        counters against the transport's property snapshot.
        """
        if self.statistics_source == "cloud_counters":
            props = self._properties_by_name()
            report = ecam_statistics.interpret(
                [], source="cloud_counters", complete=True
            )
            report.cloud_counters = ecam_statistics.resolve_cloud_counters(props)
            report.breakdowns = ecam_statistics.resolve_cloud_breakdowns(props)
            return report

        if getattr(self.transport, "pop_response", None) is None:
            raise NotImplementedError(
                "native 0xA2 statistics require a transport with "
                "pop_response (LAN); this transport does not provide one"
            )
        entries: dict[int, int] = {}
        start, count = 0, 10
        while True:
            self.send_frame(
                ecam_builder.build_statistics_page(start, count, self._profile())
            )
            frame = self._wait_answer(ecam_builder.REQ_STATISTICS)
            if frame is None:
                # A timeout is never end-of-table — retry with count-1.
                if count <= 1:
                    raise TimeoutError(f"statistics paging stalled at start_id={start}")
                count -= 1
                continue
            page = ecam_answers.decode_a2_page(frame)
            if not page:
                break
            for stat_id, value in page:
                entries[stat_id] = value
            if len(page) < count:
                break
            start = page[-1][0] + 1
        return ecam_statistics.interpret(
            sorted(entries.items()), source="native", complete=True
        )

    # --- profiles ---
    def get_profiles(self) -> list[ecam_profile_parser.ProfileSlot]:
        """Read the occupied profile slots (``a4f0`` blobs, profile-scoped)."""
        names = [name for name in self.catalog_datapoints if "_profiles_" in name]
        slots: dict[int, ecam_profile_parser.ProfileSlot] = {}
        if names:
            props = self._collect_named_properties(names)
            for value in props.values():
                raw = self._as_blob_bytes(value)
                if raw is None:
                    continue
                for slot in ecam_profile_parser.parse_profile_names(
                    raw, self._profile()
                ):
                    slots[slot.index] = slot
        if not slots:
            # Fallback: 0xA4 frame read on the response mailbox.
            self.send_frame(
                ecam_builder.build_profile_names(1, self.n_profiles, self._profile())
            )
            frame = self._wait_answer(ecam_builder.REQ_PROFILE_NAMES)
            if frame is not None:
                for slot in ecam_profile_parser.parse_profile_names(
                    frame, self._profile()
                ):
                    slots[slot.index] = slot
        return [slots[i] for i in sorted(slots)]

    def select_profile(self, index: int) -> bool:
        """Switch the active profile (session-gated ``0xA9``).

        Wakes a standby machine first. On striker a matching positive
        ack is required (the ``a9`` ack path updates ``EcamMachine.B``).
        On non-striker the app treats ``0xA9`` as fire-and-retry — its
        connect storm sends it ~11× and the 2026-10-14 cloud capture
        shows zero acks, while the machine still applies the selection
        (DECODED.md "Current profile — never fetched on non-striker").
        There is no machine-side read-back on Soul: after a nack-free
        send the asserted index becomes the truth, matching the app's
        ``UserData.USER_PROFILE_SELECTED`` model.
        """
        if not 1 <= index <= max(self.n_profiles, 1):
            raise ValueError(f"profile index {index} out of range 1..{self.n_profiles}")
        status = None
        if hasattr(self.transport, "get_monitor"):
            try:
                status = getattr(self.get_monitor(), "status_name", None)
            except (OSError, ValueError, RuntimeError) as err:
                logger.debug("monitor status probe failed: %s", err)
        if status in ("standby", "sleeping", "off"):
            self.wake()
        striker = (
            self._profile().session != ecam_profiles_mod.SESSION_DEVICE_CONNECTED_TS
        )
        attempts = 1 if striker else 3
        for attempt in range(attempts):
            self._send_gated(ecam_builder.build_profile_select(index, self._profile()))
            try:
                frame = self._wait_answer(
                    ecam_builder.REQ_PROFILE_SELECT,
                    timeout=None if striker else min(self.response_timeout, 4.0),
                )
            except TimeoutError:
                return False
            if frame is None:
                continue  # expected on non-striker — see docstring
            try:
                profile, ok = ecam_answers.parse_a9_ack(frame)
            except ValueError:
                continue
            if ok and profile == index:
                self.current_profile = index
                return True
            if not ok:
                return False  # explicit nack — resending is pointless
        if striker:
            return False
        self.current_profile = index
        return True

    @property
    def selected_profile(self) -> int | None:
        """The last acknowledged ``0xA9`` profile index, if any."""
        return self.current_profile or None

    #: Param behind striker ``d286_mach_sett_profile`` — on striker it is
    #: the live selected-profile register (``f2()`` asserts the envelope
    #: param id). On the Soul it answers ``0x95`` but does NOT track
    #: ``0xA9`` selections — verified 2026-10-25: display on profile 2
    #: while the register still read 0. On non-striker it is only a
    #: boot-state hint, never truth (DECODED.md §335).
    PARAM_CURRENT_PROFILE: ClassVar[int] = 0x03EE

    def refresh_current_profile(self, *, force: bool = False) -> int | None:
        """Refresh :attr:`current_profile` — striker reads ``0x03EE``,
        non-striker keeps the last-asserted profile.

        On striker the register is read back whenever it is stale.
        On non-striker the app never asks the machine for the profile —
        ``0xA9`` is told-never-asked and ``0x03EE`` does not follow
        panel/app selects, so the read only seeds the initial state
        (when ``current_profile`` is still 0/unknown) and can never
        overwrite a selection this process asserted or confirmed.
        Throttled to :attr:`profile_poll_seconds`; the read runs under
        the session gate (§14.8).
        """
        striker = (
            self._profile().session != ecam_profiles_mod.SESSION_DEVICE_CONNECTED_TS
        )
        if not striker and self.current_profile:
            return self.current_profile  # last-asserted is the truth on Soul
        now = time.monotonic()
        if (
            not force
            and self._profile_polled_at
            and now - self._profile_polled_at < self.profile_poll_seconds
        ):
            return None
        if getattr(self.transport, "pop_response", None) is None:
            return None  # transports without a mailbox cannot answer 0x95
        self._profile_polled_at = now
        try:
            self._session().ensure_fresh()
        except (ConnectionError, OSError, RuntimeError) as err:
            logger.debug("session announce before profile read failed: %s", err)
        frame = self._read_param_frame(self.PARAM_CURRENT_PROFILE)
        if frame is None:
            return None
        raw = ecam_settings.parse_settings(frame).get(self.PARAM_CURRENT_PROFILE)
        if raw is None:
            return None
        if not striker and self.current_profile:
            return self.current_profile  # register can lag panel selects
        self.current_profile = raw + 1
        return self.current_profile

    # --- settings ---
    def get_settings(
        self,
        *,
        hints: Collection[str] | None = None,
        keys: Collection[str] | None = None,
    ) -> dict[str, int | None]:
        """Read writable settings as ``{key: raw_option_index}``.

        Republished ``dNNN`` ``95``-blobs are read first; settings without
        a stored blob fall back to a ``0x95`` mailbox read. Capability-
        gated settings the map marks unsupported are omitted.

        ``hints`` narrows the re-requested datapoints to names containing
        one of the hint substrings (``datapoint_hint`` semantics) — the
        HA coordinator passes the entity-backed hints so the periodic
        refresh queues ~3 GETs instead of the full six (observed
        2026-10-30: the full lane burst-starved interactive commands on
        the one-command-per-poll drain). ``keys`` narrows the evaluated
        settings to the given map keys — entities refreshing a single
        setting pass their own key so sibling keys don't each sink into
        a seconds-long ``0x95`` fallback.
        """
        names = self.settings_datapoints
        if hints is not None:
            names = [n for n in names if any(h in n for h in hints)]
        merged: dict[int, int] = {}
        if names:
            props = self._collect_named_properties(names)
            for value in props.values():
                raw = self._as_blob_bytes(value)
                if raw is not None:
                    merged.update(ecam_settings.parse_settings(raw))
        out: dict[str, int | None] = {}
        for key, setting in ecam_settings.SETTING_OPTION_MAPS.items():
            if keys is not None and key not in keys:
                continue
            if self.capabilities and not self.capabilities.get(
                setting.capability, True
            ):
                continue
            raw = merged.get(setting.param_id)
            if raw is None:
                frame = self._read_param_frame(setting.param_id)
                if frame is not None:
                    raw = ecam_settings.parse_settings(frame).get(setting.param_id)
            out[key] = raw
        return out

    def _machine_in_standby(self) -> bool:
        """True when the monitor reports a standby-like status."""
        if not hasattr(self.transport, "get_monitor"):
            return False
        try:
            status = getattr(self.get_monitor(), "status_name", None)
        except (OSError, ValueError, RuntimeError) as err:
            logger.debug("monitor status probe failed: %s", err)
            return False
        return status in _STANDBY_STATUS_NAMES

    def set_setting(self, key: str, option_index: int) -> bool:
        """Write a writable setting (session-gated ``0x90`` + ack/read-back).

        Follows protocol.md §14.5: ``device_connected`` announce (via
        :meth:`_send_gated`) → ``0x90``. §14.5 woke the machine for its test;
        by product decision this library never wakes it — configuration
        writes are attempted in standby as well: the machine processes
        ``data_request`` while ``in_standby`` (§14.8 — ``0xA2`` reads were
        answered there) and the official app exposes config changes in
        standby (only profile selection is gated). §14.5 confirmed the
        commits on the machine's display and recorded no ack for them, so
        the ack is treated as optional: the write counts as committed when
        a read-back — mailbox answer or the republished ``dNNN`` backing
        property — shows the new value. An unconfirmed write is retried
        once with a fresh session announce — the machine rate-limits and
        drops frames under load (observed 2026-10-14).

        Returns ``True`` when the write is confirmed and ``False`` when the
        machine nacked it or reports a different value. Raises
        ``ValueError`` for unknown keys/options and ``TimeoutError`` when
        neither an ack nor a read-back arrives.
        """
        setting = ecam_settings.SETTING_OPTION_MAPS.get(key)
        if setting is None:
            raise ValueError(f"unknown setting {key!r}")
        if option_index not in setting.options:
            raise ValueError(
                f"option {option_index} out of range for {key!r} "
                f"(allowed: {sorted(setting.options)})"
            )
        return self._write_setting_confirmed(
            key,
            ecam_builder.build_write_param(
                setting.param_id, option_index, self._profile()
            ),
            setting.param_id,
            option_index,
            setting.datapoint_hint,
        )

    def _confirm_param_write(
        self, key: str, param_id: int, expected: int, datapoint_hint: str
    ) -> str:
        """Post-write confirmation shared by option and bit writes.

        Polls the mailbox (``0x90`` ack + the ``0x95`` read-back already on
        the wire) and re-requests the republished ``dNNN`` backing property
        until the new value shows or the timeout expires. Returns
        ``"confirmed"``, ``"rejected"`` (explicit nack) or ``"stale"``
        (a different value seen — worth a retry)."""

        datapoint = next(
            (
                name
                for name in self.settings_datapoints or []
                if datapoint_hint and datapoint_hint in name
            ),
            None,
        )
        request = getattr(self.transport, "request_property", None)
        deadline = time.monotonic() + self.write_confirm_seconds
        last_request: float | None = None
        acked = False
        seen: int | None = None
        while True:
            frame = self._pop_response_frame()
            if frame is not None:
                if ecam_answers.is_sync_timeout(frame):
                    raise TimeoutError(f"machine aborted the write of {key!r} (0xE1)")
                try:
                    answer = ecam_answers.answer_id(frame)
                except ValueError:
                    answer = None
                if answer == ecam_builder.REQ_WRITE_PARAM:
                    param, ok = ecam_answers.parse_write_ack(frame)
                    if param == param_id:
                        if not ok:
                            self._session().invalidate()
                            return "rejected"
                        acked = True
                elif answer == ecam_builder.REQ_READ_PARAM:
                    value = ecam_settings.parse_settings(frame).get(param_id)
                    seen = value if value is not None else seen
                if seen == expected:
                    return "confirmed"
                continue
            now = time.monotonic()
            if datapoint and request is not None:
                if last_request is None or now - last_request >= 3.0:
                    last_request = now
                    try:
                        request(datapoint)
                    except (OSError, ValueError, RuntimeError) as err:
                        logger.debug("request_property(%s) failed: %s", datapoint, err)
                raw = self._as_blob_bytes(self._named_value(datapoint))
                value = (
                    ecam_settings.parse_settings(raw).get(param_id)
                    if raw is not None
                    else None
                )
                seen = value if value is not None else seen
                if seen == expected:
                    return "confirmed"
            if now >= deadline:
                break
            time.sleep(self.response_poll)
        logger.debug(
            "param write (%s=%s) unconfirmed: acked=%s last_seen=%s",
            key,
            expected,
            acked,
            seen,
        )
        if seen is None:
            if acked:
                return "confirmed"
            raise TimeoutError(
                f"machine neither acknowledged nor republished setting {key!r}"
            )
        return "stale"

    def _read_param_value(self, param_id: int, datapoint_hint: str = "") -> int | None:
        """Current u32 of a parameter — republished ``dNNN`` first, then a
        ``0x95`` mailbox read.

        The named property is re-requested before reading so a stale
        last-published value is less likely to be used as a merge base.
        """
        datapoint = next(
            (
                name
                for name in self.settings_datapoints or []
                if datapoint_hint and datapoint_hint in name
            ),
            None,
        )
        if datapoint:
            request = getattr(self.transport, "request_property", None)
            if request is not None:
                try:
                    request(datapoint)
                except (OSError, ValueError, RuntimeError) as err:
                    logger.debug("request_property(%s) failed: %s", datapoint, err)
            # The machine serves ~one queued GET per poll cycle and the
            # answer arrives as a republish of the property itself
            # (protocol.md §13 — `0x95` replies for settings don't use the
            # data_response mailbox), so poll the store for a few seconds.
            deadline = time.monotonic() + min(self.response_timeout, 4.0)
            while True:
                raw = self._as_blob_bytes(self._named_value(datapoint))
                if raw is not None:
                    value = ecam_settings.parse_settings(raw).get(param_id)
                    if value is not None:
                        return value
                if time.monotonic() >= deadline:
                    break
                time.sleep(self.response_poll)
        frame = self._read_param_frame(param_id)
        if frame is None:
            return None
        return ecam_settings.parse_settings(frame).get(param_id)

    def _read_param_frame(self, param_id: int, *, retries: int = 1) -> bytes | None:
        """Send a ``0x95`` param read and wait for its mailbox answer.

        The machine drops ``data_request`` frames when busy or
        rate-limited (observed 2026-10-14 — whole read batches went
        unanswered), so an unanswered read is retried ``retries`` times
        before giving up. Session commands appear to be processed only
        while a session is live (§14.8 — reads went silent on dead
        sessions while ``0xA2`` answered), so the gate is refreshed
        first; an announce failure is tolerated.
        """
        try:
            self._session().ensure_fresh()
        except (ConnectionError, OSError, RuntimeError) as err:
            logger.debug("session announce before param read failed: %s", err)
        frame: bytes | None = None
        for _ in range(retries + 1):
            self.send_frame(ecam_builder.build_read_param(param_id, self._profile()))
            frame = self._wait_answer(ecam_builder.REQ_READ_PARAM)
            if frame is not None:
                break
        return frame

    def get_setting_bits(self, *, refresh: bool = True) -> dict[str, bool | None]:
        """Read boolean bit settings as ``{key: on/off}`` (param ``0x003F``).

        Same read strategy as :meth:`get_settings`: republished ``dNNN``
        ``95``-blobs first, ``0x95`` mailbox read for params without one.
        Capability-gated settings the map marks unsupported are omitted;
        unreadable params yield ``None`` — never a fabricated ``False``.

        ``refresh=False`` harvests only what is already in the property
        store — no ``request_property`` GETs and no ``0x95`` fallback.
        Used by the HA coordinator's tracking poll: ``get_settings``
        refreshes the store first, then this reads param ``0x003F`` off
        it without queuing a second GET round (protocol.md §14.11 —
        republished ``dNNN`` is the WiFi read channel).
        """
        merged: dict[int, int] = {}
        if self.settings_datapoints:
            if refresh:
                props = self._collect_named_properties(self.settings_datapoints)
            else:
                props = {
                    name: value
                    for name in self.settings_datapoints
                    if (value := self._named_value(name)) is not None
                }
            for value in props.values():
                raw = self._as_blob_bytes(value)
                if raw is not None:
                    merged.update(ecam_settings.parse_settings(raw))
        needed: dict[int, list[str]] = {}
        for key, setting in ecam_settings.SETTING_BIT_MAPS.items():
            if self.capabilities and not self.capabilities.get(
                setting.capability, True
            ):
                continue
            needed.setdefault(setting.param_id, []).append(key)
        out: dict[str, bool | None] = {}
        for param_id, keys in needed.items():
            raw = merged.get(param_id)
            if raw is None and refresh:
                frame = self._read_param_frame(param_id)
                if frame is not None:
                    raw = ecam_settings.parse_settings(frame).get(param_id)
            bits = ecam_settings.settings_bits(param_id, raw) if raw is not None else {}
            for key in keys:
                out[key] = bits.get(key)
        return out

    def set_setting_bit(self, key: str, on: bool) -> bool:
        """Flip a boolean flag inside a shared param word (``0x90`` write).

        Reads the param's current u32 first — the flag is merged so
        unrelated bits survive (the app's ``f0`` write does exactly this;
        protocol.md §14.8). Standby does not block configuration writes:
        the machine processes ``data_request`` while ``in_standby`` and
        the app exposes these toggles there. An unreadable base value
        aborts with ``TimeoutError`` — a blind write could clobber other
        settings. Returns ``True`` on confirmed state, ``False`` when the
        machine reports a different value.
        """
        setting = ecam_settings.SETTING_BIT_MAPS.get(key)
        if setting is None:
            raise ValueError(f"unknown bit setting {key!r}")
        raw = self._read_param_value(setting.param_id, setting.datapoint_hint)
        if raw is None:
            raise TimeoutError(
                f"could not read the current value of param "
                f"0x{setting.param_id:04x}; refusing a blind write of {key!r}"
            )
        new = ecam_settings.merge_setting_bit(raw, setting, on)
        if new == raw:
            return True  # already in the desired state — no wire traffic
        return self._write_setting_confirmed(
            key,
            ecam_builder.build_write_param(setting.param_id, new, self._profile()),
            setting.param_id,
            new,
            setting.datapoint_hint,
        )

    def _write_setting_confirmed(
        self,
        key: str,
        frame: bytes,
        param_id: int,
        expected: int,
        datapoint_hint: str,
    ) -> bool:
        """Send a ``0x90`` param write and confirm, retrying once.

        The machine drops frames when busy/rate-limited (observed
        2026-10-14 — a write was acked ``ok`` yet the param republished
        the old value). An unconfirmed first attempt re-announces the
        session and resends the same write — idempotent, the merged
        value is unchanged. The read-back follows each write.
        """
        for attempt in range(2):
            self._send_gated(frame)
            self.send_frame(ecam_builder.build_read_param(param_id, self._profile()))
            try:
                result = self._confirm_param_write(
                    key, param_id, expected, datapoint_hint
                )
            except TimeoutError:
                if attempt:
                    raise
                result = "stale"
            if result == "confirmed":
                return True
            if result == "rejected":
                return False  # an explicit nack — resending is pointless
            # "stale": the write was likely ignored — re-announce and
            # resend (idempotent, the merged value is unchanged).
            if attempt == 0:
                self._session().invalidate()
            else:
                return False
        return False

    # --- catalogue ---
    def declared_catalog(self) -> ecam_catalog.RecipeCatalogue:
        """The advisory catalogue declared by the app's model table.

        Uses ``model_ref.recipe_declaration`` (an ``appModelId`` whose
        table entry stands in for this model) or else
        ``model_ref.appModelId``. Empty when the table declares no recipes.
        """
        model = self.model_ref.get("recipe_declaration") or self.model_ref.get(
            "appModelId"
        )
        return ecam_catalog.build_declared_catalog(model)

    def read_catalog(self, *, settle: float = 0.5) -> ecam_catalog.RecipeCatalogue:
        """Build the recipe catalogue.

        Machines that publish recipe datapoints are read per-name and
        parsed; maps without ``catalog_datapoints`` (striker models) get
        the model-table declaration instead.
        """
        if not self.catalog_datapoints:
            return self.declared_catalog()
        props = self._collect_named_properties(self.catalog_datapoints, settle=settle)
        return ecam_catalog.build_catalog(
            {name: {"value": value} for name, value in props.items()}
        )

    @staticmethod
    def _as_blob_bytes(value: Any) -> bytes | None:
        """Decode a property value (b64 str or bytes) to raw blob bytes."""
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            try:
                return b64decode("".join(value.split()))
            except (ValueError, TypeError):
                return None
        return None
