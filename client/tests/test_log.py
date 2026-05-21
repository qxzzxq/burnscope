import logging

from burnscope_client._log import _SENTINEL_ATTR, configure_logging


def _our_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, _SENTINEL_ATTR, False)
    ]


def test_writes_to_file_when_env_var_set(monkeypatch, tmp_path):
    log_path = tmp_path / "burn.log"
    monkeypatch.setenv("BURNSCOPE_LOG_FILE", str(log_path))

    configure_logging()
    logging.getLogger("test").warning("hello from test")
    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_path.read_text()
    assert "hello from test" in contents
    assert "WARNING" in contents


def test_expands_user_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BURNSCOPE_LOG_FILE", "~/inside-home.log")

    configure_logging()
    logging.getLogger("test").info("ping")
    for h in logging.getLogger().handlers:
        h.flush()

    assert (tmp_path / "inside-home.log").read_text().endswith("ping\n")


def test_silent_by_default_when_no_env_var_and_no_fallback(monkeypatch):
    monkeypatch.delenv("BURNSCOPE_LOG_FILE", raising=False)

    configure_logging()

    ours = _our_handlers()
    assert len(ours) == 1
    assert isinstance(ours[0], logging.NullHandler)


def test_fallback_stderr_when_env_var_absent(monkeypatch):
    monkeypatch.delenv("BURNSCOPE_LOG_FILE", raising=False)

    configure_logging(fallback_stderr=True)

    ours = _our_handlers()
    assert len(ours) == 1
    assert isinstance(ours[0], logging.StreamHandler)
    assert not isinstance(ours[0], logging.FileHandler)


def test_file_wins_over_fallback_stderr(monkeypatch, tmp_path):
    log_path = tmp_path / "burn.log"
    monkeypatch.setenv("BURNSCOPE_LOG_FILE", str(log_path))

    configure_logging(fallback_stderr=True)

    assert any(isinstance(h, logging.FileHandler) for h in _our_handlers())


def test_idempotent_does_not_stack_handlers(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_LOG_FILE", str(tmp_path / "x.log"))
    configure_logging()
    configure_logging()
    configure_logging()
    assert len(_our_handlers()) == 1


def test_default_level_is_info(monkeypatch):
    monkeypatch.delenv("BURNSCOPE_LOG_LEVEL", raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_burnscope_log_level_overrides_default(monkeypatch):
    monkeypatch.setenv("BURNSCOPE_LOG_LEVEL", "DEBUG")
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_burnscope_log_level_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("BURNSCOPE_LOG_LEVEL", "warning")
    configure_logging()
    assert logging.getLogger().level == logging.WARNING


def test_burnscope_log_level_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("BURNSCOPE_LOG_LEVEL", "VERY_LOUD")
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_debug_record_lands_in_file_when_level_is_debug(monkeypatch, tmp_path):
    log_path = tmp_path / "burn.log"
    monkeypatch.setenv("BURNSCOPE_LOG_FILE", str(log_path))
    monkeypatch.setenv("BURNSCOPE_LOG_LEVEL", "DEBUG")
    configure_logging()
    logging.getLogger("test").debug("debug visible at debug level")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "debug visible at debug level" in log_path.read_text()


def test_debug_record_dropped_at_default_info_level(monkeypatch, tmp_path):
    log_path = tmp_path / "burn.log"
    monkeypatch.setenv("BURNSCOPE_LOG_FILE", str(log_path))
    monkeypatch.delenv("BURNSCOPE_LOG_LEVEL", raising=False)
    configure_logging()
    logging.getLogger("test").debug("should be filtered out")
    logging.getLogger("test").info("should appear")
    for h in logging.getLogger().handlers:
        h.flush()
    contents = log_path.read_text()
    assert "should be filtered out" not in contents
    assert "should appear" in contents


def test_creates_parent_directory(monkeypatch, tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "burn.log"
    monkeypatch.setenv("BURNSCOPE_LOG_FILE", str(nested))

    configure_logging()
    logging.getLogger("test").info("works")
    for h in logging.getLogger().handlers:
        h.flush()

    assert nested.exists()
