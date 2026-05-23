import json

import httpx
import pytest
import respx

from burnscope_client.host_cache import PairedDevice
from burnscope_client.pusher import (
    CLIENT_ID_HEADER,
    PushAuthError,
    PushError,
    PushResult,
    fetch_health,
    push,
    push_to_all,
)
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


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
