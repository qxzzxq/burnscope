"""Claude Code statusline hook — short-lived, fired per assistant turn.

Two modes, selected by `--push`:

* **Default (foreground)** — Claude Code invokes this script with the session
  payload on stdin. The script must render the statusline to stdout and exit
  within Claude's 300ms debounce window or the next event will cancel it. To
  avoid blocking the rendered statusline on network I/O, the actual mDNS
  resolve + HTTP POST happens in a detached background child re-entering this
  same module with `--push`.

* **`--push` (detached child)** — reads the AgentSnapshot JSON from stdin,
  resolves the host via the mDNS cache, derives the client_id from the
  Claude keyring credentials, POSTs to the ESP32, and writes the outcome to
  `~/.burnscope/last-push.claude` for the next foreground fire to render
  as the ✓/✗ indicator.

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
from .discovery import discover_esp32  # noqa: E402
from .pusher import PushAuthError, PushError, push  # noqa: E402
from .schema import AgentSnapshot, SessionSnapshot  # noqa: E402

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
    """Detached child: discover host, derive client_id, POST, write outcome."""
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
        host_cache.write_push_state("claude", ok=False)
        return 1

    client_id = host_cache.read_client_id("claude")
    if client_id is None:
        log.debug("client_id cache miss; deriving from credentials")
        try:
            client_id = identity.client_id_for_agent(
                "claude", identity.claude_org_uuid()
            )
        except identity.IdentityError as exc:
            log.error("could not derive client_id: %s", exc)
            host_cache.write_push_state("claude", ok=False)
            return 1
        host_cache.write_client_id("claude", client_id)
        log.debug("client_id derived and cached (%s…)", client_id[:8])
    else:
        log.debug("client_id cache hit (%s…)", client_id[:8])

    try:
        asyncio.run(_do_push(snapshot, client_id))
    except PushAuthError as exc:
        log.error("auth rejected by ESP32: %s", exc)
        host_cache.write_push_state("claude", ok=False)
        return 1
    except PushError as exc:
        log.warning("push failed, invalidating host cache: %s", exc)
        host_cache.invalidate_host()
        host_cache.write_push_state("claude", ok=False)
        return 1

    host_cache.write_push_state("claude", ok=True)
    return 0


async def _do_push(snapshot: AgentSnapshot, client_id: str) -> None:
    host = await _resolve_host()
    if host is None:
        raise PushError("mDNS discovery found no BurnScope ESP32 on the LAN")
    async with httpx.AsyncClient() as client:
        await push(snapshot, host, client_id, client)


async def _resolve_host() -> str | None:
    """Use the cached host if present; otherwise rediscover and cache."""
    cached = host_cache.load_host()
    if cached:
        log.debug("host cache hit: %s", cached)
        return cached
    log.debug("host cache miss; running mDNS discovery")
    found = await discover_esp32()
    if found:
        host_cache.store_host(found)
        log.debug("host cached: %s", found)
    return found


if __name__ == "__main__":
    raise SystemExit(main())
