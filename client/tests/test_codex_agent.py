import time

import httpx
import pytest
import respx

from burnscope_client.agent import ProbeError
from burnscope_client.agents.codex import CodexAgent, CodexCredential


CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"


def _agent(token: str = "tok", account_id: str | None = None) -> CodexAgent:
    return CodexAgent(CodexCredential(access_token=token, account_id=account_id))


def _stream_response(headers: dict[str, str], status_code: int = 200) -> httpx.Response:
    # An SSE body the agent should never consume.
    return httpx.Response(
        status_code,
        headers={"Content-Type": "text/event-stream", **headers},
        content=b"data: {}\n\n",
    )


@respx.mock
async def test_probe_builds_snapshot_with_percent_divided_by_100():
    headers = {
        "x-codex-primary-used-percent": "7",
        "x-codex-primary-reset-at": "1779125400",
        "x-codex-secondary-used-percent": "42",
        "x-codex-secondary-reset-at": "1779213600",
    }
    route = respx.post(CODEX_URL).mock(return_value=_stream_response(headers))

    async with httpx.AsyncClient() as client:
        snap = await _agent("tok-abc", account_id="acct-1").probe(client)

    assert route.called
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer tok-abc"
    assert sent.headers["chatgpt-account-id"] == "acct-1"
    assert sent.headers["openai-beta"] == "responses=experimental"

    assert snap.agent == "codex"
    assert isinstance(snap.captured_at, int)
    assert abs(snap.captured_at - int(time.time())) < 5

    by_type = {s.type: s for s in snap.sessions}
    assert set(by_type) == {"primary", "secondary"}
    assert by_type["primary"].used_pct == pytest.approx(0.07)
    assert by_type["primary"].resets_at == 1779125400
    assert by_type["secondary"].used_pct == pytest.approx(0.42)
    assert by_type["secondary"].resets_at == 1779213600


@respx.mock
async def test_probe_omits_account_header_when_no_account_id():
    headers = {
        "x-codex-primary-used-percent": "5",
        "x-codex-primary-reset-at": "1779000000",
    }
    route = respx.post(CODEX_URL).mock(return_value=_stream_response(headers))

    async with httpx.AsyncClient() as client:
        await _agent("tok").probe(client)

    sent = route.calls[0].request
    assert "chatgpt-account-id" not in {k.lower() for k in sent.headers}


@respx.mock
async def test_probe_partial_headers_returns_only_present_sessions():
    headers = {
        "x-codex-primary-used-percent": "33",
        "x-codex-primary-reset-at": "1779000000",
    }
    respx.post(CODEX_URL).mock(return_value=_stream_response(headers))

    async with httpx.AsyncClient() as client:
        snap = await _agent().probe(client)

    types = [s.type for s in snap.sessions]
    assert types == ["primary"]


@respx.mock
async def test_probe_no_rate_limit_headers_raises():
    respx.post(CODEX_URL).mock(return_value=_stream_response({}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProbeError):
            await _agent().probe(client)


@respx.mock
async def test_probe_malformed_percent_raises():
    headers = {
        "x-codex-primary-used-percent": "abc",
        "x-codex-primary-reset-at": "1779000000",
    }
    respx.post(CODEX_URL).mock(return_value=_stream_response(headers))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProbeError):
            await _agent().probe(client)


@respx.mock
async def test_probe_malformed_reset_raises():
    headers = {
        "x-codex-primary-used-percent": "10",
        "x-codex-primary-reset-at": "soonish",
    }
    respx.post(CODEX_URL).mock(return_value=_stream_response(headers))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProbeError):
            await _agent().probe(client)


@respx.mock
async def test_probe_http_error_raises():
    respx.post(CODEX_URL).mock(return_value=_stream_response({}, status_code=500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProbeError):
            await _agent().probe(client)


def test_codex_agent_name():
    assert CodexAgent.name == "codex"
