import time

import httpx
import pytest
import respx

from burnscope_client.agent import ProbeError
from burnscope_client.agents.claude import ClaudeAgent, ClaudeCredential
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _agent(token: str = "test-token") -> ClaudeAgent:
    return ClaudeAgent(ClaudeCredential(access_token=token))


def _mock_response(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(
        200,
        headers=headers,
        json={"id": "msg_x", "content": [{"type": "text", "text": ""}]},
    )


@respx.mock
async def test_probe_builds_snapshot_from_headers():
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.03",
        "anthropic-ratelimit-unified-5h-reset": "2026-05-18T17:30:00Z",
        "anthropic-ratelimit-unified-7d-utilization": "0.09",
        "anthropic-ratelimit-unified-7d-reset": "2026-05-19T18:00:00Z",
    }
    route = respx.post(ANTHROPIC_URL).mock(return_value=_mock_response(headers))

    async with httpx.AsyncClient() as client:
        snap = await _agent().probe(client)

    assert route.called
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer test-token"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["anthropic-beta"] == "oauth-2025-04-20"

    assert snap.agent == "claude"
    assert isinstance(snap.captured_at, int)
    assert abs(snap.captured_at - int(time.time())) < 5

    by_type = {s.type: s for s in snap.sessions}
    assert set(by_type) == {"current", "weekly"}
    assert by_type["current"].used_pct == pytest.approx(0.03)
    # 2026-05-18T17:30:00Z and 2026-05-19T18:00:00Z
    assert by_type["current"].resets_at == 1779125400
    assert by_type["weekly"].used_pct == pytest.approx(0.09)
    assert by_type["weekly"].resets_at == 1779213600


@respx.mock
async def test_probe_to_dict_matches_wire_format():
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.5",
        "anthropic-ratelimit-unified-5h-reset": "2026-05-18T17:30:00Z",
        "anthropic-ratelimit-unified-7d-utilization": "0.25",
        "anthropic-ratelimit-unified-7d-reset": "2026-05-19T18:00:00Z",
    }
    respx.post(ANTHROPIC_URL).mock(return_value=_mock_response(headers))

    async with httpx.AsyncClient() as client:
        snap = await _agent("t").probe(client)

    payload = snap.to_dict()
    assert payload["agent"] == "claude"
    assert isinstance(payload["captured_at"], int)
    assert isinstance(payload["sessions"], list)
    for entry in payload["sessions"]:
        assert set(entry) == {"type", "used_pct", "resets_at"}
        assert isinstance(entry["used_pct"], float)
        assert isinstance(entry["resets_at"], int)


@respx.mock
async def test_probe_partial_headers_returns_only_present_sessions():
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-5h-reset": "2026-05-18T17:30:00Z",
    }
    respx.post(ANTHROPIC_URL).mock(return_value=_mock_response(headers))

    async with httpx.AsyncClient() as client:
        snap = await _agent("t").probe(client)

    types = [s.type for s in snap.sessions]
    assert types == ["current"]


@respx.mock
async def test_probe_no_rate_limit_headers_raises():
    respx.post(ANTHROPIC_URL).mock(return_value=_mock_response({}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(ProbeError):
            await _agent("t").probe(client)


@respx.mock
async def test_probe_malformed_utilization_raises():
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "not-a-number",
        "anthropic-ratelimit-unified-5h-reset": "2026-05-18T17:30:00Z",
    }
    respx.post(ANTHROPIC_URL).mock(return_value=_mock_response(headers))

    async with httpx.AsyncClient() as client:
        with pytest.raises(ProbeError):
            await _agent("t").probe(client)


@respx.mock
async def test_probe_http_error_raises():
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(500, json={}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProbeError):
            await _agent("t").probe(client)


def test_session_snapshot_dataclass_fields():
    s = SessionSnapshot(type="current", used_pct=0.5, resets_at=123)
    assert s.type == "current"
    assert s.used_pct == 0.5
    assert s.resets_at == 123


def test_agent_snapshot_dataclass_fields():
    snap = AgentSnapshot(
        agent="claude",
        captured_at=100,
        sessions=[SessionSnapshot("current", 0.1, 200)],
    )
    assert snap.agent == "claude"
    assert snap.sessions[0].type == "current"


def test_claude_agent_name():
    assert ClaudeAgent.name == "claude"
