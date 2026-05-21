"""Installer, status, and pair-reset CLI. Not a daemon entry point.

Subcommands:

  burnscope install   {claude,codex}
  burnscope uninstall {claude,codex}
  burnscope status
  burnscope pair-reset

Claude install is OS-agnostic (just patches `~/.claude/settings.json`).
Codex install dispatches on the host OS: launchd on macOS, systemd --user
on Linux, manual-instructions everywhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import host_cache

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_STATUSLINE_COMMAND = f"{sys.executable} -m burnscope_client.claude_statusline"

LAUNCHD_LABEL = "com.burnscope.codex"
LAUNCHD_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
)

SYSTEMD_UNIT_NAME = "burnscope-codex.service"
SYSTEMD_UNIT_PATH = (
    Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
)

# launchd and systemd-user start processes with a minimal PATH that excludes
# Homebrew, ~/.cargo/bin, ~/.local/bin, etc. — so the daemon can't exec
# `codex` even if it's on the user's shell PATH. We bake a PATH into the
# supervisor unit at install time: the dir of whatever `codex` resolves to
# now, plus OS-appropriate fallbacks.
_PATH_FALLBACKS_DARWIN = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
_PATH_FALLBACKS_LINUX = (
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / ".cargo" / "bin"),
    str(Path.home() / ".npm-global" / "bin"),
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/local/sbin",
    "/usr/sbin",
    "/sbin",
)


def _supervisor_path() -> str:
    """Return the PATH to bake into the launchd/systemd unit.

    Prepends the directory of `codex` (if found on the current PATH) to the
    OS-appropriate fallback list, with duplicates removed.
    """
    fallbacks = (
        _PATH_FALLBACKS_LINUX
        if sys.platform.startswith("linux")
        else _PATH_FALLBACKS_DARWIN
    )
    codex_path = shutil.which("codex")
    parts: list[str] = []
    if codex_path:
        parts.append(str(Path(codex_path).parent))
    for p in fallbacks:
        if p not in parts:
            parts.append(p)
    return ":".join(parts)


# ============================================================ argparse setup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="burnscope")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="Install a collector")
    p_install.add_argument("agent", choices=["claude", "codex"])

    p_uninstall = sub.add_parser("uninstall", help="Uninstall a collector")
    p_uninstall.add_argument("agent", choices=["claude", "codex"])

    sub.add_parser("status", help="Print local wiring status")
    sub.add_parser("pair-reset", help="Forget the cached ESP32 host")

    args = parser.parse_args(argv)

    if args.cmd == "install":
        return _install(args.agent)
    if args.cmd == "uninstall":
        return _uninstall(args.agent)
    if args.cmd == "status":
        return _status()
    if args.cmd == "pair-reset":
        host_cache.invalidate_host()
        for agent in ("claude", "codex"):
            host_cache.invalidate_client_id(agent)
        print("Forgot cached ESP32 host and per-agent client_ids.")
        return 0
    return 1


# ==================================================================== claude


def _install_claude() -> int:
    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings = _read_json(CLAUDE_SETTINGS_PATH) or {}
    desired = {
        "type": "command",
        "command": CLAUDE_STATUSLINE_COMMAND,
        "padding": 0,
    }
    if settings.get("statusLine") == desired:
        print(f"Claude statusline already configured at {CLAUDE_SETTINGS_PATH}.")
        return 0
    settings["statusLine"] = desired
    _write_json(CLAUDE_SETTINGS_PATH, settings)
    print(f"Configured Claude statusline at {CLAUDE_SETTINGS_PATH}.")
    return 0


def _uninstall_claude() -> int:
    settings = _read_json(CLAUDE_SETTINGS_PATH)
    if not settings or "statusLine" not in settings:
        print("Claude statusline was not configured.")
        return 0
    del settings["statusLine"]
    _write_json(CLAUDE_SETTINGS_PATH, settings)
    print("Removed Claude statusline configuration.")
    return 0


# ===================================================================== codex


def _install_codex() -> int:
    if sys.platform == "darwin":
        return _install_codex_launchd()
    if sys.platform.startswith("linux"):
        return _install_codex_systemd()
    return _install_codex_manual()


def _uninstall_codex() -> int:
    if sys.platform == "darwin":
        return _uninstall_codex_launchd()
    if sys.platform.startswith("linux"):
        return _uninstall_codex_systemd()
    print("No automatic uninstall on this platform.")
    return 0


def _install_codex_launchd() -> int:
    LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = _render_launchd_plist()
    LAUNCHD_PLIST_PATH.write_text(plist)
    uid = os.getuid()
    _run(["launchctl", "bootout", f"gui/{uid}", str(LAUNCHD_PLIST_PATH)], check=False)
    rc = _run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCHD_PLIST_PATH)],
        check=False,
    )
    if rc != 0:
        print(
            f"launchctl bootstrap failed (rc={rc}). Plist written at "
            f"{LAUNCHD_PLIST_PATH} — try `launchctl bootstrap gui/{uid} "
            f"{LAUNCHD_PLIST_PATH}` manually."
        )
        return rc
    print(f"Loaded {LAUNCHD_LABEL} via launchd.")
    return 0


def _uninstall_codex_launchd() -> int:
    uid = os.getuid()
    if LAUNCHD_PLIST_PATH.exists():
        _run(
            ["launchctl", "bootout", f"gui/{uid}", str(LAUNCHD_PLIST_PATH)],
            check=False,
        )
        LAUNCHD_PLIST_PATH.unlink()
        print(f"Removed {LAUNCHD_PLIST_PATH}.")
    else:
        print("launchd plist was not present.")
    return 0


def _render_launchd_plist() -> str:
    program_args = "".join(
        f"\n        <string>{a}</string>"
        for a in (sys.executable, "-m", "burnscope_client.codex_daemon")
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{program_args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{_supervisor_path()}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.burnscope/codex.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.burnscope/codex.stderr.log</string>
</dict>
</plist>
"""


def _install_codex_systemd() -> int:
    SYSTEMD_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEMD_UNIT_PATH.write_text(_render_systemd_unit())
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    rc = _run(
        ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],
        check=False,
    )
    if rc != 0:
        print(
            f"systemctl --user enable failed (rc={rc}). Unit written at "
            f"{SYSTEMD_UNIT_PATH} — try `systemctl --user enable --now "
            f"{SYSTEMD_UNIT_NAME}` manually."
        )
        return rc
    print(f"Enabled {SYSTEMD_UNIT_NAME} via systemd --user.")
    return 0


def _uninstall_codex_systemd() -> int:
    _run(
        ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME],
        check=False,
    )
    if SYSTEMD_UNIT_PATH.exists():
        SYSTEMD_UNIT_PATH.unlink()
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        print(f"Removed {SYSTEMD_UNIT_PATH}.")
    else:
        print("systemd unit was not present.")
    return 0


def _render_systemd_unit() -> str:
    return f"""[Unit]
Description=BurnScope Codex daemon
After=default.target

[Service]
Environment=PATH={_supervisor_path()}
ExecStart={sys.executable} -m burnscope_client.codex_daemon
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _install_codex_manual() -> int:
    print(
        "Automatic supervisor install is only wired up for macOS (launchd) "
        "and Linux (systemd --user). To run the daemon manually:"
    )
    print(f"  {sys.executable} -m burnscope_client.codex_daemon")
    return 1


# ==================================================================== status


def _status() -> int:
    print(f"State directory: {host_cache.state_dir()}")
    print(f"Cached host:     {host_cache.load_host() or '<none>'}")

    for agent in ("claude", "codex"):
        state = host_cache.read_push_state(agent)
        cid = host_cache.read_client_id(agent)
        cid_short = f"{cid[:12]}…" if cid else "<not cached>"
        print(f"Last push {agent}: {state or '<none>'}")
        print(f"Client-id {agent}: {cid_short}")

    claude_settings = _read_json(CLAUDE_SETTINGS_PATH) or {}
    sl = claude_settings.get("statusLine")
    print(
        "Claude statusline: "
        + (
            "configured"
            if isinstance(sl, dict) and sl.get("command") == CLAUDE_STATUSLINE_COMMAND
            else "not configured"
        )
    )

    print(f"Codex supervisor:  {_codex_supervisor_status()}")
    return 0


def _codex_supervisor_status() -> str:
    if sys.platform == "darwin":
        if not LAUNCHD_PLIST_PATH.exists():
            return "launchd plist not installed"
        rc = _run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False, quiet=True)
        return "loaded" if rc == 0 else "plist present but not loaded"
    if sys.platform.startswith("linux"):
        if not SYSTEMD_UNIT_PATH.exists():
            return "systemd unit not installed"
        rc = _run(
            ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT_NAME],
            check=False,
            quiet=True,
        )
        return "active" if rc == 0 else "unit present but not active"
    return "no supervisor on this platform"


# ============================================================== dispatchers


def _install(agent: str) -> int:
    return _install_claude() if agent == "claude" else _install_codex()


def _uninstall(agent: str) -> int:
    return _uninstall_claude() if agent == "claude" else _uninstall_codex()


# ================================================================== helpers


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _run(cmd: list[str], *, check: bool, quiet: bool = False) -> int:
    if shutil.which(cmd[0]) is None:
        if not quiet:
            print(f"command not found: {cmd[0]}")
        return 127
    out = subprocess.DEVNULL if quiet else None
    rc = subprocess.run(cmd, stdout=out, stderr=out).returncode
    if check and rc != 0:
        raise SystemExit(rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
