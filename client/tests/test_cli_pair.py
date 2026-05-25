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


def _stub_discover(monkeypatch, results, *, call_counter=None):
    """Replace `discover_all` with a coroutine returning `results` verbatim.

    Mirrors production `discover_all()` (without `agent=`): one
    `DiscoveredDevice` per unique device, with both `paired_claude` and
    `paired_codex` already populated. The CLI only invokes the unfiltered
    form now, so per-agent dict stubs would no longer reflect production.
    Pass `call_counter` (a one-element list) to assert the CLI doesn't
    re-browse mDNS per agent.
    """

    async def fake_discover_all(timeout=10.0, agent=None, zc=None):
        if call_counter is not None:
            call_counter[0] += 1
        assert agent is None, (
            "cli._pair must use unfiltered discovery — per-agent re-browse "
            "would double the mDNS wait. See PR #34 review."
        )
        return list(results)

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
        [
            DiscoveredDevice("dev-x", "10.0.0.5:80", False, False),
            DiscoveredDevice("dev-y", "10.0.0.6:80", False, True),
        ],
    )

    rc = cli.main(["pair"])
    assert rc == 0
    paired_claude = {d.device_id for d in host_cache.load_paired_devices("claude")}
    paired_codex  = {d.device_id for d in host_cache.load_paired_devices("codex")}
    # Unfiltered discovery returns all devices for both agents (#18).
    assert paired_claude == {"dev-x", "dev-y"}
    assert paired_codex  == {"dev-x", "dev-y"}


def test_pair_browses_mdns_at_most_once_across_agents(monkeypatch):
    # discover_all() blocks for the full timeout, so re-running it per
    # agent doubles the user-visible wait. Pin the single-browse contract.
    host_cache.write_client_id("claude", "me@example.com")
    host_cache.write_client_id("codex",  "me@example.com")
    call_counter = [0]
    _stub_discover(
        monkeypatch,
        [DiscoveredDevice("dev-x", "10.0.0.5:80", False, False)],
        call_counter=call_counter,
    )

    cli.main(["pair"])
    assert call_counter[0] == 1, (
        f"discover_all() was called {call_counter[0]} times; pair must reuse "
        "one browse across both agents"
    )


def test_pair_with_agent_filter_only_runs_one_agent(monkeypatch):
    host_cache.write_client_id("claude", "me@example.com")
    host_cache.write_client_id("codex",  "me@example.com")
    _stub_discover(
        monkeypatch,
        [DiscoveredDevice("dev-x", "10.0.0.5:80", False, False)],
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
        [DiscoveredDevice("dev-x", "10.0.0.5:80", False, False)],
    )

    cli.main(["pair"])
    out = capsys.readouterr().out
    # Claude already has dev-x — no new devices.
    assert "[claude] no claimable devices" in out
    # Codex discovers dev-x via unfiltered discovery (#18).
    assert "[codex] added 1 device" in out
    # Original entry still there, exactly once per agent.
    devices = host_cache.load_paired_devices("claude")
    assert len(devices) == 1
    assert len(host_cache.load_paired_devices("codex")) == 1


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


def test_pair_reset_warns_when_codex_daemon_supervisor_present(
    monkeypatch, capsys, tmp_path
):
    """When the codex daemon is installed (launchd plist or systemd
    unit on disk), pair-reset must print a daemon-restart warning so
    the user reloads it — otherwise stale in-memory state
    (_last_pushed_snapshot, failure counters) survives the reset and
    can cause spurious early eviction on freshly re-claimed devices
    (deep-review M-4).
    """
    # Fake a launchd plist on disk.
    plist = tmp_path / "com.burnscope.codex.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(cli, "LAUNCHD_PLIST_PATH", plist)
    # Make sure the systemd unit path doesn't accidentally also exist.
    monkeypatch.setattr(cli, "SYSTEMD_UNIT_PATH", tmp_path / "not-present.service")

    rc = cli.main(["pair-reset"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "codex daemon is installed" in out
    assert "launchctl" in out


def test_pair_reset_silent_when_no_supervisor_installed(monkeypatch, capsys, tmp_path):
    """If neither supervisor unit is on disk, pair-reset shouldn't
    print the daemon-restart paragraph — that just confuses users
    who never installed the codex daemon.
    """
    monkeypatch.setattr(cli, "LAUNCHD_PLIST_PATH", tmp_path / "absent.plist")
    monkeypatch.setattr(cli, "SYSTEMD_UNIT_PATH", tmp_path / "absent.service")

    rc = cli.main(["pair-reset"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "codex daemon" not in out
    assert "launchctl" not in out


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
