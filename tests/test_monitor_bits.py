"""Documented switch/alarm bits decoded from the monitor frame."""

from cremalink.ecam.monitor_bits import ALARM_BITS, SWITCH_BITS, decode_bits
from cremalink.parsing.monitor.decode import build_monitor_snapshot
from cremalink.parsing.monitor.view import MonitorView

# Captured ECAM610 monitor blobs (HA log, 2026-10-08).
READY_IDLE = "0BJ1DwAAAAAABwQAAAAAAAAXZWrHVjY="
BREWING_MOTORS = "0BJ1DwAGAAAABwsRAAAAAADJlmrHVko="


def _view(raw_b64):
    return MonitorView(build_monitor_snapshot({"monitor_b64": raw_b64}))


def test_idle_frame_has_no_bits_set():
    view = _view(READY_IDLE)
    assert not any(view.switch_states().values())
    assert not any(view.alarm_states().values())


def test_brew_frame_sets_motor_switches():
    states = _view(BREWING_MOTORS).switch_states()
    assert states["motor_up"] is True and states["motor_down"] is True
    assert states["clean_knob"] is False


def test_decode_bits_named_switches_and_alarms():
    # switches bit 10 (clean knob) = byte1 bit2; alarms bit 14 = byte1 bit6.
    assert decode_bits(bytes([0x00, 0x04]), SWITCH_BITS)["clean_knob"] is True
    assert decode_bits(bytes([0, 0x40, 0, 0]), ALARM_BITS)["clean_knob"] is True
    assert decode_bits(bytes([0, 0, 0x01, 0]), ALARM_BITS)["not_enough_coffee"] is False


def test_short_word_yields_none_not_false():
    # A 2-byte alarm word (short frame) cannot speak for the upper bits.
    states = decode_bits(bytes([0x00, 0x00]), ALARM_BITS)
    assert states["descale"] is False
    assert states["grinding_unit_1_problem"] is None


def test_bit_tables_have_unique_keys():
    for table in (SWITCH_BITS, ALARM_BITS):
        keys = [bit.key for bit in table.values()]
        assert len(keys) == len(set(keys))
