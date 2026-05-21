"""Concrete `Agent` implementations.

Each module here adds one upstream provider. Keep the list small —
`cli.py` enumerates these for the auto-detect path.
"""

from .claude import ClaudeAgent
from .codex import CodexAgent

__all__ = ["ClaudeAgent", "CodexAgent"]
