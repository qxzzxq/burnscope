"""Claude rate-limit probe.

Issues a tiny `POST /v1/messages` (max_tokens=1) to api.anthropic.com using the
user's OAuth token and parses the `anthropic-ratelimit-unified-{5h,7d}-*`
response headers into an `AgentSnapshot` matching `docs/wire-format.md`.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
PROBE_MODEL = "claude-haiku-4-5-20251001"
USER_AGENT = "claude-code/2.1.5"

_SESSION_TYPES = ("5h", "7d")


class ProbeError(RuntimeError):
    """Raised when the Anthropic probe fails or returns unparseable data."""


@dataclass(frozen=True)
class SessionSnapshot:
    type: str
    used_pct: float
    resets_at: int


@dataclass(frozen=True)
class AgentSnapshot:
    agent: str
    captured_at: int
    sessions: list[SessionSnapshot]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "captured_at": self.captured_at,
            "sessions": [asdict(s) for s in self.sessions],
        }


async def probe(token: str, client: httpx.AsyncClient) -> AgentSnapshot:
    """Probe Anthropic and return the current Claude AgentSnapshot."""
    try:
        response = await client.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            json={
                "model": PROBE_MODEL,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise ProbeError(f"HTTP error talking to Anthropic: {exc}") from exc

    if response.status_code >= 400:
        raise ProbeError(
            f"Anthropic returned {response.status_code}: {response.text[:200]}"
        )

    sessions = _parse_sessions(response.headers)
    if not sessions:
        raise ProbeError("No anthropic-ratelimit-unified-* headers in response")

    return AgentSnapshot(
        agent="claude",
        captured_at=int(time.time()),
        sessions=sessions,
    )


def _parse_sessions(headers: httpx.Headers) -> list[SessionSnapshot]:
    sessions: list[SessionSnapshot] = []
    for session_type in _SESSION_TYPES:
        util = headers.get(f"anthropic-ratelimit-unified-{session_type}-utilization")
        reset = headers.get(f"anthropic-ratelimit-unified-{session_type}-reset")
        if util is None or reset is None:
            continue
        sessions.append(
            SessionSnapshot(
                type=session_type,
                used_pct=_parse_float(util, session_type, "utilization"),
                resets_at=_parse_reset(reset, session_type),
            )
        )
    return sessions


def _parse_float(value: str, session_type: str, field: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ProbeError(
            f"Malformed {field} for {session_type}: {value!r}"
        ) from exc


def _parse_reset(value: str, session_type: str) -> int:
    # Anthropic returns ISO-8601 (e.g. "2026-05-18T17:30:00Z"); accept a bare
    # unix-seconds integer too, in case the format ever changes.
    if value.isdigit():
        return int(value)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbeError(
            f"Malformed reset timestamp for {session_type}: {value!r}"
        ) from exc
    return int(dt.timestamp())
