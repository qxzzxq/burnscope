"""Tests for supervisor-unit rendering (launchd plist, systemd unit).

Locks in that the PATH for the spawned daemon includes the directory of
the user's `codex` binary — otherwise the daemon can't exec it because
launchd / systemd-user start with a minimal PATH that excludes
/opt/homebrew/bin, ~/.cargo/bin, ~/.local/bin, etc.
"""

from __future__ import annotations

from burnscope_client import cli


def test_supervisor_path_prepends_codex_dir(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/custom/bin/codex")
    path = cli._supervisor_path()
    parts = path.split(":")
    assert parts[0] == "/opt/custom/bin"
    # No duplicates.
    assert len(parts) == len(set(parts))


def test_supervisor_path_without_codex_is_still_usable(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    path = cli._supervisor_path()
    assert "/usr/bin" in path.split(":")
    assert "/bin" in path.split(":")


def test_launchd_plist_includes_path(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/homebrew/bin/codex")
    plist = cli._render_launchd_plist()
    assert "<key>EnvironmentVariables</key>" in plist
    assert "<key>PATH</key>" in plist
    assert "/opt/homebrew/bin" in plist


def test_systemd_unit_includes_path(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/home/u/.cargo/bin/codex")
    unit = cli._render_systemd_unit()
    assert "Environment=" in unit
    assert "PATH=" in unit
    assert "/home/u/.cargo/bin" in unit
