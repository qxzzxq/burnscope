"""Shared cleanup so logging-handler state can't leak across tests.

`claude_statusline.main()` and `codex_daemon.main()` install a sentinel
handler on the root logger. Without this hook, the first test that
exercises one of those entrypoints would poison every subsequent
`configure_logging()` call (which is idempotent on the sentinel).
"""

from __future__ import annotations

import logging

import pytest

from burnscope_client._log import _SENTINEL_ATTR


@pytest.fixture(autouse=True)
def _strip_burnscope_handlers():
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _SENTINEL_ATTR, False)]
    yield
    root.handlers = [h for h in root.handlers if not getattr(h, _SENTINEL_ATTR, False)]


@pytest.fixture(autouse=True)
def _isolate_burnscope_env(monkeypatch):
    """Don't let the developer's shell env leak into the test run.

    BURNSCOPE_LOG_FILE in particular would cause test-injected exception
    paths to pollute the developer's real ~/.burnscope/claude.log.
    """
    for var in ("BURNSCOPE_LOG_FILE", "BURNSCOPE_LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
