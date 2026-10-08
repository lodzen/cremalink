"""Device-level ECAM flows: session gate, brew/do, profiles, settings."""

import base64
import json
import time
from pathlib import Path

import pytest
from cremalink.core.binary import crc16_ccitt
from cremalink.domain.device import Device

DEVICES = Path(__file__).resolve().parents[1] / "cremalink" / "devices"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ecam"


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _blob(name):
    return base64.b64decode("".join(_fixture(name)["data"]["value"].split()))


CAPTURED_FRAMES = _fixture("command_frames")


def _answer(content: bytes) -> bytes:
    """Build a 0xD0 answer frame for the mailbox."""
    frame = bytes([0xD0, len(content) + 3]) + content
    return frame + crc16_ccitt(frame)


class FakeTransport:
    """Minimal LAN transport: records sends, scripts mailbox/properties."""

    def __init__(self, *, properties=None, responses=None):
        self.sent = []  # decoded wire payloads (frame + ts)
        self.properties = dict(properties or {})
        self.responses = list(responses or [])
        self.written_properties = []
        self.requested_properties = []

    def send_command(self, command, alternative_property=None):
        self.sent.append((base64.b64decode(command), alternative_property))
        return {}

    def write_property(self, name, value):
        self.written_properties.append((name, value))

    def request_property(self, name):
        self.requested_properties.append(name)

    def get_property(self, name):
        return self.properties.get(name)

    def pop_response(self):
        return self.responses.pop(0) if self.responses else None

    def configure(self):
        pass

    def get_properties(self):
        return self.properties

    def refresh_monitor(self):
        pass

    def health(self):
        return {"ok": True}


def _device(map_name="ECAM610.json", **kwargs):
    transport = kwargs.pop("transport", FakeTransport())
    device = Device.from_map(
        transport=transport, device_map_path=str(DEVICES / map_name), **kwargs
    )
    return device


def _sent_frame(device, index=-1):
    return device.transport.sent[index][0]


def _acked_device(**kwargs):
    """Device whose transport answers 0x90 writes + 0x95 read-backs."""
    ack = _answer(bytes([0x90, 0xF0, 0x00, 0x3E, 0x00]))
    readback = _answer(bytes([0x95, 0x0F, 0x00, 0x3E]) + (2).to_bytes(4, "big"))
    device = _device(response_poll=0, **kwargs)
    device.transport.responses.extend([ack, readback])
    return device


class TestSessionGate:
    def test_first_write_announces_device_connected(self):
        device = _acked_device()
        device.set_setting("auto_off", 2)
        writes = device.transport.written_properties
        assert writes and writes[0][0] == "device_connected"
        assert 0 < time.time() - writes[0][1] < 10

    def test_session_reused_within_ttl(self):
        device = _acked_device()
        device.set_setting("auto_off", 1)
        device.transport.responses.extend(
            [
                _answer(bytes([0x90, 0xF0, 0x00, 0x3E, 0x00])),
                _answer(bytes([0x95, 0x0F, 0x00, 0x3E]) + (2).to_bytes(4, "big")),
            ]
        )
        device.set_setting("auto_off", 2)
        announces = [
            w for w in device.transport.written_properties if w[0] == "device_connected"
        ]
        assert len(announces) == 1

    def test_session_re_announced_after_ttl(self):
        device = _acked_device()
        device.set_setting("auto_off", 2)
        device._session().announced_at = time.time() - 400  # expired
        device.transport.responses.extend(
            [
                _answer(bytes([0x90, 0xF0, 0x00, 0x3E, 0x00])),
                _answer(bytes([0x95, 0x0F, 0x00, 0x3E]) + (2).to_bytes(4, "big")),
            ]
        )
        device.set_setting("auto_off", 2)
        announces = [
            w for w in device.transport.written_properties if w[0] == "device_connected"
        ]
        assert len(announces) == 2

    def test_unsupported_transport_announces_fail(self):
        class NoWrite(FakeTransport):
            write_property = None

        device = _device(transport=NoWrite())
        with pytest.raises((ConnectionError, AttributeError, TypeError)):
            device.standby()


class TestBrewAndPower:
    def test_brew_explicit_recipe(self):
        device = _device()
        device.brew(0x07, b"\x01\x00\x28\x1b\x01\x02\x04", profile_slot=1, accessory=2)
        frame = _sent_frame(device)[:-4]  # strip 4B timestamp
        assert frame[:4] == b"\x0d\x0f\x83\xf0"
        assert frame[4] == 0x07
        assert frame[5] == 0x01  # START
        assert frame[-3] == 0x06  # (1<<2)|2 before the CRC

    def test_brew_fetches_profile_recipe(self):
        blob = _blob("lan_d039_1_rec_espresso")
        transport = FakeTransport(
            properties={
                "d039_1_rec_espresso": {"value": base64.b64encode(blob).decode()}
            }
        )
        device = _device(transport=transport)
        device.brew(0x01)
        frame = _sent_frame(device)[:-4]
        # TLVs of the a6f0 blob minus drop tags {0x08, 0x19}.
        assert frame[6:-3] == bytes.fromhex("01 00 28 1b 01 02 04 04 00")
        assert frame[-3] == 0x04  # (slot 1 << 2) | accessory 0
        assert transport.requested_properties == ["d039_1_rec_espresso"]

    def test_brew_unknown_recipe_fails(self):
        device = _device()
        with pytest.raises(ConnectionError):
            device.brew(0x7F)  # not in recipe_beverage_order

    def test_power_methods(self):
        device = _device()
        device.wake()
        assert _sent_frame(device)[:-4].hex() == "0d07840f02015512"
        device.standby()
        assert _sent_frame(device)[:-4].hex() == "0d07840f01010041"
        device.session_refresh()
        # non-striker session refresh re-announces the timestamp
        last = device.transport.written_properties[-1]
        assert last[0] == "device_connected"

    def test_stop_brew_frame(self):
        device = _device()
        device.stop_brew()
        # Identical to the captured "stop" frame.
        assert _sent_frame(device)[:-4] == bytes.fromhex(CAPTURED_FRAMES["stop"])

    def test_do_stop_uses_default_recipe_without_fetch(self):
        device = _device()
        device.do("stop")
        assert _sent_frame(device)[:-4] == bytes.fromhex(CAPTURED_FRAMES["stop"])
        assert device.transport.requested_properties == []

    def test_do_stored_recipe_entry_is_byte_identical(self):
        # espresso_soul has no catalogue recipe → stored `recipe` is used.
        device = _device()
        device.do("espresso_soul")
        assert _sent_frame(device)[:-4] == bytes.fromhex(
            CAPTURED_FRAMES["espresso_soul"]
        )
        assert device.transport.written_properties[0][0] == "device_connected"

    def test_do_power_entry_builds_and_gates(self):
        device = _device()
        device.do("wakeup")
        assert _sent_frame(device)[:-4] == bytes.fromhex(CAPTURED_FRAMES["wakeup"])
        assert device.transport.written_properties[0][0] == "device_connected"

    def test_do_without_live_recipe_fails_closed(self):
        device = _device(response_poll=0)
        with pytest.raises(ConnectionError):
            device.do("espresso")
        # Only the 0xA6 republish trigger may go out, never a brew frame.
        assert all(frame[0][2] != 0x83 for frame in device.transport.sent)

    def test_do_brews_with_live_recipe(self):
        blob = _blob("lan_d039_1_rec_espresso")
        transport = FakeTransport(
            properties={
                "d039_1_rec_espresso": {"value": base64.b64encode(blob).decode()}
            }
        )
        device = _device(transport=transport, response_poll=0)
        device.do("espresso")
        frame = _sent_frame(device)[:-4]
        assert frame[4] == 0x01  # espresso beverage id from the map entry
        assert frame[5] == 0x01  # START action from the map entry
        # Live TLVs of the a6f0 blob minus drop tags {0x08, 0x19}.
        assert frame[6:-3] == bytes.fromhex("01 00 28 1b 01 02 04 04 00")
        assert transport.requested_properties == ["d039_1_rec_espresso"]
        assert transport.written_properties[0][0] == "device_connected"

    def test_do_unknown_name_raises(self):
        device = _device()
        with pytest.raises(ValueError):
            device.do("not_a_drink")


class TestProfiles:
    def test_get_profiles_from_republished_props(self):
        props = {
            "d034_profiles_1_3": {
                "value": base64.b64encode(_blob("lan_d034_profiles_1_3")).decode()
            },
            "d035_profiles_4_5": {
                "value": base64.b64encode(_blob("lan_d035_profiles_4_5")).decode()
            },
        }
        device = _device(transport=FakeTransport(properties=props))
        slots = device.get_profiles()
        assert [(s.index, s.name) for s in slots] == [
            (1, "Alice"),
            (2, "Bob"),
            (3, "Profil 3"),
            (4, "Profil 4"),
            (5, "Profil 5"),
        ]

    def test_select_profile_updates_on_ack(self):
        ack = _answer(bytes([0xA9, 0xF0, 0x02, 0x00]))
        device = _device(transport=FakeTransport(responses=[ack]))
        device.response_poll = 0
        assert device.select_profile(2) is True
        assert device.current_profile == 2
        # The A9 frame was sent after a session announce.
        sent = _sent_frame(device)
        assert sent[:4].hex() == "0d06a9f0" and sent[4] == 0x02

    def test_select_profile_no_ack_no_state(self):
        device = _device(
            transport=FakeTransport(), response_timeout=0.01, response_poll=0
        )
        assert device.select_profile(3) is False
        assert device.current_profile == 0

    def test_select_profile_negative_ack_no_state(self):
        nack = _answer(bytes([0xA9, 0xF0, 0x04, 0x01]))
        device = _device(transport=FakeTransport(responses=[nack]))
        device.response_poll = 0
        assert device.select_profile(4) is False
        assert device.current_profile == 0


class TestSettings:
    def test_set_setting_write_and_readback(self):
        ack = _answer(bytes([0x90, 0xF0, 0x00, 0x3E, 0x00]))
        readback = _answer(bytes([0x95, 0x0F, 0x00, 0x3E]) + (2).to_bytes(4, "big"))
        device = _device(transport=FakeTransport(responses=[ack, readback]))
        device.response_poll = 0
        assert device.set_setting("auto_off", 2) is True

    def test_set_setting_write_ack_failure(self):
        nack = _answer(bytes([0x90, 0xF0, 0x00, 0x3E, 0x01]))
        device = _device(transport=FakeTransport(responses=[nack]))
        device.response_poll = 0
        assert device.set_setting("auto_off", 2) is False

    def test_set_setting_no_ack_raises(self):
        device = _device(
            transport=FakeTransport(), response_timeout=0.01, response_poll=0
        )
        with pytest.raises(TimeoutError):
            device.set_setting("auto_off", 1)

    def test_set_setting_validation(self):
        device = _device()
        with pytest.raises(ValueError):
            device.set_setting("bogus", 1)
        with pytest.raises(ValueError):
            device.set_setting("auto_off", 9)

    def test_get_settings_from_republished_blobs(self):
        props = {
            "d282_mchn_sett_aoff": {
                "value": _fixture("lan_d282_mchn_sett_aoff")["data"]["value"]
            },
            "d283_mchn_sett_water": {
                "value": _fixture("lan_d283_mchn_sett_water")["data"]["value"]
            },
        }
        device = _device(transport=FakeTransport(properties=props))
        settings = device.get_settings()
        assert settings["auto_off"] == 2
        assert settings["water_hardness"] == 1


class TestCatalog:
    def test_read_catalog_requests_names_and_parses(self):
        # Seed two representative blobs; the device must request them all
        # and hand the collected values to build_catalog.
        props = {
            "d001_rec_espresso": {
                "value": _fixture("lan_d001_rec_espresso")["data"]["value"]
            },
            "d039_1_rec_espresso": {
                "value": _fixture("lan_d039_1_rec_espresso")["data"]["value"]
            },
        }
        device = _device(transport=FakeTransport(properties=props))
        device.response_poll = 0
        catalog = device.read_catalog(settle=0)
        assert 0x01 in catalog.beverages
        assert catalog.beverages[0x01].profiles.get(1)
        # Every catalog_datapoints name was requested.
        assert set(device.transport.requested_properties) == set(
            device.catalog_datapoints
        )
        assert catalog.source == "machine"

    def test_declared_catalog_follows_model_ref(self):
        assert len(_device("ECAM610.json").declared_catalog().beverages) == 28
        assert len(_device("ECAM612.json").declared_catalog().beverages) == 22

    def test_striker_map_reads_declared_catalog_without_traffic(self):
        # ECAM452 publishes no recipe datapoints; its model_ref points the
        # declaration at STRIKER_BEST (STRIKER_GOOD declares no recipes).
        device = _device("ECAM452.json")
        catalog = device.read_catalog()
        assert catalog.source == "model_table"
        assert len(catalog.beverages) == 48
        assert device.transport.requested_properties == []
