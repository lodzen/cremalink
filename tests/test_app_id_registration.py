"""App-id registration for striker (cloud) machines.

The cloud copy of ``app_id`` can keep showing our id after the machine has
dropped the registration (its reset to ``0`` never reached the cloud).
These tests pin that the device registers again instead of only refreshing,
waits for the machine to confirm, and falls back sensibly when it does not.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

import cremalink
from cremalink.core.binary import hex_to_signed_decimal
from cremalink.domain import device as device_mod
from cremalink.domain.device import APP_ID_HEX, Device

OWN = hex_to_signed_decimal(APP_ID_HEX)
FOREIGN_HEX = "12345678"
FOREIGN = hex_to_signed_decimal(FOREIGN_HEX)
STRIKER_MAP = Path(cremalink.__file__).parent / "devices" / "ECAM452.json"
REGISTER = "app_device_connected"


class FakeCloud:
    """Minimal cloud transport: properties plus a machine that may answer."""

    def __init__(self, app_id: str | None = "0", *, machine_answers: bool = True):
        self.props: dict[str, dict] = {
            "d302_monitor_machine": {"value": "frame", "data_updated_at": "m0"},
        }
        if app_id is not None:
            self.props["app_id"] = {"value": app_id, "data_updated_at": "a0"}
        self.machine_answers = machine_answers
        self.sent: list[tuple[str, bytes]] = []
        self._tick = 0

    def get_property(self, name):
        entry = self.props.get(name)
        return dict(entry) if entry else None

    def send_command(self, command, alternative_property=None):
        prop = alternative_property or "app_data_request"
        self.sent.append((prop, base64.b64decode(command)))
        if prop == REGISTER and self.machine_answers:
            self._tick += 1
            self.props["app_id"] = {"value": OWN, "data_updated_at": f"a{self._tick}"}
            self.props["d302_monitor_machine"]["data_updated_at"] = f"m{self._tick}"
        return {}

    # helpers for assertions
    def registrations(self) -> int:
        return sum(1 for prop, _ in self.sent if prop == REGISTER)

    def last_trailer(self) -> str:
        return self.sent[-1][1][-4:].hex().upper()


def _device(cloud: FakeCloud) -> Device:
    dev = Device.from_map(cloud, device_map_path=str(STRIKER_MAP))
    dev.app_id_confirm_seconds = 0.05
    dev.app_id_confirm_own_seconds = 0.05
    dev.app_id_confirm_poll = 0.0
    return dev


def test_registers_when_cloud_shows_zero():
    cloud = FakeCloud("0")
    dev = _device(cloud)

    dev.send_command("0d07840f02015512")

    assert cloud.registrations() == 1
    assert cloud.sent[0][0] == REGISTER
    assert cloud.last_trailer() == APP_ID_HEX


def test_stale_own_id_in_cloud_registers_again_instead_of_only_refreshing():
    # Cloud still shows our id, but this instance never saw it confirmed:
    # the machine may long have dropped it. Must register, not just refresh.
    cloud = FakeCloud(OWN)
    dev = _device(cloud)

    dev.send_command("0d07840f02015512")

    assert cloud.registrations() == 1
    assert cloud.last_trailer() == APP_ID_HEX


def test_refreshes_inside_window_after_confirmed_registration():
    cloud = FakeCloud("0")
    dev = _device(cloud)
    dev.send_command("0d07840f02015512")
    sent_before = len(cloud.sent)

    dev.send_command("0d07840f02015512")

    new = cloud.sent[sent_before:]
    assert all(prop != REGISTER for prop, _ in new)
    # Within app_id_refresh_min_seconds the command itself keeps the
    # registration alive: no extra refresh frame.
    assert not any(p.startswith(bytes.fromhex("0d07840f0302")) for _, p in new)


def test_sends_explicit_refresh_after_min_interval(monkeypatch):
    cloud = FakeCloud("0")
    dev = _device(cloud)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(device_mod.time, "time", lambda: clock[0])
    dev.send_command("0d07840f02015512")
    clock[0] += dev.app_id_refresh_min_seconds + 1  # still inside 240 s
    sent_before = len(cloud.sent)

    dev.send_command("0d07840f02015512")

    new = cloud.sent[sent_before:]
    assert all(prop != REGISTER for prop, _ in new)
    assert new[0][1].startswith(bytes.fromhex("0d07840f0302"))  # refresh frame


def test_parallel_callers_register_once():
    import threading

    cloud = FakeCloud("0")
    dev = _device(cloud)
    threads = [
        threading.Thread(target=dev.send_command, args=("0d07840f02015512",))
        for _ in range(4)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert cloud.registrations() == 1


def test_registers_again_once_the_refresh_window_has_passed(monkeypatch):
    cloud = FakeCloud("0")
    dev = _device(cloud)
    clock = [1_800_000_000.0]  # realistic 4-byte unix time
    monkeypatch.setattr(device_mod.time, "time", lambda: clock[0])
    dev.send_command("0d07840f02015512")

    clock[0] += dev.app_id_refresh_seconds + 1
    dev.send_command("0d07840f02015512")

    assert cloud.registrations() == 2


def test_own_id_without_machine_echo_renews_and_uses_own_id():
    # A live registration with an unchanged id republishes nothing (seen on
    # real hardware). The renewal still goes out and our id is used.
    cloud = FakeCloud(OWN, machine_answers=False)
    dev = _device(cloud)
    dev.app_id_confirm_seconds = 5.0  # must not be used for this case

    dev.send_command("0d07840f02015512")

    assert cloud.registrations() == 1
    assert cloud.last_trailer() == APP_ID_HEX


def test_unconfirmed_registration_falls_back_to_foreign_id():
    cloud = FakeCloud(FOREIGN, machine_answers=False)
    dev = _device(cloud)

    dev.send_command("0d07840f02015512")

    assert cloud.registrations() == 1
    assert cloud.last_trailer() == FOREIGN_HEX


def test_raises_when_machine_never_confirms_and_no_id_is_set():
    cloud = FakeCloud("0", machine_answers=False)
    dev = _device(cloud)

    with pytest.raises(ConnectionError):
        dev.send_command("0d07840f02015512")


def test_missing_app_id_property_registers_instead_of_failing():
    cloud = FakeCloud(None)
    dev = _device(cloud)

    dev.send_command("0d07840f02015512")

    assert cloud.registrations() == 1
    assert cloud.last_trailer() == APP_ID_HEX


def test_striker_wake_registers_on_stale_own_id():
    cloud = FakeCloud(OWN)
    dev = _device(cloud)

    dev.do("wakeup")

    assert cloud.sent[0][0] == REGISTER
    frames = [payload for prop, payload in cloud.sent if prop != REGISTER]
    assert any(p.startswith(bytes.fromhex("0d06e8f0")) for p in frames)  # handshake
    assert frames[-1].startswith(bytes.fromhex("0d07840f0201"))  # wake
    assert cloud.last_trailer() == APP_ID_HEX


class FakeLan(FakeCloud):
    """LAN transport: ``get_property`` returns the bare value, no timestamps."""

    def __init__(self, app_id: str | None = "0", *, machine_answers: bool = True):
        super().__init__(app_id, machine_answers=machine_answers)
        self.reads = 0

    def get_property(self, name):
        self.reads += 1
        entry = self.props.get(name)
        if entry is None:
            raise OSError("404 Property not found")
        return entry["value"]


def test_lan_bare_values_confirm_through_the_value_change():
    lan = FakeLan("0")
    dev = _device(lan)

    dev.send_command("0d07840f02015512")
    dev.send_command("0d07840f02015512")

    assert lan.registrations() == 1
    assert lan.last_trailer() == APP_ID_HEX


def test_lan_own_id_without_timestamps_uses_own_id():
    lan = FakeLan(OWN, machine_answers=False)
    dev = _device(lan)

    dev.send_command("0d07840f02015512")

    assert lan.registrations() == 1
    assert lan.last_trailer() == APP_ID_HEX


def test_unconfirmed_registration_checks_only_a_few_times(monkeypatch):
    lan = FakeLan("0", machine_answers=False)
    dev = Device.from_map(lan, device_map_path=str(STRIKER_MAP))
    clock = [1000.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(device_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(device_mod.time, "sleep", sleep)

    with pytest.raises(ConnectionError):
        dev.send_command("0d07840f02015512")

    # 10 s window with checks every 3 s: a handful of reads, not one per second.
    assert lan.reads <= 10
