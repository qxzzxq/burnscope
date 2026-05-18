"""Wire-format types shared by the daemon and the ESP32 firmware.

These dataclasses are agent-agnostic; their shape is the source of truth
documented in `docs/wire-format.md`. Each value of `AgentSnapshot.agent`
(e.g. `"claude"`, `"codex"`) is produced by a concrete subclass of
`burnscope_client.agent.Agent`, which is responsible for normalising any
upstream-specific scale (Codex's 0-100 percent → 0.0-1.0) before
constructing `SessionSnapshot` instances here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SessionSnapshot:
    """One rate-limit session (rolling-quota window) at one point in time.

    Fields:
        type: Agent-defined label (e.g. `"5h"`, `"7d"` for Claude;
            `"primary"`, `"secondary"` for Codex). Passed through to the
            display untouched.
        used_pct: Fraction of the session used, in `0.0`-`1.0`. Agents
            must scale upstream values into this range before
            constructing the snapshot.
        resets_at: Unix seconds (UTC) when the session rolls over.
    """

    type: str
    used_pct: float
    resets_at: int


@dataclass(frozen=True)
class AgentSnapshot:
    """All sessions reported by one agent at one point in time.

    Fields:
        agent: Lowercase agent identifier (e.g. `"claude"`, `"codex"`).
        captured_at: Unix seconds (UTC) when the daemon read the
            upstream response.
        sessions: One or more `SessionSnapshot` entries. Order is not
            significant; clients look up by `type`.
    """

    agent: str
    captured_at: int
    sessions: list[SessionSnapshot]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict matching `docs/wire-format.md`."""
        return {
            "agent": self.agent,
            "captured_at": self.captured_at,
            "sessions": [asdict(s) for s in self.sessions],
        }
