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
    """One rate-limit session (quota window) at one point in time.

    Fields:
        type: Agent-defined label (e.g. `"current"`, `"weekly"` for Claude;
            `"primary"`, `"secondary"` for Codex). Passed through to the
            display untouched.
        used_pct: Fraction of the session used, in `0.0`-`1.0`. Agents
            must scale upstream values into this range before
            constructing the snapshot.
        resets_at: Unix seconds (UTC) when the session rolls over.
        rolling: True when the upstream's `resets_at` drifts with
            wall-clock during idle (Codex). False when it is anchored to
            a first-usage event and stays put until expiry (Claude). The
            firmware uses this to decide whether to synthesize a local
            countdown when `used_pct` is essentially zero.
        window_duration_mins: Total length of the window in minutes
            (e.g. 300 for a 5h window, 10080 for a 7d window). Required
            for the firmware's idle-synthesis path; the wire format
            accepts 0 to mean "unknown / synthesis disabled".

    Defaults exist for the two new fields so call sites that don't
    distinguish rolling-vs-fixed (older tests, ad-hoc construction) keep
    working. Producers that know the answer must set both explicitly.
    """

    type: str
    used_pct: float
    resets_at: int
    rolling: bool = False
    window_duration_mins: int = 0


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

    def semantically_equal(self, other: "AgentSnapshot | None") -> bool:
        """Return True iff `other` represents the same user-visible state.

        Compares `agent` and the per-session
        `(type, used_pct, resets_at, rolling, window_duration_mins)`
        tuples, sorted by `type` so session order is not significant.
        Ignores `captured_at` — that timestamp bumps every time the
        daemon re-reads, but does not reflect a user-visible change.
        Returns False if `other is None`.

        Used by the Codex daemon's poll loop to decide whether a freshly
        read rate-limit snapshot needs to be pushed to the firmware.
        """
        if other is None:
            return False
        if self.agent != other.agent:
            return False
        a = sorted(
            (s.type, s.used_pct, s.resets_at, s.rolling, s.window_duration_mins)
            for s in self.sessions
        )
        b = sorted(
            (s.type, s.used_pct, s.resets_at, s.rolling, s.window_duration_mins)
            for s in other.sessions
        )
        return a == b
