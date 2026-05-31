"""Regression: a healthy codex /health probe clears a stale per-device
failure flag.

After a transient blip writes `last-push.codex.<device> = ok:false`, a
device can recover at the *same* host with its firmware still in sync.
When that happens neither self-healing path fires — the host-change heal
needs a new address, and the divergence re-push needs the firmware to
have lost state — and an idle codex daemon never re-pushes to refresh the
flag. So the plain healthy probe (`_mark_health_ok`) must itself persist
ok=True, or `burnscope status` reports a perfectly healthy display as
down indefinitely.
"""

from __future__ import annotations

from burnscope_client import host_cache
from burnscope_client.codex_daemon import CodexDaemon
from burnscope_client.host_cache import PairedDevice
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


def _snapshot() -> AgentSnapshot:
    return AgentSnapshot(
        agent="codex",
        captured_at=1000,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.1,
                resets_at=2000,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )


def _in_sync_health_body() -> dict:
    """A /health body whose codex sessions match `_snapshot()` exactly,
    so `_firmware_diverged` reports no divergence."""
    return {
        "agents": {
            "codex": {
                "sessions": [
                    {
                        "type": "primary",
                        "used_pct": 0.1,
                        "resets_at": 2000,
                        "rolling": True,
                        "window_duration_mins": 300,
                    }
                ]
            }
        }
    }


def test_mark_health_ok_clears_stale_failure(monkeypatch, tmp_path):
    # `host_cache.state_dir()` reads BURNSCOPE_STATE_DIR on every call, so
    # setting the env var is enough to redirect all state into tmp_path —
    # no module reload needed (matches the convention in test_codex_daemon).
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))

    device = PairedDevice(device_id="burnscope-x", host="192.168.1.5:80")
    # Leftover failure from an earlier transient blip.
    host_cache.write_push_state("codex", ok=False, device_id="burnscope-x")
    stale = host_cache.read_push_state("codex", device_id="burnscope-x")
    assert stale is not None and stale["ok"] is False

    daemon = CodexDaemon()
    daemon._last_pushed_snapshot = _snapshot()
    diverged: list[PairedDevice] = []
    daemon._mark_health_ok(device, _in_sync_health_body(), diverged)

    # In-sync firmware must not be flagged for a re-push...
    assert diverged == []
    # ...and the healthy probe must clear the stale failure flag.
    state = host_cache.read_push_state("codex", device_id="burnscope-x")
    assert state is not None
    assert state["ok"] is True
