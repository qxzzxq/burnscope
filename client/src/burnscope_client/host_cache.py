"""File-backed state shared between the two collectors.

Files in `~/.burnscope/` (mode 0700):
  - `paired-devices.<agent>.json` — list of `{device_id, host}` entries
    representing displays whose `<agent>` NVS slot is bound to this
    laptop's identifier. One file per agent; the Claude collector never
    reads or writes the Codex file (and vice versa).
  - `last-push.<agent>` — `{"ok": bool, "at": int}`, the aggregate
    outcome of the most recent fan-out push. `ok` is true iff every
    paired device responded 204. Drives the ✓/✗ indicator on the next
    statusline fire.
  - `last-push.<agent>.<device_id>` — same shape, scoped to one device.
    Lets `burnscope status` surface which specific display is failing.
  - `client-id.<agent>` — plaintext per-agent identifier (email or
    userID), cached so the Claude statusline doesn't re-read
    `~/.claude.json` on every fire.

A v1 single-host `host` file is silently deleted on first access — the
auto-pair-on-first-run path will re-claim the device via TOFU.

All writes are atomic via `NamedTemporaryFile` + `os.replace` so
concurrent statusline fires can't corrupt any file. State directory is
overridable via `BURNSCOPE_STATE_DIR` for tests.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger(__name__)

_LAST_PUSH_PREFIX = "last-push."
_CLIENT_ID_PREFIX = "client-id."
_PAIRED_DEVICES_PREFIX = "paired-devices."
_RECONCILE_PREFIX = "mdns-reconcile."
_LEGACY_HOST_FILENAME = "host"

# Default per-(agent, device_id) cooldown between mDNS reconciliation
# browses. The plan caps the firmware-side cost: a powered-off device
# shouldn't be able to trigger a fresh full-LAN browse on every
# statusline fire (~once a turn) or every push/health failure event.
# Overridable for tests and ops via BURNSCOPE_MDNS_RECONCILE_COOLDOWN_S.
RECONCILE_COOLDOWN_DEFAULT_S = 60.0

_MAX_CLIENT_ID_LEN = 254  # RFC 5321 email cap; leaves headroom for userIDs.
_MAX_DEVICE_ID_LEN = 64
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UPGRADE_HINT_FILENAME = "v1-upgrade-hint"


@dataclass(frozen=True)
class PairedDevice:
    """One ESP32 display claimed by this laptop for a given agent.

    `device_id` matches `DiscoveredDevice.device_id` — derived from
    the mDNS hostname (`info.server`, e.g. `burnscope-a1b2`), which
    the firmware seeds from the WiFi MAC. It is stable across IP
    changes and unique per unit, so it's safe as the dictionary key
    on disk. `host` is the current `host:port` the device was last
    seen at.
    """

    device_id: str
    host: str


def state_dir() -> Path:
    """Resolve the state dir (honors `BURNSCOPE_STATE_DIR`)."""
    override = os.environ.get("BURNSCOPE_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".burnscope"


def _ensure_dir() -> Path:
    d = state_dir()
    d.mkdir(mode=0o700, exist_ok=True, parents=True)
    return d


def _safe_device_id(raw: str) -> str | None:
    """Validate `device_id` for filesystem safety.

    Returns the id unchanged if it passes the allowlist + length cap;
    returns None if it contains path separators, traversal, or other
    unsafe characters (malicious mDNS responder on the LAN).
    """
    if not raw or len(raw) > _MAX_DEVICE_ID_LEN:
        return None
    if not _DEVICE_ID_RE.match(raw):
        return None
    return raw


def _lockfile_path(agent: str) -> Path:
    return state_dir() / f"paired-devices.{agent}.lock"


@contextmanager
def _with_lock(agent: str) -> Iterator[None]:
    """Hold an exclusive flock on the per-agent paired-devices lockfile.

    Serializes read-modify-write sequences so concurrent statusline
    children cannot clobber each other's updates via last-writer-wins.
    """
    path = _lockfile_path(agent)
    _ensure_dir()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def unlink_push_state(agent: str, device_id: str) -> None:
    """Delete the per-device last-push file. Idempotent."""
    safe_id = _safe_device_id(device_id)
    if safe_id is None:
        return
    path = _last_push_path(agent, safe_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def clear_push_state(agent: str) -> None:
    """Delete every last-push file for `agent` (aggregate + per-device)."""
    d = state_dir()
    prefix = f"{_LAST_PUSH_PREFIX}{agent}"
    for path in d.glob(f"{prefix}*"):
        try:
            path.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, data: str) -> None:
    d = _ensure_dir()
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            # fsync before close so the renamed file is durable across
            # a host power-loss / kernel panic, not just a process crash.
            # `os.replace` itself is atomic on POSIX, but only with
            # respect to the *rename*; the data still has to reach the
            # disk.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ----------------------------------------------------------- paired devices

def _paired_devices_path(agent: str) -> Path:
    return state_dir() / f"{_PAIRED_DEVICES_PREFIX}{agent}.json"


def load_paired_devices(agent: str) -> list[PairedDevice]:
    """Return this agent's paired devices, or `[]` if absent/malformed."""
    path = _paired_devices_path(agent)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("paired-devices.%s contained invalid JSON; ignoring", agent)
        return []
    if not isinstance(parsed, list):
        return []
    out: list[PairedDevice] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        device_id = entry.get("device_id")
        host = entry.get("host")
        if not isinstance(device_id, str) or not isinstance(host, str):
            continue
        if not device_id or not host:
            continue
        if _safe_device_id(device_id) is None:
            log.warning(
                "paired-devices.%s: skipping unsafe device_id %r", agent, device_id
            )
            continue
        out.append(PairedDevice(device_id=device_id, host=host))
    return out


def save_paired_devices(agent: str, devices: list[PairedDevice]) -> None:
    """Atomically replace the paired-device list for `agent`."""
    payload = json.dumps([asdict(d) for d in devices])
    _atomic_write(_paired_devices_path(agent), payload)


def add_paired_device(agent: str, device: PairedDevice) -> None:
    """Insert or update one device in the agent's paired list.

    Dedupes by `device_id`. If the device is already known under the
    same identifier, its stored `host` is updated to reflect the latest
    IP / port we saw it at.

    Serialized via flock so concurrent statusline children cannot
    clobber each other's updates.
    """
    if _safe_device_id(device.device_id) is None:
        log.warning("add_paired_device: rejecting unsafe device_id %r", device.device_id)
        return
    with _with_lock(agent):
        existing = load_paired_devices(agent)
        updated = [d for d in existing if d.device_id != device.device_id]
        updated.append(device)
        save_paired_devices(agent, updated)


def update_paired_device_host(agent: str, device_id: str, host: str) -> bool:
    """Update one paired device's `host` in-place. Never inserts.

    Returns True iff the device was present and its host was rewritten;
    False if the device is no longer paired (so a concurrent
    `/summary` 401 or `pair-reset` already removed it).

    This is the narrow update-only sibling of `add_paired_device`. mDNS
    reconciliation paths must use it instead of `add_paired_device` so a
    stale browse that started before a concurrent removal cannot
    resurrect a forgotten pairing. The load → check → save sequence runs
    inside the per-agent flock for serializability against other
    statusline children and the codex daemon.

    Unsafe `device_id` values are rejected (return False) to match the
    rest of this module's resilient policy on malicious mDNS responders.
    """
    if _safe_device_id(device_id) is None:
        log.warning(
            "update_paired_device_host: rejecting unsafe device_id %r", device_id
        )
        return False
    with _with_lock(agent):
        existing = load_paired_devices(agent)
        for idx, current in enumerate(existing):
            if current.device_id == device_id:
                if current.host == host:
                    return True
                existing[idx] = PairedDevice(device_id=device_id, host=host)
                save_paired_devices(agent, existing)
                return True
        return False


def remove_paired_device(agent: str, device_id: str) -> None:
    """Drop the device with this `device_id`. No-op if not present.

    Serialized via flock so concurrent 401-drop paths cannot race.
    Also unlinks the per-device `last-push.<agent>.<device_id>` file
    so orphan push-state files don't accumulate. The unlink stays
    inside the lock so a concurrent statusline child can't write a
    fresh per-device file between save-paired and unlink and leave
    an orphan visible to `burnscope status`.
    """
    with _with_lock(agent):
        existing = load_paired_devices(agent)
        filtered = [d for d in existing if d.device_id != device_id]
        if len(filtered) == len(existing):
            return
        save_paired_devices(agent, filtered)
        unlink_push_state(agent, device_id)


def clear_paired_devices(agent: str) -> None:
    """Remove the agent's paired-device file entirely."""
    path = _paired_devices_path(agent)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------- mDNS reconciliation throttle

def _reconcile_path(agent: str) -> Path:
    return state_dir() / f"{_RECONCILE_PREFIX}{agent}.json"


def _reconcile_cooldown_s() -> float:
    raw = os.environ.get("BURNSCOPE_MDNS_RECONCILE_COOLDOWN_S")
    if raw is None:
        return RECONCILE_COOLDOWN_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "BURNSCOPE_MDNS_RECONCILE_COOLDOWN_S=%r is not a float; using default",
            raw,
        )
        return RECONCILE_COOLDOWN_DEFAULT_S
    return max(0.0, value)


def _load_reconcile_state(agent: str) -> dict[str, float]:
    path = _reconcile_path(agent)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("%s contained invalid JSON; resetting", path.name)
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or _safe_device_id(key) is None:
            continue
        if not isinstance(value, (int, float)):
            continue
        out[key] = float(value)
    return out


def claim_reconcile_slots(
    agent: str,
    device_ids: Iterable[str],
    *,
    now: float | None = None,
    cooldown_s: float | None = None,
) -> set[str]:
    """Atomically filter `device_ids` to the ones whose reconciliation
    cooldown has expired, and mark each as just-reconciled.

    Reconciliation cost (a full LAN mDNS browse) is fixed-per-call, so
    the per-(agent, device_id) cooldown caps how often a single failing
    device can drive new browses. The file-backed map survives across
    short-lived statusline children and across daemon restarts, so
    cooldown state isn't lost on process death.

    The read-check-write sequence runs inside the per-agent flock for
    serializability against concurrent claimants. Two statusline children
    racing the same device get exactly one winner — the loser sees its
    own just-written timestamp and reports the device as in-cooldown.

    Unsafe `device_id` values are silently dropped (return set excludes
    them) to match the rest of this module's policy on malicious mDNS
    responders.

    Returns the set the caller should now browse for; entries inside
    their cooldown window are omitted.
    """
    requested = [d for d in device_ids if _safe_device_id(d) is not None]
    if not requested:
        return set()
    now_ts = float(now) if now is not None else time.time()
    cooldown = float(cooldown_s) if cooldown_s is not None else _reconcile_cooldown_s()
    eligible: set[str] = set()
    with _with_lock(agent):
        state = _load_reconcile_state(agent)
        for device_id in requested:
            last_ts = state.get(device_id)
            if last_ts is None or (now_ts - last_ts) >= cooldown:
                eligible.add(device_id)
                state[device_id] = now_ts
        if eligible:
            _atomic_write(_reconcile_path(agent), json.dumps(state))
    return eligible


def clear_reconcile_state(agent: str) -> None:
    """Drop the per-agent reconcile cooldown map. Idempotent.

    Called by `pair-reset` so a fresh re-pair after wiping local state
    doesn't have to wait for cooldown timestamps that referenced
    devices we no longer trust.
    """
    path = _reconcile_path(agent)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ----------------------------------------------------------- last-push state

def _last_push_path(agent: str, device_id: str | None) -> Path:
    if device_id is None:
        return state_dir() / f"{_LAST_PUSH_PREFIX}{agent}"
    safe = _safe_device_id(device_id)
    if safe is None:
        raise ValueError(f"unsafe device_id for filesystem path: {device_id!r}")
    return state_dir() / f"{_LAST_PUSH_PREFIX}{agent}.{safe}"


def write_push_state(agent: str, ok: bool, *, device_id: str | None = None) -> None:
    """Write the aggregate (`device_id=None`) or per-device push outcome.

    Per-device writes for an unsafe `device_id` (malicious mDNS
    responder on the LAN) are silently dropped so the long-lived codex
    daemon and the claude statusline child stay resilient.
    """
    if device_id is not None and _safe_device_id(device_id) is None:
        log.warning("write_push_state: skipping unsafe device_id %r", device_id)
        return
    payload = json.dumps({"ok": bool(ok), "at": int(time.time())})
    _atomic_write(_last_push_path(agent, device_id), payload)


def bump_push_failures(agent: str, device_id: str) -> int:
    """Atomically increment the per-device `consecutive_failures` counter.

    Returns the new count. Also writes `ok=False, at=now()` so
    `burnscope status` reflects the latest outcome.

    Stateless callers (the claude statusline `--push` child) use this
    to make the counter visible across short-lived process lifetimes
    so `burnscope status` can surface degraded devices. Per the mDNS
    resilience plan the counter is diagnostic-only — only `/summary`
    401 may remove a pairing. Serialized via flock so two concurrent
    statusline children can't both miss each other's increments.

    Unsafe `device_id` values yield 0 silently (matches the rest of
    this module's resilient policy on malicious mDNS responders).
    """
    if _safe_device_id(device_id) is None:
        log.warning("bump_push_failures: skipping unsafe device_id %r", device_id)
        return 0
    with _with_lock(agent):
        existing = read_push_state(agent, device_id=device_id) or {}
        new_count = int(existing.get("consecutive_failures", 0) or 0) + 1
        payload = json.dumps({
            "ok": False,
            "at": int(time.time()),
            "consecutive_failures": new_count,
        })
        _atomic_write(_last_push_path(agent, device_id), payload)
    return new_count


def reset_push_failures(agent: str, device_id: str) -> None:
    """Record a successful push and clear the failure counter.

    Atomic — overwrites the per-device file with `ok=True, at=now(),
    consecutive_failures=0`. Used by stateless callers in place of
    `write_push_state(..., ok=True, ...)` to also clear the counter
    that `bump_push_failures` advanced.
    """
    if _safe_device_id(device_id) is None:
        log.warning("reset_push_failures: skipping unsafe device_id %r", device_id)
        return
    payload = json.dumps({
        "ok": True,
        "at": int(time.time()),
        "consecutive_failures": 0,
    })
    _atomic_write(_last_push_path(agent, device_id), payload)


def read_push_state(agent: str, *, device_id: str | None = None) -> dict | None:
    """Return the parsed last-push state, or None if missing/malformed.

    Unsafe `device_id` values yield None rather than raising, matching
    the resilient policy in `write_push_state`.
    """
    if device_id is not None and _safe_device_id(device_id) is None:
        return None
    path = _last_push_path(agent, device_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("%s contained invalid JSON", path.name)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ------------------------------------------------------------- client_id cache

def read_client_id(agent: str) -> str | None:
    """Return the cached per-agent identifier, or None if missing/invalid."""
    path = state_dir() / f"{_CLIENT_ID_PREFIX}{agent}"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    except UnicodeError:
        log.warning("client-id.%s contained non-UTF-8 data; ignoring", agent)
        return None
    if not raw or len(raw) > _MAX_CLIENT_ID_LEN or any(ord(c) < 0x20 for c in raw):
        log.warning("client-id.%s contained malformed identifier; ignoring", agent)
        return None
    return raw


def write_client_id(agent: str, client_id: str) -> None:
    """Persist the derived client_id atomically."""
    _atomic_write(state_dir() / f"{_CLIENT_ID_PREFIX}{agent}", client_id)


def invalidate_client_id(agent: str) -> None:
    """Drop the cached client_id. Idempotent."""
    path = state_dir() / f"{_CLIENT_ID_PREFIX}{agent}"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ----------------------------------------------------------------- migration

def migrate_legacy_host_file() -> None:
    """Delete the v1 single-host `host` file if it still exists.

    Writes a one-shot upgrade hint so `burnscope status` can surface a
    recovery message: the v1→v2 client_id format change (hash → plaintext
    email) means every previously-paired device will 401 until the user
    factory-resets it. Idempotent.
    """
    path = state_dir() / _LEGACY_HOST_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    log.info("removed legacy single-host cache file %s", path)
    hint_path = state_dir() / _UPGRADE_HINT_FILENAME
    hint_path.write_text(
        "v1→v2 upgrade: client_id format changed (hash → plaintext email). "
        "Previously-paired devices will 401 until factory-reset. "
        "Run `burnscope pair` after resetting each device.\n",
        encoding="utf-8",
    )


def read_upgrade_hint() -> str | None:
    """Return the v1→v2 upgrade hint if present, or None."""
    path = state_dir() / _UPGRADE_HINT_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def clear_upgrade_hint() -> None:
    """Delete the v1→v2 upgrade hint. Idempotent."""
    path = state_dir() / _UPGRADE_HINT_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass
