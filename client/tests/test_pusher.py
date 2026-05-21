import json

import httpx
import pytest
import respx

from burnscope_client.pusher import (
    CLIENT_ID_HEADER,
    PushAuthError,
    PushError,
    fetch_health,
    push,
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
