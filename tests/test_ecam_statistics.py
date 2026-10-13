"""US2 — 0xA2 statistics paging + label interpretation + cloud counters."""

import base64
import json
from pathlib import Path

import pytest
from cremalink.domain.device import Device
from cremalink.ecam.answers import decode_a2_page, is_sync_timeout
from cremalink.ecam.statistics import (
    counter_breakdown,
    parse_counter_value,
    parse_water_volume_liters,
    resolve_cloud_counters,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ecam"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _frame(name):
    """Decode a `frame_hex`/`hex` field or `data.value` b64 into bytes."""
    fixture = _fixture(name)
    if isinstance(fixture, dict) and "frame_hex" in fixture:
        return bytes.fromhex(fixture["frame_hex"])
    data = fixture.get("data", fixture)
    return base64.b64decode("".join(str(data["value"]).split()))


class ScriptedTransport:
    """Drives `Device.get_statistics` with a scripted A2 table.

    ``table`` maps stat id -> value (the machine's sparse table). Each
    ``(start, count)`` request answers the next ``count`` records with
    id >= start — emulating the real machine's fill semantics.
    ``missing_starts`` simulates ids that produce no answer (timeout).
    """

    def __init__(self, table, *, missing_starts=(), raw_frames=None):
        from cremalink.core.binary import crc16_ccitt

        self.table = dict(sorted(table.items()))
        self.missing_starts = set(missing_starts)
        self.raw_frames = raw_frames or {}
        self.requests = []
        self._crc16 = crc16_ccitt

    def send_command(self, command, alternative_property=None):
        frame = base64.b64decode(command)
        start = int.from_bytes(frame[4:6], "big")
        count = frame[6]
        self.requests.append((start, count))
        return {}

    def pop_response(self):
        if not self.requests:
            return None
        start, count = self.requests[-1]
        if start in self.missing_starts:
            return None
        if start in self.raw_frames:
            return self.raw_frames[start]
        records = [(i, v) for i, v in self.table.items() if i >= start][:count]
        if not records:
            return None
        body = bytearray([0xD0, 0x00, 0xA2, 0x0F])
        for stat_id, value in records:
            body += stat_id.to_bytes(2, "big") + value.to_bytes(4, "big")
        body[1] = len(body) + 1
        return bytes(body) + self._crc16(bytes(body))


def _table_from_corpus():
    table = {}
    for page in _fixture("a2_frames.json"):
        table.update({int(k): v for k, v in page["entries"].items()})
    return table


def _device(table, timeout=0.01, poll=0.001, **kwargs):
    return Device(
        transport=ScriptedTransport(table, **kwargs),
        response_timeout=timeout,
        response_poll=poll,
    )


def test_decode_a2_page_records():
    pages = _fixture("a2_frames.json")
    first = bytes.fromhex(pages[0]["frame_hex"])
    records = decode_a2_page(first)
    assert records[:3] == [(100, 2483940), (101, 823), (105, 1)]
    assert len(records) == 10


def test_full_statistics_replay_display_values():
    report = _device(_table_from_corpus()).get_statistics()
    assert report.complete
    assert report.source == "native"

    # Display-verified values (a2_display_match.json).
    assert report.entries[105].value == 1  # Entkalkungen
    assert report.entries[106].value == pytest.approx(207.207, abs=1e-3)
    assert report.entries[106].unit == "l"
    assert report.entries[115].value == 100  # Behälter cleans
    assert report.entries[3000].value == 577  # black beverages
    assert report.entries[3001].value == 143  # milk coffee
    assert report.entries[3003].value == 91  # milk only
    assert report.entries[43010].value == 813  # total

    # Labelled/unlabelled behavior.
    assert report.entries[3000].label == "total_black"
    assert report.entries[100].label is None  # unknown id passthrough
    assert report.entries[23000].label is None


def test_short_page_terminates_paging():
    device = _device(_table_from_corpus())
    device.get_statistics()
    # Walks the table until a page comes back shorter than requested.
    last = device.transport.requests[-1]
    assert last[1] == 10
    remaining = [i for i in _table_from_corpus() if i >= last[0]]
    assert len(remaining) < 10
    starts = [req[0] for req in device.transport.requests]
    assert starts[:2] == [0, 3001]


def test_timeout_retries_with_smaller_count_never_eof():
    table = _table_from_corpus()

    class Flaky(ScriptedTransport):
        def pop_response(self):
            if self.requests and self.requests[-1] == (0, 10):
                return None  # force one timeout at full page size
            return super().pop_response()

    device = Device(transport=Flaky(table), response_timeout=0.01, response_poll=0.001)
    report = device.get_statistics()
    assert report.entries[100].raw == 2483940
    first_requests = device.transport.requests[:2]
    assert first_requests == [(0, 10), (0, 9)]


def test_timeout_exhaustion_raises_timeout_error():
    device = _device({}, missing_starts={0})  # machine never answers
    with pytest.raises(TimeoutError):
        device.get_statistics()


def test_sync_timeout_frame_aborts():
    # Craft a 0xE1 answer frame: D0 05 E1 0F <crc>.
    from cremalink.core.binary import crc16_ccitt

    body = bytes([0xD0, 0x05, 0xE1, 0x0F])
    e1 = body + crc16_ccitt(body)
    assert is_sync_timeout(e1)
    device = _device({}, raw_frames={0: e1})
    with pytest.raises(TimeoutError):
        device.get_statistics()


def test_native_statistics_requires_pop_response():
    class BareTransport:
        pass

    device = Device(transport=BareTransport())
    with pytest.raises(NotImplementedError):
        device.get_statistics()


# --- Cloud counters path -------------------------------------------------


def _cloud_device(properties):
    class CloudProps:
        def get_properties(self):
            return properties

        def pop_response(self):
            raise NotImplementedError

    device = Device(transport=CloudProps(), statistics_source="cloud_counters")
    return device


def test_parse_counter_value_shapes():
    assert parse_counter_value(42) == 42
    assert parse_counter_value("42") == 42
    assert parse_counter_value('{"r1": 3, "r2": 4}') == 7
    assert parse_counter_value(None) is None
    assert parse_counter_value(True) is None
    assert parse_counter_value("garbage") is None
    assert counter_breakdown('{"r1": 3}') == {"r1": 3}
    assert counter_breakdown(3) is None


def test_water_volume_liters():
    assert parse_water_volume_liters(2_427_590) == pytest.approx(2427.59)
    assert parse_water_volume_liters("1500") == 1.5
    assert parse_water_volume_liters("junk") is None


def test_resolve_cloud_counters_candidates():
    props = {
        "d701_tot_bev_b": '{"1": 5, "2": 10}',
        "d553_water_tot_qty": "2000",
        "d552_cnt_calc_tot": "3",
    }
    counters = resolve_cloud_counters(props)
    assert counters["total_beverages"] == 15
    assert counters["water_total_quantity"] == pytest.approx(2.0)
    assert counters["descales"] == 3
    # absent candidates produce no key (never fabricated zeroes)
    assert "espresso" not in counters


def test_get_statistics_cloud_counters_report():
    props = {
        "d701_tot_bev_b": '{"1": 5, "2": 10}',
        "d553_water_tot_qty": "100000",
    }
    report = _cloud_device(props).get_statistics()
    assert report.source == "cloud_counters"
    assert report.cloud_counters["total_beverages"] == 15
    assert report.cloud_counters["water_total_quantity"] == pytest.approx(100.0)
    assert report.breakdowns["total_beverages"] == {"1": 5, "2": 10}
