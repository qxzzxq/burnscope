"""Optional file-backed logging shared by both collector entry points.

Honored env vars:
    BURNSCOPE_LOG_FILE — absolute or ~-prefixed path. When set, all log
        records are appended there. Survives Popen because env vars are
        inherited, so the detached `--push` child writes to the same file
        as the foreground parent.
    BURNSCOPE_LOG_LEVEL — DEBUG / INFO / WARNING / ERROR (case-insensitive).
        Defaults to INFO. Set to DEBUG to see the per-fire narrative
        (mDNS, cache hits, push URL, etc.).

When BURNSCOPE_LOG_FILE is unset:
    * claude_statusline calls `configure_logging()` — logs are dropped.
      Claude Code discards the script's stderr anyway, so there's nowhere
      useful for them to go by default.
    * codex_daemon calls `configure_logging(fallback_stderr=True)` — logs
      land on stderr, which the launchd plist / systemd unit redirects to
      ~/.burnscope/codex.stderr.log. Preserves the supervisor-level
      logging story.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path


_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(process)d]: %(message)s"
_SENTINEL_ATTR = "_burnscope_handler"


def _resolve_level(default: int) -> int:
    """Parse $BURNSCOPE_LOG_LEVEL or return `default`."""
    raw = os.environ.get("BURNSCOPE_LOG_LEVEL")
    if not raw:
        return default
    parsed = logging.getLevelName(raw.strip().upper())
    # getLevelName returns the int for known names, or the original string
    # wrapped as "Level X" for unknown names. Validate by type.
    if isinstance(parsed, int):
        return parsed
    return default


def configure_logging(
    *,
    fallback_stderr: bool = False,
    default_level: int = logging.INFO,
) -> None:
    """Wire up the root logger based on $BURNSCOPE_LOG_FILE and $BURNSCOPE_LOG_LEVEL.

    Idempotent: a sentinel attribute on the installed handler ensures
    repeat calls within one process don't stack handlers, while still
    coexisting with foreign handlers (pytest's caplog, etc).
    """
    root = logging.getLogger()
    if any(getattr(h, _SENTINEL_ATTR, False) for h in root.handlers):
        return

    path = os.environ.get("BURNSCOPE_LOG_FILE")
    formatter = logging.Formatter(_FORMAT)
    handler: logging.Handler
    if path:
        expanded = Path(path).expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(expanded, mode="a")
    elif fallback_stderr:
        handler = logging.StreamHandler()
    else:
        handler = logging.NullHandler()
    handler.setFormatter(formatter)
    setattr(handler, _SENTINEL_ATTR, True)
    root.addHandler(handler)
    root.setLevel(_resolve_level(default_level))

    # Third-party loggers are chatty at INFO/DEBUG and duplicate our own
    # records. Pin them to WARNING so they don't clutter the per-fire trail.
    for noisy in ("httpx", "httpcore", "zeroconf", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
