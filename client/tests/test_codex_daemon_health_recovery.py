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

import asyncio

import pytest

from burnscope_client import codex_daemon, host_cache
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


def test_mark_health_ok_does_not_mask_pending_failed_push(monkeypatch, tmp_path):
    """A healthy probe must NOT clear ok=false while a newer snapshot is
    still failing to deliver to this device.

    Scenario (Codex review P2): a newer snapshot S2 failed to push to the
    device (transport), so `_push_failures[device] > 0` and the per-device
    state is ok=false. The firmware still holds the older, successfully
    pushed S1 — which equals `_last_pushed_snapshot` — so the probe body
    is *not* diverged. An unconditional ok=true here would mask the real
    failed delivery of S2 until the next push attempt flips it back.

    The clear is gated on there being no pending failed push for the
    device, so the failure stays visible to `burnscope status`.
    """
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))

    device = PairedDevice(device_id="burnscope-y", host="192.168.1.6:80")
    # Pusher recorded a failed delivery of the newer snapshot.
    host_cache.write_push_state("codex", ok=False, device_id="burnscope-y")

    daemon = CodexDaemon()
    daemon._last_pushed_snapshot = _snapshot()  # S1 — what the firmware holds
    daemon._push_failures["burnscope-y"] = 1  # S2 delivery still failing

    diverged: list[PairedDevice] = []
    # Body matches S1 (last successfully pushed) → not diverged.
    daemon._mark_health_ok(device, _in_sync_health_body(), diverged)

    assert diverged == []
    # The pending failed push must remain visible — not masked as healthy.
    state = host_cache.read_push_state("codex", device_id="burnscope-y")
    assert state is not None
    assert state["ok"] is False


def test_mark_health_ok_does_not_clear_failure_without_push_baseline(
    monkeypatch, tmp_path
):
    """A healthy probe must NOT clear ok=false before any snapshot has been
    delivered this run.

    Scenario (Codex review P2): the daemon just restarted, so
    `_last_pushed_snapshot` is None and `_push_failures` is empty, but a
    prior run left `last-push.codex.<device> = ok:false` on disk because the
    last `/summary` push had failed. The health loop runs before the first
    poll/push establishes a baseline. A successful `/health` probe proves
    only reachability — with no baseline we cannot compare the firmware
    against any delivered snapshot, so it does NOT prove the device holds
    current data. Clearing the persisted failure on reachability alone would
    report a never-successfully-pushed display as healthy.

    The clear is gated on having a known `_last_pushed_snapshot` (and the
    firmware matching it), so the failure stays visible until a real
    delivery confirms sync.
    """
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))

    device = PairedDevice(device_id="burnscope-z", host="192.168.1.7:80")
    # Persisted failure from a prior run's failed /summary push.
    host_cache.write_push_state("codex", ok=False, device_id="burnscope-z")

    daemon = CodexDaemon()
    # Fresh restart: no delivery baseline, no failures recorded yet this run.
    assert daemon._last_pushed_snapshot is None
    assert daemon._push_failures == {}

    diverged: list[PairedDevice] = []
    daemon._mark_health_ok(device, _in_sync_health_body(), diverged)

    # Reachability alone is not sync — no re-push flag, no false recovery.
    assert diverged == []
    state = host_cache.read_push_state("codex", device_id="burnscope-z")
    assert state is not None
    assert state["ok"] is False


async def test_health_loop_clears_stale_failure_so_derived_aggregate_recovers(
    monkeypatch, tmp_path
):
    """End-to-end: a device with a stale per-device ok=false that probes
    healthy and in-sync recovers, and the *derived* aggregate follows.

    History: rounds 2 and 3 of review were about a separately-*stored*
    aggregate going stale relative to the per-device files. That whole bug
    class is gone now — the aggregate is derived from the per-device files
    by `compute_aggregate_ok`, so it cannot disagree with them. This test
    keeps the integration angle: the health loop heals the per-device
    entry, and the derived aggregate reads True as a consequence.
    """
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    daemon._last_pushed_snapshot = _snapshot()
    host_cache.add_paired_device("codex", PairedDevice("dev-A", "10.0.0.5:80"))
    # Stale per-device failure from an earlier transient miss; aggregate
    # currently derives False because of it.
    host_cache.write_push_state("codex", ok=False, device_id="dev-A")
    assert host_cache.compute_aggregate_ok("codex") is False

    async def fake_fetch_health(host, client_id, client):
        return _in_sync_health_body()

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []

    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)
    from burnscope_client import pusher as pusher_mod

    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(200):
            if host_cache.compute_aggregate_ok("codex") is True:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("derived aggregate never recovered to true")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert host_cache.compute_aggregate_ok("codex") is True
    assert host_cache.read_push_state("codex", device_id="dev-A")["ok"] is True


async def test_health_loop_keeps_aggregate_red_while_push_pending(
    monkeypatch, tmp_path
):
    """A healthy probe must not flip the device green while a newer
    snapshot is still undelivered — so the derived aggregate stays red.

    Codex review P2 (round 3): when snapshot S2 failed to push to the
    device, `_push_failures[device] > 0` and `_mark_health_ok` must leave
    the per-device state ok=false even though the device probes healthy at
    the HTTP level (its newest snapshot is undelivered). The aggregate is
    now derived from the per-device files, so as long as `_mark_health_ok`
    holds that gate, the aggregate cannot read green while a push is
    pending — the round-3 invariant is preserved structurally.
    """
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    daemon._last_pushed_snapshot = _snapshot()  # S1 — what the firmware holds
    host_cache.add_paired_device("codex", PairedDevice("dev-A", "10.0.0.5:80"))
    # Newer snapshot S2 failed to deliver: pending failure + per-device
    # ok=false (the aggregate is derived from this, not stored).
    daemon._push_failures["dev-A"] = 1
    host_cache.write_push_state("codex", ok=False, device_id="dev-A")

    async def fake_fetch_health(host, client_id, client):
        return _in_sync_health_body()

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []

    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)
    from burnscope_client import pusher as pusher_mod

    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        # Let several cycles run; a buggy aggregate-true write would land.
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Newest snapshot still undelivered → per-device entry stays red...
    assert host_cache.read_push_state("codex", device_id="dev-A")["ok"] is False
    # ...so the derived aggregate is red too.
    assert host_cache.compute_aggregate_ok("codex") is False
