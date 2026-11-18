"""ECAM452 device-map hygiene: only entities the Eletta Explore can back."""

import json
from pathlib import Path

from cremalink.domain.device import Device
from cremalink.ecam.monitor_bits import ALARM_BITS, SWITCH_BITS

DEVICES = Path(__file__).resolve().parents[1] / "cremalink" / "devices"
ECAM452 = json.loads((DEVICES / "ECAM452.json").read_text())


def test_monitor_bit_policy_names_real_bits():
    known = {b.key for b in SWITCH_BITS.values()} | {b.key for b in ALARM_BITS.values()}
    policy = ECAM452["monitor_bits"]
    listed = policy["exclude"] + policy["disabled_by_default"]
    assert set(listed) <= known
    assert not set(policy["exclude"]) & set(policy["disabled_by_default"])


def test_monitor_bit_policy_reaches_the_device():
    class _T:
        def set_mappings(self, *_a):
            pass

    dev = Device.from_map(_T(), device_map_path=str(DEVICES / "ECAM452.json"))
    assert "choco_tank_present" in dev.monitor_bits["exclude"]


def test_statistics_list_only_counters_an_eletta_publishes():
    # Not published by an Eletta Explore (verified against a live machine).
    missing = {
        "d703_tot_bev_w",
        "d727_id24_cortado",
        "d728_id25_long_black",
        "d729_id26_travel_mug",
        "d825_descale_status",
    }
    assert not missing & set(ECAM452["statistics_datapoints"])
