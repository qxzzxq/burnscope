"""Derive the per-agent identifier shipped in `X-BurnScope-Client-Id`.

Pure module. The only side effect is reading `~/.claude.json`.

Claude: prefers `oauthAccount.emailAddress`; falls back to top-level
        `userID` when the account is not yet OAuth-signed-in but Claude Code
        has assigned a local user ID.
Codex:  `account.email` from the app-server `account/read` response — the
        resolver lives in `codex_daemon.py` because it needs an open
        subprocess.

The value is sent plaintext over the LAN so the ESP32 can display the
operator's email/userID on screen. No hashing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CLAUDE_SETTINGS_FILE = Path.home() / ".claude.json"


class IdentityError(RuntimeError):
    """Raised when ~/.claude.json is missing, malformed, or has no identifier."""


def claude_user_identifier() -> str:
    """Return Claude's plaintext identifier from `~/.claude.json`.

    Resolution order:
      1. `oauthAccount.emailAddress` (preferred — human-readable).
      2. Top-level `userID` (fallback when emailAddress is absent).

    Raises `IdentityError` if neither yields a non-empty string.
    """
    data = _read_settings()

    oauth = data.get("oauthAccount")
    if isinstance(oauth, dict):
        email = oauth.get("emailAddress")
        if isinstance(email, str) and email:
            log.debug("resolved Claude identifier via oauthAccount.emailAddress")
            return email

    user_id = data.get("userID")
    if isinstance(user_id, str) and user_id:
        log.debug("resolved Claude identifier via userID fallback")
        return user_id

    raise IdentityError(
        f"{CLAUDE_SETTINGS_FILE} has neither "
        "`oauthAccount.emailAddress` nor a top-level `userID`"
    )


def redact_client_id(client_id: str) -> str:
    """Return a debug-safe form of a client_id (typically an email).

    Used by both the Claude statusline and the Codex daemon when logging
    so the operator's email/userID isn't leaked verbatim to log sinks.
    """
    if not client_id:
        return "<empty>"
    if "@" in client_id:
        local, _, domain = client_id.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}***@{domain}"
    head = client_id[:2] if len(client_id) > 2 else client_id[:1]
    return f"{head}*** ({len(client_id)} chars)"


def _read_settings() -> dict:
    try:
        raw = CLAUDE_SETTINGS_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdentityError(
            f"Could not read {CLAUDE_SETTINGS_FILE}: {exc}"
        ) from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise IdentityError(
            f"{CLAUDE_SETTINGS_FILE} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise IdentityError(
            f"{CLAUDE_SETTINGS_FILE} root is not a JSON object"
        )
    return parsed
