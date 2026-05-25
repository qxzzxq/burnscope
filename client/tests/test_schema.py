"""Tests for `burnscope_client.schema` — the wire-format dataclasses."""

from __future__ import annotations

from burnscope_client.schema import AgentSnapshot, SessionSnapshot


def _snap(captured_at: int, sessions: list[SessionSnapshot]) -> AgentSnapshot:
    return AgentSnapshot(agent="codex", captured_at=captured_at, sessions=sessions)


def test_semantically_equal_returns_false_for_none():
    snap = _snap(1, [SessionSnapshot("primary", 0.15, 1779066600)])
    assert snap.semantically_equal(None) is False


def test_semantically_equal_ignores_captured_at():
    a = _snap(1000, [SessionSnapshot("primary", 0.15, 1779066600)])
    b = _snap(9999, [SessionSnapshot("primary", 0.15, 1779066600)])
    assert a.semantically_equal(b) is True


def test_semantically_equal_handles_session_order():
    a = _snap(
        1,
        [
            SessionSnapshot("primary", 0.15, 1779066600),
            SessionSnapshot("secondary", 0.02, 1779156000),
        ],
    )
    b = _snap(
        1,
        [
            SessionSnapshot("secondary", 0.02, 1779156000),
            SessionSnapshot("primary", 0.15, 1779066600),
        ],
    )
    assert a.semantically_equal(b) is True


def test_semantically_equal_detects_used_pct_change():
    a = _snap(1, [SessionSnapshot("primary", 0.15, 1779066600)])
    b = _snap(1, [SessionSnapshot("primary", 0.17, 1779066600)])
    assert a.semantically_equal(b) is False


def test_semantically_equal_detects_resets_at_change():
    """A new window reset boundary is a real state transition, not jitter."""
    a = _snap(1, [SessionSnapshot("primary", 0.0, 1779066600)])
    b = _snap(1, [SessionSnapshot("primary", 0.0, 1779070000)])
    assert a.semantically_equal(b) is False


def test_semantically_equal_detects_session_set_change():
    a = _snap(1, [SessionSnapshot("primary", 0.15, 1779066600)])
    b = _snap(
        1,
        [
            SessionSnapshot("primary", 0.15, 1779066600),
            SessionSnapshot("secondary", 0.02, 1779156000),
        ],
    )
    assert a.semantically_equal(b) is False


def test_semantically_equal_detects_agent_change():
    a = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.15, 1779066600)],
    )
    b = AgentSnapshot(
        agent="claude",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.15, 1779066600)],
    )
    assert a.semantically_equal(b) is False


def test_session_snapshot_defaults_are_conservative():
    """Defaults must mean 'treat as fixed-window, no synthesis' — the
    Claude case. Producers that know better override both."""
    s = SessionSnapshot("primary", 0.15, 1779066600)
    assert s.rolling is False
    assert s.window_duration_mins == 0


def test_to_dict_emits_rolling_and_window_duration():
    snap = AgentSnapshot(
        agent="codex",
        captured_at=1779050146,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.15,
                resets_at=1779066600,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )
    session = snap.to_dict()["sessions"][0]
    assert session["rolling"] is True
    assert session["window_duration_mins"] == 300


def test_semantically_equal_detects_rolling_flip():
    a = _snap(
        1,
        [
            SessionSnapshot(
                "primary", 0.15, 1779066600, rolling=False, window_duration_mins=300
            )
        ],
    )
    b = _snap(
        1,
        [
            SessionSnapshot(
                "primary", 0.15, 1779066600, rolling=True, window_duration_mins=300
            )
        ],
    )
    assert a.semantically_equal(b) is False


def test_semantically_equal_detects_window_duration_change():
    a = _snap(
        1,
        [
            SessionSnapshot(
                "primary", 0.15, 1779066600, rolling=True, window_duration_mins=300
            )
        ],
    )
    b = _snap(
        1,
        [
            SessionSnapshot(
                "primary", 0.15, 1779066600, rolling=True, window_duration_mins=600
            )
        ],
    )
    assert a.semantically_equal(b) is False
