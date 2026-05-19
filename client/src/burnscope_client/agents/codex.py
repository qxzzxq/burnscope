"""Codex rate-limit probe.

Issues a minimal streaming POST to the ChatGPT Codex `responses`
endpoint using the user's access token and reads
`x-codex-{primary,secondary}-{used-percent,reset-at}` from the
response headers. The body is an SSE stream we never need to consume —
we exit the streaming context as soon as headers arrive.

Codex returns `used-percent` as a `0`-`100` integer, so this agent
divides by 100 before constructing `SessionSnapshot`s (the wire format
always carries `0.0`-`1.0`). See `docs/wire-format.md` and
`docs/probe-codex.sh`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import httpx

from ..agent import Agent, ProbeError
from ..credentials import Credential, CredentialsError
from ..schema import AgentSnapshot, SessionSnapshot

API_URL = "https://chatgpt.com/backend-api/codex/responses"

_SESSION_TYPES = ("primary", "secondary")

_PROBE_PAYLOAD = {
    "model": "gpt-5.5",
    "instructions": "",
    "input": [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }
    ],
    "tools": [],
    "tool_choice": "auto",
    "parallel_tool_calls": False,
    "store": False,
    "stream": True,
    "include": [],
}


@dataclass(frozen=True)
class CodexCredential(Credential):
    """Codex access token plus the optional ChatGPT account id.

    `account_id`, when present, is sent as the `ChatGPT-Account-ID`
    header on each probe — matching the Codex CLI's own behaviour.
    """

    access_token: str
    account_id: str | None = None

    @classmethod
    def load(cls, *, credentials_path: Path) -> "CodexCredential":
        """Read `credentials_path` (typically `~/.codex/auth.json`).

        `tokens.access_token` is required; `tokens.account_id` is
        optional.
        """
        blob = cls._read_file(credentials_path)
        data = cls._parse_json(blob, str(credentials_path))
        tokens = data.get("tokens") or {}
        access = tokens.get("access_token")
        if not access:
            raise CredentialsError(
                f"No tokens.access_token in Codex credentials at {credentials_path}"
            )
        return cls(access_token=access, account_id=tokens.get("account_id") or None)


class CodexAgent(Agent):
    """Probes ChatGPT Codex's rate-limit headers via a streaming POST."""

    name = "codex"
    active_interval: ClassVar[float] = 60.0
    idle_interval: ClassVar[float] = 300.0
    CREDENTIALS_PATH: ClassVar[Path] = Path.home() / ".codex" / "auth.json"

    def __init__(self, credential: CodexCredential) -> None:
        self._credential = credential

    async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
        """Probe ChatGPT and return the current Codex `AgentSnapshot`.

        Opens a streaming POST so the SSE body is never downloaded; we
        only need the response headers. The stream is closed by exiting
        the async context manager.
        """
        headers = {
            "Authorization": f"Bearer {self._credential.access_token}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
        }
        if self._credential.account_id:
            headers["ChatGPT-Account-ID"] = self._credential.account_id

        try:
            async with client.stream(
                "POST",
                API_URL,
                headers=headers,
                json=_PROBE_PAYLOAD,
                timeout=10.0,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise ProbeError(
                        f"ChatGPT returned {response.status_code}: "
                        f"{body[:200]!r}"
                    )
                sessions = _parse_sessions(response.headers)
        except httpx.HTTPError as exc:
            raise ProbeError(f"HTTP error talking to ChatGPT: {exc}") from exc

        if not sessions:
            raise ProbeError("No x-codex-* rate-limit headers in response")

        return AgentSnapshot(
            agent=self.name,
            captured_at=int(time.time()),
            sessions=sessions,
        )

    @classmethod
    def load_credential(cls) -> CodexCredential:
        """Load Codex credentials from the agent-owned location."""
        return CodexCredential.load(credentials_path=cls.CREDENTIALS_PATH)


def _parse_sessions(headers: httpx.Headers) -> list[SessionSnapshot]:
    """Extract zero or more sessions from ChatGPT's response headers."""
    sessions: list[SessionSnapshot] = []
    for session_type in _SESSION_TYPES:
        pct = headers.get(f"x-codex-{session_type}-used-percent")
        reset = headers.get(f"x-codex-{session_type}-reset-at")
        if pct is None or reset is None:
            continue
        sessions.append(
            SessionSnapshot(
                type=session_type,
                used_pct=_parse_percent(pct, session_type),
                resets_at=_parse_reset(reset, session_type),
            )
        )
    return sessions


def _parse_percent(value: str, session_type: str) -> float:
    """Convert a `0`-`100` percent string to a `0.0`-`1.0` float.

    Codex reports integers but we accept anything `float()` understands
    so a future shift to fractional percents is non-breaking.
    """
    try:
        return float(value) / 100.0
    except ValueError as exc:
        raise ProbeError(
            f"Malformed used-percent for {session_type}: {value!r}"
        ) from exc


def _parse_reset(value: str, session_type: str) -> int:
    """Parse a `reset-at` header as unix seconds."""
    try:
        return int(value)
    except ValueError as exc:
        raise ProbeError(
            f"Malformed reset-at for {session_type}: {value!r}"
        ) from exc
