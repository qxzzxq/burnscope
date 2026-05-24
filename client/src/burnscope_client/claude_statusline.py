"""Claude Code statusline hook — short-lived, fired per assistant turn.

Two modes, selected by `--push`:

* **Default (foreground)** — Claude Code invokes this script with the session
  payload on stdin. The script must render the statusline to stdout and exit
  within Claude's 300ms debounce window or the next event will cancel it. To
  avoid blocking the rendered statusline on network I/O, the actual mDNS
  resolve + HTTP POST happens in a detached background child re-entering this
  same module with `--push`.

* **`--push` (detached child)** — reads the AgentSnapshot JSON from stdin,
  resolves the per-agent paired-device list (auto-pairing any unclaimed
  display on the LAN when the list is empty), fans out `POST /summary` to
  every paired device with the cached client_id, and writes the aggregate
  outcome to `~/.burnscope/last-push.claude` (drives the next foreground
  fire's ✓/✗ indicator) plus per-device outcomes for `burnscope status`.
  Devices that 401 are silently dropped from the agent's paired list.

See `docs/client-spec-v2.html` § 6 for the per-fire lifecycle.

Debug logging: set `BURNSCOPE_LOG_FILE=/path/to/file.log` and both the
foreground render and the detached `--push` child will append to it.
When unset, logging is silent (Claude Code discards script stderr).
"""

from __future__ import annotations

# Allow `python path/to/claude_statusline.py` for ad-hoc debugging in addition
# to the canonical `python -m burnscope_client.claude_statusline` invocation
# used by the installed Claude statusline hook.
if __package__ in (None, ""):
    import os
    import sys as _sys

    _sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    __package__ = "burnscope_client"

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402

from . import host_cache, identity  # noqa: E402
from ._log import configure_logging  # noqa: E402
from .discovery import discover_all  # noqa: E402
from .host_cache import PairedDevice  # noqa: E402
from .pusher import PushAuthError, PushError, push, push_to_all  # noqa: E402
from .schema import AgentSnapshot, SessionSnapshot  # noqa: E402

AGENT_NAME = "claude"
DISCOVERY_TIMEOUT_S = 4.0

log = logging.getLogger(__name__)

_INDICATOR_OK = "✓"
_INDICATOR_FAIL = "✗"
_INDICATOR_PENDING = "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="burnscope_client.claude_statusline")
    parser.add_argument(
        "--push",
        action="store_true",
        help="Detached-child mode: read AgentSnapshot from stdin and POST it.",
    )
    args = parser.parse_args(argv)
    configure_logging()
    log.debug("claude_statusline invoked (push=%s)", args.push)

    if args.push:
        return _push_mode()
    return _foreground_mode()


def _foreground_mode() -> int:
    """Render statusline; spawn detached child to push if data is present."""
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}

    prev = host_cache.read_push_state("claude")
    indicator = _indicator_for(prev)
    rate_limits = payload.get("rate_limits") or {}
    five = rate_limits.get("five_hour") or {}
    seven = rate_limits.get("seven_day") or {}
    log.debug(
        "foreground fire: prev=%s indicator=%s rate_limits=%s",
        prev,
        indicator,
        "present" if rate_limits else "absent",
    )

    # Always render first so Claude has something to display even if push fails.
    sys.stdout.write(_render_line(five, seven, indicator))
    sys.stdout.write("\n")
    sys.stdout.flush()

    snapshot = _build_snapshot(five, seven)
    if snapshot is None:
        log.debug("no usable rate_limits; skipping push")
        return 0

    log.debug(
        "spawning detached push child (sessions=%d)", len(snapshot.sessions)
    )
    _spawn_push_child(snapshot)
    return 0


def _render_line(five: dict, seven: dict, indicator: str) -> str:
    five_pct = _format_pct(five.get("used_percentage"))
    seven_pct = _format_pct(seven.get("used_percentage"))
    return f"5h {five_pct} · 7d {seven_pct} {indicator}"


def _format_pct(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.0f}%"
    return "—"


def _indicator_for(prev: dict | None) -> str:
    if prev is None:
        return _INDICATOR_PENDING
    return _INDICATOR_OK if prev.get("ok") else _INDICATOR_FAIL


def _build_snapshot(five: dict, seven: dict) -> AgentSnapshot | None:
    """Build an AgentSnapshot, or return None if rate_limits is empty.

    Both windows must have both a used_percentage and a resets_at to be
    included. If neither window is usable, no push happens this fire.
    """
    sessions: list[SessionSnapshot] = []
    for label, window in (("current", five), ("weekly", seven)):
        pct = window.get("used_percentage")
        resets = window.get("resets_at")
        if not isinstance(pct, (int, float)):
            continue
        if not isinstance(resets, int):
            continue
        sessions.append(SessionSnapshot(label, float(pct) / 100.0, int(resets)))
    if not sessions:
        return None
    return AgentSnapshot(
        agent="claude",
        captured_at=int(time.time()),
        sessions=sessions,
    )


def _spawn_push_child(snapshot: AgentSnapshot) -> None:
    """Fork a detached child to do the push. Don't wait on it."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "burnscope_client.claude_statusline", "--push"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(snapshot.to_dict()).encode())
        proc.stdin.close()
    except OSError as exc:
        log.warning("failed to hand snapshot to push child: %s", exc)


def _push_mode() -> int:
    """Detached child: resolve devices, fan out POST, write outcomes."""
    host_cache.migrate_legacy_host_file()

    try:
        raw = sys.stdin.read()
        snap_dict = json.loads(raw)
        snapshot = AgentSnapshot(
            agent=snap_dict["agent"],
            captured_at=snap_dict["captured_at"],
            sessions=[SessionSnapshot(**s) for s in snap_dict["sessions"]],
        )
    except (ValueError, KeyError, TypeError) as exc:
        log.error("invalid snapshot on stdin: %s", exc)
        host_cache.write_push_state(AGENT_NAME, ok=False)
        return 1

    client_id = _resolve_client_id()
    if client_id is None:
        host_cache.write_push_state(AGENT_NAME, ok=False)
        return 1

    return asyncio.run(_do_fanout(snapshot, client_id))


def _resolve_client_id() -> str | None:
    """Return the cached client_id, deriving from ~/.claude.json on miss."""
    cached = host_cache.read_client_id(AGENT_NAME)
    if cached is not None:
        log.debug("client_id cache hit: %s", identity.redact_client_id(cached))
        return cached
    log.debug("client_id cache miss; reading ~/.claude.json")
    try:
        derived = identity.claude_user_identifier()
    except identity.IdentityError as exc:
        log.error("could not derive client_id: %s", exc)
        return None
    host_cache.write_client_id(AGENT_NAME, derived)
    log.debug("client_id resolved and cached: %s", identity.redact_client_id(derived))
    return derived


async def _do_fanout(snapshot: AgentSnapshot, client_id: str) -> int:
    async with httpx.AsyncClient() as client:
        devices = await _resolve_paired_devices(snapshot, client_id, client)
        if not devices:
            log.warning("no paired devices and discovery found nothing claimable")
            host_cache.write_push_state(AGENT_NAME, ok=False)
            return 1

        results = await push_to_all(snapshot, devices, client_id, client)

    overall_ok = True
    for device_id, result in results.items():
        host_cache.write_push_state(
            AGENT_NAME, ok=result.ok, device_id=device_id
        )
        if result.kind == "auth":
            log.info("dropping %s from claude paired list (401)", device_id)
            host_cache.remove_paired_device(AGENT_NAME, device_id)
        if not result.ok:
            overall_ok = False

    host_cache.write_push_state(AGENT_NAME, ok=overall_ok)
    return 0 if overall_ok else 1


async def _resolve_paired_devices(
    snapshot: AgentSnapshot,
    client_id: str,
    client: httpx.AsyncClient,
) -> list[PairedDevice]:
    """Steady-state: return the cached list. First run: discover + claim."""
    cached = host_cache.load_paired_devices(AGENT_NAME)
    if cached:
        log.debug("paired-devices.%s hit (%d device(s))", AGENT_NAME, len(cached))
        return cached

    log.info("paired-devices.%s empty; running auto-pair discovery", AGENT_NAME)
    # Don't pre-filter by `paired_<agent>=0`: a device with the slot
    # already TOFU-bound to *us* will return 204 and we want to recover
    # it (migration from a wiped paired-devices.json). Devices owned by
    # someone else return 401 and are silently skipped below.
    discovered = await discover_all(timeout=DISCOVERY_TIMEOUT_S)
    if not discovered:
        log.info("auto-pair discovery found no claimable devices")
        return []

    claimed: list[PairedDevice] = []
    for device in discovered:
        candidate = PairedDevice(device_id=device.device_id, host=device.host)
        try:
            await push(snapshot, candidate.host, client_id, client)
        except PushAuthError:
            log.info("auto-pair skipped %s (401)", candidate.device_id)
            continue
        except PushError as exc:
            log.info("auto-pair skipped %s (transport): %s", candidate.device_id, exc)
            continue
        log.info("auto-pair claimed %s at %s", candidate.device_id, candidate.host)
        host_cache.add_paired_device(AGENT_NAME, candidate)
        host_cache.write_push_state(
            AGENT_NAME, ok=True, device_id=candidate.device_id
        )
        claimed.append(candidate)
    return claimed


if __name__ == "__main__":
    raise SystemExit(main())
