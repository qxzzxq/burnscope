import httpx
import pytest
import respx

from burnscope_client.schema import AgentSnapshot, SessionSnapshot
from burnscope_client.pusher import PushError, push


def _snapshot() -> AgentSnapshot:
    return AgentSnapshot(
        agent="claude",
        captured_at=1779050146,
        sessions=[
            SessionSnapshot("5h", 0.03, 1779066600),
            SessionSnapshot("7d", 0.09, 1779156000),
        ],
    )


@respx.mock
async def test_push_posts_snapshot_as_json():
    route = respx.post("http://esp.local:80/summary").mock(
        return_value=httpx.Response(204)
    )

    async with httpx.AsyncClient() as client:
        await push(_snapshot(), "esp.local:80", client)

    assert route.called
    body = route.calls[0].request.read()
    import json

    parsed = json.loads(body)
    assert parsed == _snapshot().to_dict()


@respx.mock
async def test_push_assumes_port_80_when_missing():
    route = respx.post("http://esp.local/summary").mock(
        return_value=httpx.Response(204)
    )
    async with httpx.AsyncClient() as client:
        await push(_snapshot(), "esp.local", client)
    assert route.called


@respx.mock
async def test_push_non_2xx_raises_push_error():
    respx.post("http://esp.local/summary").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError):
            await push(_snapshot(), "esp.local", client)


@respx.mock
async def test_push_network_error_raises_push_error():
    respx.post("http://esp.local/summary").mock(
        side_effect=httpx.ConnectError("boom")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError):
            await push(_snapshot(), "esp.local", client)
