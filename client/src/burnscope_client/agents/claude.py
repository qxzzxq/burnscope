"""Claude rate-limit probe.

Issues a tiny `POST /v1/messages` (max_tokens=1) to api.anthropic.com
using the user's OAuth token and parses the
`anthropic-ratelimit-unified-{5h,7d}-*` response headers (relabelled
to `current`/`weekly`) into an
`AgentSnapshot` matching `docs/wire-format.md`.

Anthropic already returns `utilization` as a `0.0`-`1.0` float, so no
scaling is needed in this agent.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import httpx

from ..agent import Agent, ProbeError
from ..credentials import Credential, CredentialsError
from ..schema import AgentSnapshot, SessionSnapshot

API_URL = "https://api.anthropic.com/v1/messages"
PROBE_MODEL = "claude-haiku-4-5-20251001"
USER_AGENT = "claude-code/2.1.5"

# Maps the Anthropic header window (`5h`/`7d`) to the BurnScope session
# label we emit on the wire. Anthropic's own vocabulary stays in the
# header names; the daemon presents a friendlier label downstream.
_SESSION_TYPES: tuple[tuple[str, str], ...] = (
    ("5h", "current"),
    ("7d", "weekly"),
)


@dataclass(frozen=True)
class ClaudeCredential(Credential):
    """The OAuth access token used to probe Anthropic.

    Built by `ClaudeCredential.load()`, which reads either the macOS
    login keychain or a JSON file (the Claude Code CLI uses both,
    platform-dependent) via the shared readers on `Credential`.
    """

    access_token: str

    @classmethod
    def load(
        cls,
        *,
        keychain_service: str,
        credentials_path: Path,
    ) -> "ClaudeCredential":
        """Read the Claude Code credential blob and extract its token.

        On macOS reads `security find-generic-password -s <service>`;
        elsewhere reads `credentials_path`. The blob is expected to
        contain `claudeAiOauth.accessToken` or a top-level
        `accessToken`.
        """
        if sys.platform == "darwin":
            blob = cls._read_keychain(keychain_service)
            source = f"keychain:{keychain_service}"
        else:
            blob = cls._read_file(credentials_path)
            source = str(credentials_path)
        data = cls._parse_json(blob, source)
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken") or data.get("accessToken")
        if not token:
            raise CredentialsError("No accessToken found in Claude credentials blob")
        return cls(access_token=token)


class ClaudeAgent(Agent):
    """Probes Anthropic's rate-limit headers via a 1-token request.

    The credential storage locations live here as class attributes so
    the agent class is the single source of truth for where Claude
    Code stores its credentials.
    """

    name = "claude"
    KEYCHAIN_SERVICE: ClassVar[str] = "Claude Code-credentials"
    CREDENTIALS_PATH: ClassVar[Path] = Path.home() / ".claude" / ".credentials.json"

    def __init__(self, credential: ClaudeCredential) -> None:
        self._credential = credential

    async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
        """Probe Anthropic and return the current Claude `AgentSnapshot`."""
        try:
            response = await client.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {self._credential.access_token}",
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
            agent=self.name,
            captured_at=int(time.time()),
            sessions=sessions,
        )

    @classmethod
    def load_credential(cls) -> ClaudeCredential:
        """Load Claude credentials from the agent-owned location."""
        return ClaudeCredential.load(
            keychain_service=cls.KEYCHAIN_SERVICE,
            credentials_path=cls.CREDENTIALS_PATH,
        )


def _parse_sessions(headers: httpx.Headers) -> list[SessionSnapshot]:
    """Extract zero or more sessions from Anthropic's response headers."""
    sessions: list[SessionSnapshot] = []
    for window, label in _SESSION_TYPES:
        util = headers.get(f"anthropic-ratelimit-unified-{window}-utilization")
        reset = headers.get(f"anthropic-ratelimit-unified-{window}-reset")
        if util is None or reset is None:
            continue
        sessions.append(
            SessionSnapshot(
                type=label,
                used_pct=_parse_float(util, label, "utilization"),
                resets_at=_parse_reset(reset, label),
            )
        )
    return sessions


def _parse_float(value: str, session_type: str, field: str) -> float:
    """Parse a header value as a float or raise `ProbeError`."""
    try:
        return float(value)
    except ValueError as exc:
        raise ProbeError(
            f"Malformed {field} for {session_type}: {value!r}"
        ) from exc


def _parse_reset(value: str, session_type: str) -> int:
    """Parse a reset timestamp (ISO-8601 or unix seconds) into unix seconds."""
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
