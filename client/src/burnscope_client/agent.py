"""Abstract base class for upstream agents (Claude, Codex, ...).

An `Agent` knows three things:

1. How to load its own credential object from local storage
   (`load_credential()`).
2. How to probe its upstream API and turn the response into a
   `schema.AgentSnapshot`, handling any agent-specific scaling so the
   wire format always uses `0.0`-`1.0` for `used_pct`.
3. Its short name (`"claude"`, `"codex"`, ...), used both as the
   `AgentSnapshot.agent` field and as the CLI selector.

Each subclass also owns its own `*Credential` dataclass — the agent's
constructor takes that credential object, so the base class can stay
agnostic about which fields a given agent needs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from .credentials import CredentialsError
from .schema import AgentSnapshot


class ProbeError(RuntimeError):
    """Raised when an upstream probe fails or returns unparseable data."""


class Agent(ABC):
    """Base class for an upstream agent.

    Subclasses define `name`, take their own `*Credential` dataclass as
    the single constructor argument, and implement `probe()` and
    `load_credential()`. `try_create()` then works for every subclass
    without overrides — credentials with extra fields (e.g. Codex's
    optional `account_id`) ride along inside the dataclass.
    """

    name: ClassVar[str]

    @abstractmethod
    def __init__(self, credential: Any) -> None:
        """Bind this agent to a fully-loaded credential object."""

    @abstractmethod
    async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
        """Issue one upstream probe and return a fresh snapshot.

        Raises `ProbeError` on transport failure, non-2xx response, or
        when the response lacks the headers we need to build a
        snapshot. Implementations must normalise upstream `used_pct`
        values into `0.0`-`1.0` before constructing
        `SessionSnapshot`s.
        """

    @classmethod
    @abstractmethod
    def load_credential(cls) -> Any:
        """Read this agent's credential from local storage.

        Raises `CredentialsError` if no credential is available.
        """

    @classmethod
    def try_create(cls) -> "Agent | None":
        """Return an instance if credentials are present, else `None`.

        Used by the CLI's auto-detect path so a daemon with only one
        agent logged in starts cleanly.
        """
        try:
            return cls(cls.load_credential())
        except CredentialsError:
            return None
