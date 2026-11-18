"""CloudTransport: token renewal on 401, request timeouts, monitor timestamp."""

from __future__ import annotations

import datetime as dt

import pytest
import requests

from cremalink.transports.cloud import transport as cloud_mod
from cremalink.transports.cloud.transport import CloudTransport


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class _FakeCloud:
    """Answers GETs; the first ``expired`` calls with the old token get 401."""

    def __init__(self, *, valid_token="new", properties=None):
        self.valid_token = valid_token
        self.properties = properties or {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers, timeout, **_kw):
        self.calls.append((url, {"headers": headers, "timeout": timeout}))
        if headers["Authorization"] != f"auth_token {self.valid_token}":
            return _Resp(401)
        if url.endswith("/lan.json"):
            return _Resp(404)
        if "properties.json?names[]=" in url:
            name = url.split("names[]=")[1]
            return _Resp(200, [{"property": self.properties[name]}])
        return _Resp(200, {"device": {"key": 1, "model": "AY008ESP1"}})


def _transport(monkeypatch, cloud, *, token="new", refresher=None):
    monkeypatch.setattr(cloud_mod.requests, "get", cloud.get)
    t = CloudTransport("DSN", token, token_refresher=refresher)
    t.set_mappings({}, {"monitor": "d302_monitor_machine"})
    return t


def test_requests_carry_a_timeout(monkeypatch):
    cloud = _FakeCloud()
    _transport(monkeypatch, cloud)
    assert all(call[1]["timeout"] == cloud_mod.REQUEST_TIMEOUT for call in cloud.calls)


def test_expired_token_is_renewed_once_and_the_request_retried(monkeypatch):
    cloud = _FakeCloud(valid_token="new")
    renewals = []

    def refresher():
        renewals.append(1)
        return "new"

    t = _transport(monkeypatch, cloud, token="old", refresher=refresher)
    assert t.access_token == "new"
    assert len(renewals) == 1  # renewed on the first 401, then reused
    assert t.model == "AY008ESP1"


def test_expired_token_without_refresher_raises(monkeypatch):
    cloud = _FakeCloud(valid_token="new")
    with pytest.raises(requests.HTTPError):
        _transport(monkeypatch, cloud, token="old")


def test_monitor_received_at_uses_data_updated_at(monkeypatch):
    cloud = _FakeCloud(
        properties={
            "d302_monitor_machine": {
                "name": "d302_monitor_machine",
                "value": "0BJ1DwEBAAAABwAAAQAAAAA65Gr5Wu4=",
                "data_updated_at": "2026-11-15T07:10:40Z",
            }
        }
    )
    t = _transport(monkeypatch, cloud)
    snapshot = t.get_monitor()
    expected = dt.datetime(2026, 11, 15, 7, 10, 40, tzinfo=dt.UTC)
    received = snapshot.received_at
    # The snapshot keeps a naive local datetime; compare the instant.
    if isinstance(received, dt.datetime):
        assert received.timestamp() == expected.timestamp()
    else:
        assert received == expected.timestamp()
