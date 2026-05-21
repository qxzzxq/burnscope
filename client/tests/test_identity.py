import json

import pytest

from burnscope_client import identity


def test_client_id_for_agent_is_deterministic():
    h1 = identity.client_id_for_agent("claude", "abc-123")
    h2 = identity.client_id_for_agent("claude", "abc-123")
    assert h1 == h2
    assert len(h1) == 64


def test_client_id_for_agent_namespaces_by_agent():
    assert identity.client_id_for_agent(
        "claude", "abc"
    ) != identity.client_id_for_agent("codex", "abc")


def test_client_id_for_agent_lowercases_agent_name():
    assert identity.client_id_for_agent(
        "CLAUDE", "abc"
    ) == identity.client_id_for_agent("claude", "abc")


def test_client_id_for_agent_known_vector():
    # SHA256 of "burnscope:claude:abc-123" — locks the wire format down.
    import hashlib

    expected = hashlib.sha256(b"burnscope:claude:abc-123").hexdigest()
    assert identity.client_id_for_agent("claude", "abc-123") == expected


def test_claude_org_uuid_prefers_keyring(monkeypatch):
    monkeypatch.setattr(
        identity,
        "_try_keyring",
        lambda: {"organizationUuid": "from-keyring"},
    )
    monkeypatch.setattr(
        identity,
        "_try_file",
        lambda: {"organizationUuid": "from-file"},
    )
    assert identity.claude_org_uuid() == "from-keyring"


def test_claude_org_uuid_falls_back_to_file(monkeypatch):
    monkeypatch.setattr(identity, "_try_keyring", lambda: None)
    monkeypatch.setattr(
        identity,
        "_try_file",
        lambda: {"organizationUuid": "from-file"},
    )
    assert identity.claude_org_uuid() == "from-file"


def test_claude_org_uuid_accepts_nested_layout(monkeypatch):
    monkeypatch.setattr(identity, "_try_keyring", lambda: None)
    monkeypatch.setattr(
        identity,
        "_try_file",
        lambda: {"claudeAiOauth": {"organizationUuid": "nested-uuid"}},
    )
    assert identity.claude_org_uuid() == "nested-uuid"


def test_claude_org_uuid_raises_when_no_source(monkeypatch):
    monkeypatch.setattr(identity, "_try_keyring", lambda: None)
    monkeypatch.setattr(identity, "_try_file", lambda: None)
    with pytest.raises(identity.IdentityError):
        identity.claude_org_uuid()


def test_claude_org_uuid_raises_when_field_missing(monkeypatch):
    monkeypatch.setattr(identity, "_try_keyring", lambda: {"other": "x"})
    monkeypatch.setattr(identity, "_try_file", lambda: None)
    with pytest.raises(identity.IdentityError):
        identity.claude_org_uuid()


def test_try_file_reads_credentials_file(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"organizationUuid": "real-uuid"}))
    monkeypatch.setattr(identity, "CLAUDE_CREDENTIALS_FILE", creds)
    assert identity._try_file() == {"organizationUuid": "real-uuid"}


def test_try_file_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        identity, "CLAUDE_CREDENTIALS_FILE", tmp_path / "nope.json"
    )
    assert identity._try_file() is None


def test_try_file_returns_none_for_invalid_json(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text("not json")
    monkeypatch.setattr(identity, "CLAUDE_CREDENTIALS_FILE", creds)
    assert identity._try_file() is None
