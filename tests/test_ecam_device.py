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

    def __init__(self, *, properties=None, responses=None, on_send=None):
        self.sent = []  # decoded wire payloads (frame + ts)
        self.properties = dict(properties or {})
        self.responses = list(responses or [])
        self.written_properties = []
        self.requested_properties = []
        self.on_send = on_send  # optional hook: frame -> responses to queue

    def send_command(self, command, alternative_property=None):
        frame = base64.b64decode(command)
        self.sent.append((frame, alternative_property))
        if self.on_send is not None:
            for answer in self.on_send(frame) or []:
                self.responses.append(answer)
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
        device.set_setting("auto_off", 2)
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

    def test_select_profile_no_ack_marks_selected(self):
        # Non-striker 0xA9 is fire-and-forget — the app storms it ~11x
        # and never sees an ack (cloud capture 2026-10-14), yet the
        # machine applies the select; there is no read-back on Soul
        # (DECODED.md §335). A silent channel still marks the selection.
        device = _device(
            transport=FakeTransport(), response_timeout=0.01, response_poll=0
        )
        assert device.select_profile(3) is True
        assert device.current_profile == 3

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

    @pytest.mark.parametrize(
        "status", ["in_standby", "going_to_sleep", "sleeping", "deep_sleep"]
    )
    def test_set_setting_in_standby_writes_without_waking(self, status):
        # Standby does not block configuration writes (§14.8 — the machine
        # processes data_request while in_standby; the app exposes config
        # changes there, only profile selection is gated). The library
        # still never sends a wake frame for a config change.
        ack = _answer(bytes([0x90, 0xF0, 0x00, 0x3E, 0x00]))
        readback = _answer(bytes([0x95, 0x0F, 0x00, 0x3E]) + (2).to_bytes(4, "big"))
        device = _device(
            transport=FakeTransport(responses=[ack, readback]), response_poll=0
        )
        device.transport.get_monitor = lambda: None
        device.get_monitor = lambda: type("M", (), {"status_name": status})()
        assert device.set_setting("auto_off", 2) is True
        assert device.transport.written_properties[0][0] == "device_connected"
        # sent[0] is the s0 session handshake (§14.8); the write follows.
        assert _sent_frame(device, 0)[:2] == b"\x45\xda"
        assert _sent_frame(device, 1)[2] == 0x90
        assert all(sent[0][2] != 0x84 for sent in device.transport.sent)

    def test_set_setting_does_not_wake_an_awake_machine(self):
        ack = _answer(bytes([0x90, 0xF0, 0x00, 0x3E, 0x00]))
        readback = _answer(bytes([0x95, 0x0F, 0x00, 0x3E]) + (2).to_bytes(4, "big"))
        device = _device(
            transport=FakeTransport(responses=[ack, readback]), response_poll=0
        )
        device.transport.get_monitor = lambda: None
        device.get_monitor = lambda: type("M", (), {"status_name": "ready"})()
        assert device.set_setting("auto_off", 2) is True
        assert 0x84 not in [sent[0][2] for sent in device.transport.sent]

    @staticmethod
    def _aoff_blob(value):
        return _answer(bytes([0x95, 0x0F, 0x00, 0x3E]) + value.to_bytes(4, "big"))

    def test_set_setting_without_ack_is_confirmed_by_the_republished_property(self):
        # The Soul is not known to ack 0x90; the write counts once the
        # backing dNNN property shows the new value.
        blob = base64.b64encode(self._aoff_blob(3)).decode()
        device = _device(
            transport=FakeTransport(properties={"d282_mchn_sett_aoff": blob}),
            response_timeout=0.5,
            response_poll=0,
        )
        assert device.set_setting("auto_off", 3) is True
        assert "d282_mchn_sett_aoff" in device.transport.requested_properties

    def test_set_setting_without_ack_and_stale_property_reports_failure(self):
        blob = base64.b64encode(self._aoff_blob(1)).decode()
        device = _device(
            transport=FakeTransport(properties={"d282_mchn_sett_aoff": blob}),
            write_confirm_seconds=0.05,
            response_poll=0,
        )
        assert device.set_setting("auto_off", 3) is False

    def test_set_setting_no_ack_raises(self):
        device = _device(
            transport=FakeTransport(), write_confirm_seconds=0.01, response_poll=0
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


# Vendored live answer to `0D 07 95 F0 03 EE` (cloud_datapoint_history_
# 20261014.json): page of params 1006-1013, param 0x03EE = 0 -> profile 1.
PROFILE_PAGE = bytes.fromhex(
    "d02795f003ee00000000005dde900000002000000002"
    "00000000000000000000000000000000d91f6acf6642"
)


def _awake(device):
    """Make _machine_in_standby() report an awake machine."""
    device.transport.get_monitor = lambda: None
    device.get_monitor = lambda: type("M", (), {"status_name": "ready"})()
    return device


class TestCurrentProfile:
    def test_refresh_reads_03ee_and_updates_state(self):
        device = _awake(
            _device(
                transport=FakeTransport(responses=[PROFILE_PAGE]),
                response_poll=0,
            )
        )
        assert device.refresh_current_profile() == 1
        assert device.current_profile == 1
        # 0x95 read went out on the wire (0x03EE >= 1000 -> flag F0).
        assert _sent_frame(device)[:-4] == bytes.fromhex("0d0895f003ee011f66")
        # Session was announced first (reads are gated too — §14.8).
        assert device.transport.written_properties[0][0] == "device_connected"

    def test_refresh_still_runs_in_standby(self):
        # §14.8 addendum: the machine answered 0xA2 statistics reads
        # while monitor status was in_standby — standby does not block
        # data_request processing, so the profile read is attempted even
        # when the machine is asleep (a timeout just keeps last-known).
        device = _device(
            transport=FakeTransport(responses=[PROFILE_PAGE]),
            response_poll=0,
        )
        device.transport.get_monitor = lambda: None
        device.get_monitor = lambda: type("M", (), {"status_name": "in_standby"})()
        assert device.refresh_current_profile() == 1
        assert device.current_profile == 1
        assert _sent_frame(device)[:-4] == bytes.fromhex("0d0895f003ee011f66")

    def test_refresh_keeps_last_known_on_no_answer(self):
        # Non-striker: a known selection is the truth (0x03EE doesn't
        # track 0xA9 selects — verified 2026-10-25) — the read is skipped
        # entirely once a profile is known.
        device = _awake(
            _device(transport=FakeTransport(), response_timeout=0.01, response_poll=0)
        )
        device.current_profile = 3
        assert device.refresh_current_profile() == 3
        assert device.current_profile == 3
        # No 0x95 read went out for a known non-striker profile.
        assert device.transport.sent == []

    def test_refresh_reads_register_when_profile_unknown(self):
        # On cold start (current_profile=0) the register is still the
        # best available hint for the machine's state.
        device = _awake(
            _device(
                transport=FakeTransport(responses=[PROFILE_PAGE]),
                response_poll=0,
            )
        )
        device.transport.get_monitor = lambda: None
        assert device.refresh_current_profile() == 1
        assert device.current_profile == 1


class TestSettingBits:
    @staticmethod
    def _conf_blob(value):
        return _answer(bytes([0x95, 0x0F, 0x00, 0x3F]) + value.to_bytes(4, "big"))

    def test_get_setting_bits_from_republished_d284(self):
        blob = base64.b64encode(self._conf_blob(0x1D)).decode()
        device = _device(
            transport=FakeTransport(
                properties={"d284_mchn_sett_user_conf": {"value": blob}}
            )
        )
        bits = device.get_setting_bits()
        assert bits["beep"] is True
        assert bits["energy_saving"] is True

    def test_set_bit_merges_and_preserves_others(self):
        # raw 0x1D, turning beep OFF must write 0x19 (only bit 0x04 cleared).
        device = _awake(_device(response_poll=0))
        device.transport.properties["d284_mchn_sett_user_conf"] = {
            "value": base64.b64encode(self._conf_blob(0x1D)).decode()
        }
        device.transport.responses.append(
            self._conf_blob(0x19)  # read-back answers the new value
        )
        assert device.set_setting_bit("beep", False) is True
        frames = [sent[0][:-4] for sent in device.transport.sent]
        assert bytes.fromhex("0d0b900f003f00000019a8aa") in frames
        assert device.transport.written_properties[0][0] == "device_connected"

    def test_set_bit_enables_energy_saving(self):
        device = _awake(_device(response_poll=0))
        device.transport.properties["d284_mchn_sett_user_conf"] = {
            "value": base64.b64encode(self._conf_blob(0x0D)).decode()
        }
        device.transport.responses.append(self._conf_blob(0x1D))
        assert device.set_setting_bit("energy_saving", True) is True
        frames = [sent[0][:-4] for sent in device.transport.sent]
        assert bytes.fromhex("0d0b900f003f0000001de82e") in frames

    def test_read_param_waits_for_delayed_property_republish(self):
        """The machine serves the queued GET a poll cycle later — the read
        must poll the store instead of falling back to the `0x95` mailbox
        (its answer *is* the republished property, protocol.md §13)."""
        blob = base64.b64encode(self._conf_blob(0x1D)).decode()

        class DelayedRepublish(FakeTransport):
            def __init__(self):
                super().__init__()
                self.get_calls = 0

            def request_property(self, name):
                super().request_property(name)

            def get_property(self, name):
                self.get_calls += 1
                if self.get_calls < 3:  # answer lands ~2 polls later
                    return None
                return {"value": blob}

        device = _device(transport=DelayedRepublish(), response_poll=0)
        assert device._read_param_value(0x003F, "user_conf") == 0x1D
        # the named-property path sufficed — no 0x95 frame hit the wire
        assert not any(sent[0][2] == 0x95 for sent in device.transport.sent)

    def test_set_bit_falls_back_to_mailbox_read(self):
        # No stored d284: the base value comes from a 0x95 mailbox read.
        device = _awake(
            _device(
                transport=FakeTransport(
                    responses=[self._conf_blob(0x1D), self._conf_blob(0x0D)]
                ),
                response_poll=0,
            )
        )
        assert device.set_setting_bit("energy_saving", False) is True
        frames = [sent[0][:-4] for sent in device.transport.sent]
        assert bytes.fromhex("0d08950f003f012b83") in frames  # base read
        assert bytes.fromhex("0d0b900f003f0000000dfa1f") in frames  # write

    def test_set_bit_refuses_blind_write_when_unreadable(self):
        device = _awake(
            _device(transport=FakeTransport(), response_timeout=0.01, response_poll=0)
        )
        with pytest.raises(TimeoutError):
            device.set_setting_bit("beep", True)
        # No 0x90 write may go out without the base value.
        assert all(sent[0][2] != 0x90 for sent in device.transport.sent)

    @pytest.mark.parametrize(
        "status", ["in_standby", "going_to_sleep", "sleeping", "deep_sleep"]
    )
    def test_set_bit_in_standby_writes_without_waking(self, status):
        # Same standby write allowance as set_setting: announce + merged
        # 0x90 write go out, no wake frame.
        blob = base64.b64encode(self._conf_blob(0x1D)).decode()
        ack = _answer(bytes([0x90, 0x0F, 0x00, 0x3F, 0x00]))
        readback = _answer(bytes([0x95, 0x0F, 0x00, 0x3F]) + (0x19).to_bytes(4, "big"))
        device = _device(
            transport=FakeTransport(
                properties={"d284_mchn_sett_user_conf": {"value": blob}},
                responses=[ack, readback],
            ),
            response_poll=0,
        )
        device.transport.get_monitor = lambda: None
        device.get_monitor = lambda: type("M", (), {"status_name": status})()
        assert device.set_setting_bit("beep", False) is True
        assert device.transport.written_properties[0][0] == "device_connected"
        write = next(sent[0] for sent in device.transport.sent if sent[0][2] == 0x90)
        assert write[:-4] == bytes.fromhex("0d0b900f003f00000019a8aa")
        assert all(sent[0][2] != 0x84 for sent in device.transport.sent)

    def test_set_bit_retries_unconfirmed_write_with_fresh_announce(self):
        # The machine dropped the first write (observed live 2026-10-14 —
        # acked ok yet the param kept its old value): the retry must
        # re-announce the session before resending.
        blob = base64.b64encode(self._conf_blob(0x1D)).decode()
        ack = _answer(bytes([0x90, 0x0F, 0x00, 0x3F, 0x00]))
        stale = _answer(bytes([0x95, 0x0F, 0x00, 0x3F]) + (0x1D).to_bytes(4, "big"))
        good = _answer(bytes([0x95, 0x0F, 0x00, 0x3F]) + (0x19).to_bytes(4, "big"))
        write_count = [0]

        def _respond(frame):
            # Model the real mailbox: answers arrive per request.
            if frame[2] == 0x90:
                write_count[0] += 1
                return [ack]
            if frame[2] == 0x95 and frame[4:6] == b"\x00\x3f":
                return [stale] if write_count[0] < 2 else [good]
            return []

        device = _awake(
            _device(
                transport=FakeTransport(
                    properties={"d284_mchn_sett_user_conf": {"value": blob}},
                    on_send=_respond,
                ),
                response_poll=0,
                write_confirm_seconds=0.05,
            )
        )
        assert device.set_setting_bit("beep", False) is True
        announces = [
            name
            for name, _ in device.transport.written_properties
            if name == "device_connected"
        ]
        assert len(announces) == 2  # re-announced before the retry
        writes = [sent[0] for sent in device.transport.sent if sent[0][2] == 0x90]
        assert len(writes) == 2

    def test_set_bit_unknown_key_raises(self):
        device = _device()
        with pytest.raises(ValueError):
            device.set_setting_bit("bogus", True)

    def test_set_bit_unconfirmed_reports_failure(self):
        # Property keeps the old value and no read-back arrives.
        blob = base64.b64encode(self._conf_blob(0x1D)).decode()
        device = _awake(
            _device(
                transport=FakeTransport(
                    properties={"d284_mchn_sett_user_conf": {"value": blob}}
                ),
                write_confirm_seconds=0.05,
                response_poll=0,
            )
        )
        assert device.set_setting_bit("beep", False) is False


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
