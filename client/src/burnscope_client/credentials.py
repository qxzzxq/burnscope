"""Load Claude Code's OAuth access token from local credential storage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_LINUX_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


class CredentialsError(RuntimeError):
    """Raised when the Claude Code OAuth token cannot be located."""


def load_claude_token(credentials_path: Path | None = None) -> str:
    """Return the user's Claude Code OAuth access token.

    On macOS the token lives in the user's login keychain under
    `Claude Code-credentials`. On other platforms it lives in
    `~/.claude/.credentials.json`. `credentials_path` overrides the latter
    (useful for tests).
    """
    if sys.platform == "darwin" and credentials_path is None:
        blob = _read_macos_keychain()
    else:
        blob = _read_credentials_file(credentials_path or DEFAULT_LINUX_CREDENTIALS)
    return _extract_token(blob)


def _read_macos_keychain() -> str:
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            os.environ.get("USER", ""),
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise CredentialsError(
            f"Failed to read '{KEYCHAIN_SERVICE}' from macOS keychain "
            f"(rc={result.returncode}). Is Claude Code logged in?"
        )
    return result.stdout


def _read_credentials_file(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError as exc:
        raise CredentialsError(
            f"Credentials file not found at {path}. Is Claude Code logged in?"
        ) from exc


def _extract_token(blob: str) -> str:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise CredentialsError(f"Credentials are not valid JSON: {exc}") from exc

    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken") or data.get("accessToken")
    if not token:
        raise CredentialsError("No accessToken found in credentials blob")
    return token
