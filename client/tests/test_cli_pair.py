"""Tests for the `burnscope pair` and updated `burnscope status` /
`burnscope pair-reset` CLI subcommands.
"""

from __future__ import annotations

import pytest

from burnscope_client import cli, host_cache
from burnscope_client.discovery import DiscoveredDevice
from burnscope_client.host_cache import PairedDevice


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))


def _stub_discover(monkeypatch, results):
    """Replace `discover_all` with a coroutine that returns `results`.

    `results` may be a list (used regardless of `agent` arg) or a dict
    keyed by agent name for per-agent control.
    """
    async def fake_discover_all(timeout=10.0, agent=None, zc=None):
        if isinstance(results, dict):
            return [
                d for d in results.get(agent, [])
                if not d.paired_for(agent)
            ]
        return [d for d in results if not d.paired_for(agent)]

    monkeypatch.setattr(cli, "discover_all", fake_discover_all)


# =================================================================== pair

def test_pair_skips_agent_without_cached_client_id(capsys, monkeypatch):
    _stub_discover(monkeypatch, [])
    host_cache.write_client_id("claude", "me@example.com")
    # codex has no cached client_id — must be skipped

    rc = cli.main(["pair"])
    out = capsys.readouterr().out
    assert "no cached client_id" in out
    assert "codex" in out
    # Exit code is 1 because at least one agent was skipped
    assert rc == 1


def test_pair_adds_discovered_free_devices(monkeypatch, capsys):
    host_cache.write_client_id("claude", "me@example.com")
    host_cache.write_client_id("codex",  "me@example.com")
    _stub_discover(
        monkeypatch,
        {
            "claude": [
                DiscoveredDevice("dev-x", "10.0.0.5:80", False, False),
                DiscoveredDevice("dev-y", "10.0.0.6:80", False, True),
            ],
            "codex":  [
                DiscoveredDevice("dev-x", "10.0.0.5:80", False, False),
            ],
        },
    )

    rc = cli.main(["pair"])
    assert rc == 0
    paired_claude = {d.device_id for d in host_cache.load_paired_devices("claude")}
    paired_codex  = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert paired_claude == {"dev-x", "dev-y"}
    assert paired_codex  == {"dev-x"}


def test_pair_with_agent_filter_only_runs_one_agent(monkeypatch):
    host_cache.write_client_id("claude", "me@example.com")
    host_cache.write_client_id("codex",  "me@example.com")
    _stub_discover(
        monkeypatch,
        {
            "claude": [DiscoveredDevice("dev-x", "10.0.0.5:80", False, False)],
            "codex":  [DiscoveredDevice("dev-x", "10.0.0.5:80", False, False)],
        },
    )

    rc = cli.main(["pair", "--agent", "claude"])
    assert rc == 0
    assert {d.device_id for d in host_cache.load_paired_devices("claude")} == {"dev-x"}
    # codex must NOT have been touched
    assert host_cache.load_paired_devices("codex") == []


def test_pair_is_idempotent_for_already_paired_devices(monkeypatch, capsys):
    host_cache.write_client_id("claude", "me@example.com")
    host_cache.write_client_id("codex",  "me@example.com")
    host_cache.add_paired_device("claude", PairedDevice("dev-x", "10.0.0.5:80"))
    # discover returns the same device — it's already in the list.
    _stub_discover(
        monkeypatch,
        {
            "claude": [DiscoveredDevice("dev-x", "10.0.0.5:80", False, False)],
            "codex":  [],
        },
    )

    cli.main(["pair"])
    out = capsys.readouterr().out
    # No new devices added on either agent.
    assert "[claude] no claimable devices" in out
    assert "[codex] no claimable devices" in out
    # Original entry still there, exactly once.
    devices = host_cache.load_paired_devices("claude")
    assert len(devices) == 1


# ============================================================ pair-reset

def test_pair_reset_wipes_paired_lists_and_client_ids(_isolate_state=None):
    host_cache.write_client_id("claude", "a@example.com")
    host_cache.write_client_id("codex",  "b@example.com")
    host_cache.add_paired_device("claude", PairedDevice("dev-1", "1.1.1.1:80"))
    host_cache.add_paired_device("codex",  PairedDevice("dev-2", "2.2.2.2:80"))

    rc = cli.main(["pair-reset"])
    assert rc == 0
    assert host_cache.read_client_id("claude") is None
    assert host_cache.read_client_id("codex")  is None
    assert host_cache.load_paired_devices("claude") == []
    assert host_cache.load_paired_devices("codex")  == []


# ================================================================ status

def test_status_shows_per_device_breakdown(capsys):
    host_cache.write_client_id("claude", "me@example.com")
    host_cache.add_paired_device("claude", PairedDevice("dev-1", "10.0.0.5:80"))
    host_cache.write_push_state("claude", ok=True, device_id="dev-1")
    host_cache.write_push_state("claude", ok=True)

    cli.main(["status"])
    out = capsys.readouterr().out
    assert "--- claude ---" in out
    assert "dev-1" in out
    assert "10.0.0.5:80" in out
    # codex has nothing — must say so cleanly.
    assert "--- codex ---" in out
    assert "<none>" in out  # at least one <none> line for the empty codex
