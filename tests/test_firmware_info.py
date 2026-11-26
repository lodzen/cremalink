"""Firmware versions from the cloud: machine datapoint + Wi-Fi module."""

from __future__ import annotations

from pathlib import Path

from cremalink.domain.device import Device

DEVICES = Path(__file__).resolve().parents[1] / "cremalink" / "devices"


class FakeCloud:
    sw_version = "ADA 1.6 esp-idf-v3.3.1 2020-05-20 21:37:21 667ab48"
    module_updated_at = "2023-02-07T19:08:11Z"

    def __init__(self, props):
        self.props = props

    def get_property(self, name):
        value = self.props.get(name)
        return {"name": name, "value": value} if value is not None else None


class FakeLan:
    def get_property(self, name):
        raise OSError("404 Property not found")


class FakeLanRepublish:
    """LAN transport: properties miss until the machine republishes them.

    Mirrors the Soul's behaviour — a ``request_property`` GET makes the
    machine republish the datapoint into the server's store
    (captures/lan_software_version.json).
    """

    def __init__(self, props):
        self.props = dict(props)
        self.pending: dict[str, str] = {}
        self.requested: list[str] = []

    def request_property(self, name):
        self.requested.append(name)
        if name in self.pending:
            # The machine's answer lands asynchronously; the fake applies
            # it to the store on the next read pass.
            self.props[name] = self.pending.pop(name)

    def get_property(self, name):
        value = self.props.get(name)
        if value is None:
            raise OSError("404 Property not found")
        return {"name": name, "value": value}


def _device(transport, device_map="ECAM452.json"):
    return Device.from_map(transport, device_map_path=str(DEVICES / device_map))


def test_machine_and_wifi_module_versions():
    dev = _device(
        FakeCloud({"software_version": "Striker_cb_demo 1.1.0   Oct 18 2022 10:44:21"})
    )

    assert dev.firmware_info() == {
        "machine": "Striker_cb_demo 1.1.0 Oct 18 2022 10:44:21",
        "wifi_module": "ADA 1.6 esp-idf-v3.3.1 2020-05-20 21:37:21 667ab48",
        "wifi_module_updated_at": "2023-02-07T19:08:11Z",
    }


def test_unknown_values_are_left_out():
    assert _device(FakeLan()).firmware_info() == {}


def test_lan_republished_software_version():
    """LAN machines republish ``software_version`` on request (Soul)."""
    transport = FakeLanRepublish(
        {"d302_monitor": "..."},
    )
    transport.pending["software_version"] = "Millcore_demo 2.0.0 Jun 18 2020 15:41:06"
    dev = _device(transport, "ECAM612.json")

    assert dev.firmware_info() == {
        "machine": "Millcore_demo 2.0.0 Jun 18 2020 15:41:06",
    }
    assert transport.requested == ["software_version"]


def test_model_name_comes_from_the_device_map():
    dev = _device(FakeLan(), "ECAM612.json")
    assert dev.model_name == "PrimaDonna Soul"
