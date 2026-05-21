"""Derive the per-agent `X-BurnScope-Client-Id` from local credential material.

Pure module. The only side effects are reading the keyring (where available)
and the plaintext credential file fallback. See `docs/client-spec-v2.html` § 3.

Claude:  organizationUuid → sha256("burnscope:claude:<uuid>")
Codex:   email (from app-server `account/read`, looked up at daemon bootstrap)
         → sha256("burnscope:codex:<email>")

The Codex resolver lives in `codex_daemon.py` because it needs an open
app-server connection. This module only provides the Claude resolver and the
shared hash helper.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"


class IdentityError(RuntimeError):
    """Raised when no credential source yields the required identifier."""


def client_id_for_agent(agent: str, raw: str) -> str:
    """Return the namespaced SHA-256 hex of `raw` under `agent`.

    The `"burnscope:"` namespace prefix prevents collisions with hashes of the
    same identifier produced for other purposes.
    """
    payload = f"burnscope:{agent.lower()}:{raw}".encode()
    return hashlib.sha256(payload).hexdigest()


def claude_org_uuid() -> str:
    """Return Claude's `organizationUuid` from keyring or the fallback file.

    Resolution order:
      1. System keyring entry `Claude Code-credentials` (macOS Keychain,
         Linux Secret Service via `libsecret`).
      2. Plaintext file `~/.claude/.credentials.json` — written by Claude Code
         when no keyring is available.

    Raises `IdentityError` if both sources fail or the JSON lacks the field.
    """
    blob = _try_keyring()
    if blob is None:
        blob = _try_file()
    if blob is None:
        raise IdentityError(
            "No Claude credentials found in keyring or "
            f"{CLAUDE_CREDENTIALS_FILE}"
        )

    uuid = _extract_org_uuid(blob)
    if not uuid:
        raise IdentityError(
            "Claude credentials JSON did not contain a non-empty "
            "`organizationUuid` field"
        )
    return uuid


def _try_keyring() -> dict | None:
    try:
        import keyring  # imported lazily — optional dependency at runtime
    except ImportError:
        return None
    try:
        raw = keyring.get_password(CLAUDE_KEYCHAIN_SERVICE, getpass.getuser())
    except Exception as exc:  # keyring backends raise their own exceptions
        log.debug("keyring lookup failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        log.warning("keyring entry was not valid JSON: %s", exc)
        return None


def _try_file() -> dict | None:
    try:
        raw = CLAUDE_CREDENTIALS_FILE.read_text()
    except OSError as exc:
        log.debug("credentials file unavailable: %s", exc)
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        log.warning("credentials file was not valid JSON: %s", exc)
        return None


def _extract_org_uuid(blob: dict) -> str | None:
    """Pull `organizationUuid` out of one of the documented shapes.

    Claude Code has shipped two layouts: top-level (`{"organizationUuid": ...}`)
    and nested under `claudeAiOauth` (`{"claudeAiOauth": {"organizationUuid":
    ...}}`). Try both; whichever wins.
    """
    direct = blob.get("organizationUuid")
    if isinstance(direct, str) and direct:
        return direct
    nested = blob.get("claudeAiOauth")
    if isinstance(nested, dict):
        val = nested.get("organizationUuid")
        if isinstance(val, str) and val:
            return val
    return None
