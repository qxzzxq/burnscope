"""Base `Credential` class and shared reading capabilities.

Each `Agent` subclass declares its own concrete `*Credential` frozen
dataclass that inherits from `Credential` and overrides `load()` with
the path/service args it needs. The base class provides the reusable
*reading capabilities*:

- `_read_keychain(service)` — macOS keychain query
- `_read_file(path)`        — JSON file on disk
- `_parse_json(blob, src)`  — JSON parse with a friendly error

Subclasses compose those primitives inside their own `load()`,
extracting whichever fields the agent needs.
"""

from __future__ import annotations

import json
import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class CredentialsError(RuntimeError):
    """Raised when an agent's auth credentials cannot be located or parsed."""


class Credential(ABC):
    """Base class for an agent's credential blob.

    Subclasses are typically `@dataclass(frozen=True)` holding the
    extracted fields, with a `load()` classmethod that calls one of
    the protected readers below and parses the result into those
    fields.
    """

    @classmethod
    @abstractmethod
    def load(cls, **kwargs: Any) -> "Credential":
        """Read and parse this credential from local storage.

        Each subclass defines its own kwargs (paths, service names).
        Raises `CredentialsError` if the credential cannot be found
        or parsed.
        """

    # ---- reading capabilities ------------------------------------------------

    @staticmethod
    def _read_keychain(service: str) -> str:
        """Return raw stdout of `security find-generic-password -s <service>`.

        Raises `CredentialsError` if the command fails or returns no data.
        """
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                os.environ.get("USER", ""),
                "-w",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise CredentialsError(
                f"Failed to read {service!r} from macOS keychain "
                f"(rc={result.returncode})."
            )
        return result.stdout

    @staticmethod
    def _read_file(path: Path) -> str:
        """Return the contents of `path` or raise `CredentialsError`."""
        try:
            return path.read_text()
        except FileNotFoundError as exc:
            raise CredentialsError(
                f"Credentials file not found at {path}."
            ) from exc

    @staticmethod
    def _parse_json(blob: str, source: str) -> dict[str, Any]:
        """Parse `blob` as JSON or raise `CredentialsError`.

        `source` is a human-readable description of where the blob came
        from (a path, a keychain service name) and is included in the
        error message.
        """
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            raise CredentialsError(
                f"Credentials from {source} are not valid JSON: {exc}"
            ) from exc
