import json

import pytest

from burnscope_client import identity


def _write_settings(monkeypatch, tmp_path, payload):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(identity, "CLAUDE_SETTINGS_FILE", path)
    return path


def test_returns_oauth_email_when_present(monkeypatch, tmp_path):
    _write_settings(
        monkeypatch,
        tmp_path,
        {"oauthAccount": {"emailAddress": "you@example.com"}, "userID": "fallback-id"},
    )
    assert identity.claude_user_identifier() == "you@example.com"


def test_falls_back_to_user_id_when_email_missing(monkeypatch, tmp_path):
    _write_settings(
        monkeypatch,
        tmp_path,
        {"oauthAccount": {"organizationUuid": "org-x"}, "userID": "abc123"},
    )
    assert identity.claude_user_identifier() == "abc123"


def test_falls_back_to_user_id_when_oauth_account_missing(monkeypatch, tmp_path):
    _write_settings(monkeypatch, tmp_path, {"userID": "abc123"})
    assert identity.claude_user_identifier() == "abc123"


def test_falls_back_when_email_is_empty_string(monkeypatch, tmp_path):
    _write_settings(
        monkeypatch,
        tmp_path,
        {"oauthAccount": {"emailAddress": ""}, "userID": "fallback"},
    )
    assert identity.claude_user_identifier() == "fallback"


def test_raises_when_neither_present(monkeypatch, tmp_path):
    _write_settings(monkeypatch, tmp_path, {"numStartups": 7})
    with pytest.raises(identity.IdentityError, match="neither"):
        identity.claude_user_identifier()


def test_raises_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        identity, "CLAUDE_SETTINGS_FILE", tmp_path / "absent.json"
    )
    with pytest.raises(identity.IdentityError, match="Could not read"):
        identity.claude_user_identifier()


def test_raises_when_json_invalid(monkeypatch, tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("not json")
    monkeypatch.setattr(identity, "CLAUDE_SETTINGS_FILE", path)
    with pytest.raises(identity.IdentityError, match="not valid JSON"):
        identity.claude_user_identifier()


def test_raises_when_root_not_object(monkeypatch, tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("[1, 2, 3]")
    monkeypatch.setattr(identity, "CLAUDE_SETTINGS_FILE", path)
    with pytest.raises(identity.IdentityError, match="not a JSON object"):
        identity.claude_user_identifier()


def test_user_id_must_be_non_empty_string(monkeypatch, tmp_path):
    _write_settings(monkeypatch, tmp_path, {"userID": ""})
    with pytest.raises(identity.IdentityError):
        identity.claude_user_identifier()
