"""Datapoint routing: data_response mailbox, named properties, monitor (T047).

Non-monitor ``data_response`` payloads must never reach the monitor
snapshot; named value datapoints land in the per-property store; the
``0xE1`` frame surfaces as a request abort to response consumers.
"""

import asyncio
import base64

import pytest
from cremalink.core.binary import crc16_ccitt
from cremalink.domain.device import Device
from cremalink.local_server_app.state import LocalServerState


class DummySettings:
    queue_max_size = 10
    log_ring_size = 10
    fixed_random_2 = None
    fixed_time_2 = None
    server_settings_path = ""
    monitor_poll_interval = 0.05


class DummyLogger:
    def info(self, *args, **kwargs):
        return None


def _run(coro):
    return asyncio.run(coro)


def _state():
    state = LocalServerState(DummySettings(), DummyLogger())
    _run(
        state.configure(
            dsn="dsn1",
            device_ip="1.2.3.4",
            lan_key="lan-key",
            device_scheme="http",
            monitor_property_name="d302_monitor",
        )
    )
    return state


def _dp(name=None, value=None):
    data = {}
    if name is not None:
        data["name"] = name
    if value is not None:
        data["value"] = value
    return {"data": data}


class TestDatapointRouting:
    def test_data_response_goes_to_mailbox_not_monitor(self):
        state = _state()
        _run(state.handle_datapoint(_dp("data_response", "0AiQ8AA=")))
        snap = _run(state.snapshot_monitor())
        assert snap["monitor_b64"] is None
        mailbox = _run(state.pop_response())
        assert mailbox["value"] == "0AiQ8AA="

    def test_app_data_response_alias_also_mailbox(self):
        state = _state()
        _run(state.handle_datapoint(_dp("app_data_response", "AAAA")))
        snap = _run(state.snapshot_monitor())
        assert snap["monitor_b64"] is None
        assert _run(state.pop_response()) is not None

    def test_named_property_goes_to_store_not_monitor(self):
        state = _state()
        _run(state.handle_datapoint(_dp("d282_mchn_sett_aoff", "0JU=")))
        snap = _run(state.snapshot_monitor())
        assert snap["monitor_b64"] is None
        named = _run(state.get_named_property("d282_mchn_sett_aoff"))
        assert named["value"] == "0JU="

    def test_monitor_datapoint_still_monitor(self):
        state = _state()
        _run(state.handle_datapoint(_dp("d302_monitor", "QUJD")))
        snap = _run(state.snapshot_monitor())
        assert snap["monitor_b64"] == "QUJD"

    def test_legacy_unnamed_value_is_monitor_compatible(self):
        state = _state()
        _run(state.handle_datapoint(_dp(value="bW9uaXRvcg==")))
        snap = _run(state.snapshot_monitor())
        assert snap["monitor_b64"] == "bW9uaXRvcg=="

    def test_properties_block_routes_entries_by_name(self):
        state = _state()
        _run(
            state.handle_datapoint(
                {
                    "data": {
                        "properties": {
                            "1": {
                                "property": {
                                    "name": "d039_1_rec_espresso",
                                    "value": "QUJD",
                                }
                            }
                        }
                    }
                }
            )
        )
        named = _run(state.get_named_property("d039_1_rec_espresso"))
        assert named["value"] == "QUJD"


class TestSyncTimeoutAbort:
    def _e1_frame_b64(self):
        # Machine-originated 0xE1 sync-timeout/abort notification.
        frame = bytes([0xD0, 0x05, 0xE1, 0x0F])
        frame += crc16_ccitt(frame)
        return base64.b64encode(frame).decode()

    def test_e1_reaches_mailbox(self):
        state = _state()
        _run(state.handle_datapoint(_dp("data_response", self._e1_frame_b64())))
        entry = _run(state.pop_response())
        assert entry is not None
        raw = base64.b64decode(entry["value"])
        assert raw[2] == 0xE1

    def test_wait_answer_raises_on_e1(self):
        from cremalink.ecam.answers import is_sync_timeout

        frame = bytes([0xD0, 0x05, 0xE1, 0x0F])
        frame += crc16_ccitt(frame)
        assert is_sync_timeout(frame)

        class _T:
            def pop_response(self):
                return bytes(frame)  # transport returns decoded bytes

            def send_command(self, *a, **k):
                return {}

        device = Device(transport=_T())
        with pytest.raises(TimeoutError):
            device._wait_answer(0xA2)
