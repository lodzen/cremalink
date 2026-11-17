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


def _device(transport):
    return Device.from_map(transport, device_map_path=str(DEVICES / "ECAM452.json"))


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
