import json

import httpx
import pytest
import respx

from burnscope_client import host_cache, pusher
from burnscope_client.discovery import DiscoveredDevice
from burnscope_client.host_cache import PairedDevice
from burnscope_client.pusher import (
    CLIENT_ID_HEADER,
    PushAuthError,
    PushError,
    PushResult,
    fetch_health,
    push,
    push_to_all,
    refresh_and_retry_transport_failures,
)
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))
    return tmp_path


CLIENT_ID = "a" * 64


def _snapshot() -> AgentSnapshot:
    return AgentSnapshot(
        agent="claude",
        captured_at=1779050146,
        sessions=[
            SessionSnapshot("current", 0.03, 1779066600),
            SessionSnapshot("weekly", 0.09, 1779156000),
        ],
    )


@respx.mock
async def test_push_posts_snapshot_with_client_id_header():
    route = respx.post("http://esp.local/summary").mock(
        return_value=httpx.Response(204)
    )

    async with httpx.AsyncClient() as client:
        await push(_snapshot(), "esp.local", CLIENT_ID, client)

    assert route.called
    request = route.calls[0].request
    assert request.headers[CLIENT_ID_HEADER] == CLIENT_ID
    assert json.loads(request.read()) == _snapshot().to_dict()


@respx.mock
async def test_push_401_raises_push_auth_error():
    respx.post("http://esp.local/summary").mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushAuthError):
            await push(_snapshot(), "esp.local", CLIENT_ID, client)


@respx.mock
async def test_push_500_raises_push_error_not_auth():
    respx.post("http://esp.local/summary").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError) as exc_info:
            await push(_snapshot(), "esp.local", CLIENT_ID, client)
    assert not isinstance(exc_info.value, PushAuthError)


@respx.mock
async def test_push_transport_error_raises_push_error():
    respx.post("http://esp.local/summary").mock(
        side_effect=httpx.ConnectError("boom")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError):
            await push(_snapshot(), "esp.local", CLIENT_ID, client)


@respx.mock
async def test_fetch_health_sends_client_id_header_and_returns_body():
    route = respx.get("http://esp.local/health").mock(
        return_value=httpx.Response(200, json={"agents": {}})
    )
    async with httpx.AsyncClient() as client:
        body = await fetch_health("esp.local", CLIENT_ID, client)

    assert body == {"agents": {}}
    assert route.calls[0].request.headers[CLIENT_ID_HEADER] == CLIENT_ID


@respx.mock
async def test_fetch_health_returns_none_on_transport_failure():
    respx.get("http://esp.local/health").mock(
        side_effect=httpx.ConnectError("nope")
    )
    async with httpx.AsyncClient() as client:
        assert await fetch_health("esp.local", CLIENT_ID, client) is None


# ----------------------------------------------------------- push_to_all

async def test_push_to_all_returns_empty_for_no_devices():
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), [], CLIENT_ID, client)
    assert results == {}


@respx.mock
async def test_push_to_all_fans_out_in_parallel_with_all_success():
    respx.post("http://10.0.0.5:80/summary").mock(return_value=httpx.Response(204))
    respx.post("http://10.0.0.6:80/summary").mock(return_value=httpx.Response(204))
    devices = [
        PairedDevice("dev-a", "10.0.0.5:80"),
        PairedDevice("dev-b", "10.0.0.6:80"),
    ]
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), devices, CLIENT_ID, client)

    assert set(results) == {"dev-a", "dev-b"}
    assert all(r.kind == "ok" and r.ok for r in results.values())


@respx.mock
async def test_push_to_all_isolates_per_device_outcomes():
    """One 401 + one 500 + one 204 should yield exactly that mix."""
    respx.post("http://10.0.0.5:80/summary").mock(return_value=httpx.Response(204))
    respx.post("http://10.0.0.6:80/summary").mock(return_value=httpx.Response(401))
    respx.post("http://10.0.0.7:80/summary").mock(return_value=httpx.Response(500))
    devices = [
        PairedDevice("dev-ok",    "10.0.0.5:80"),
        PairedDevice("dev-auth",  "10.0.0.6:80"),
        PairedDevice("dev-trans", "10.0.0.7:80"),
    ]
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), devices, CLIENT_ID, client)

    assert results["dev-ok"]    == PushResult("dev-ok",    True,  "ok")
    assert results["dev-auth"]  == PushResult("dev-auth",  False, "auth")
    assert results["dev-trans"] == PushResult("dev-trans", False, "transport")


@respx.mock
async def test_push_to_all_handles_transport_exception():
    respx.post("http://10.0.0.6:80/summary").mock(
        side_effect=httpx.ConnectError("dropped")
    )
    devices = [PairedDevice("dev-down", "10.0.0.6:80")]
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), devices, CLIENT_ID, client)
    assert results["dev-down"].kind == "transport"


@respx.mock
async def test_push_to_all_sends_client_id_header_per_device():
    route_a = respx.post("http://10.0.0.5:80/summary").mock(
        return_value=httpx.Response(204)
    )
    route_b = respx.post("http://10.0.0.6:80/summary").mock(
        return_value=httpx.Response(204)
    )
    async with httpx.AsyncClient() as client:
        await push_to_all(
            _snapshot(),
            [PairedDevice("a", "10.0.0.5:80"), PairedDevice("b", "10.0.0.6:80")],
            CLIENT_ID,
            client,
        )
    assert route_a.calls[0].request.headers[CLIENT_ID_HEADER] == CLIENT_ID
    assert route_b.calls[0].request.headers[CLIENT_ID_HEADER] == CLIENT_ID


# -------------------------------------------- refresh_and_retry_transport_failures


def _stub_discover(monkeypatch, devices):
    async def fake(timeout=4.0, agent=None, zc=None):
        return list(devices)
    monkeypatch.setattr(pusher, "discover_all", fake)


async def test_refresh_and_retry_noop_when_no_transport_failures(monkeypatch):
    """No transport failures → no mDNS browse, results returned as-is."""
    def boom(*a, **kw):
        raise AssertionError("discover_all must not run without transport failures")
    monkeypatch.setattr(pusher, "discover_all", boom)

    results = {
        "dev-a": PushResult("dev-a", True,  "ok"),
        "dev-b": PushResult("dev-b", False, "auth"),
    }
    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(),
            [PairedDevice("dev-a", "10.0.0.5:80"), PairedDevice("dev-b", "10.0.0.6:80")],
            results, CLIENT_ID, client, "claude",
        )
    assert out == results


@respx.mock
async def test_refresh_and_retry_updates_cache_and_retries_on_new_host(monkeypatch):
    """The headline bug: device's IP changed → discover new host, update cache, retry."""
    host_cache.add_paired_device("claude", PairedDevice("dev-moved", "10.0.0.5:80"))
    _stub_discover(monkeypatch, [
        DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False),
    ])
    new_route = respx.post("http://10.0.0.9:80/summary").mock(
        return_value=httpx.Response(204)
    )
    results = {"dev-moved": PushResult("dev-moved", False, "transport")}
    devices = [PairedDevice("dev-moved", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )

    assert out["dev-moved"] == PushResult("dev-moved", True, "ok")
    assert new_route.called
    cached = host_cache.load_paired_devices("claude")
    assert cached == [PairedDevice("dev-moved", "10.0.0.9:80")]


async def test_refresh_and_retry_skips_when_host_unchanged(monkeypatch):
    """mDNS confirms same host → no retry (the failure was truly transient)."""
    _stub_discover(monkeypatch, [
        DiscoveredDevice("dev-same", "10.0.0.5:80", True, False),
    ])
    results = {"dev-same": PushResult("dev-same", False, "transport")}
    devices = [PairedDevice("dev-same", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )
    assert out == results


async def test_refresh_and_retry_keeps_failure_when_device_not_discovered(monkeypatch):
    """mDNS didn't see the device → keep original transport failure, don't retry."""
    _stub_discover(monkeypatch, [])
    results = {"dev-gone": PushResult("dev-gone", False, "transport")}
    devices = [PairedDevice("dev-gone", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )
    assert out == results


@respx.mock
async def test_refresh_and_retry_leaves_other_devices_untouched(monkeypatch):
    """Only the transport-failed device is mDNS-refreshed; others pass through."""
    host_cache.add_paired_device("claude", PairedDevice("dev-moved", "10.0.0.5:80"))
    _stub_discover(monkeypatch, [
        DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False),
        DiscoveredDevice("dev-ok",    "10.0.0.6:80", True, False),
    ])
    respx.post("http://10.0.0.9:80/summary").mock(return_value=httpx.Response(204))
    results = {
        "dev-ok":    PushResult("dev-ok",    True,  "ok"),
        "dev-moved": PushResult("dev-moved", False, "transport"),
    }
    devices = [
        PairedDevice("dev-ok",    "10.0.0.6:80"),
        PairedDevice("dev-moved", "10.0.0.5:80"),
    ]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )
    assert out["dev-ok"] == PushResult("dev-ok", True, "ok")
    assert out["dev-moved"] == PushResult("dev-moved", True, "ok")
