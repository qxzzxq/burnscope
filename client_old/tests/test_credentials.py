"""Tests for the per-agent `*Credential` dataclasses and the shared
low-level helpers in `credentials.py`."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from burnscope_client.agents.claude import ClaudeAgent, ClaudeCredential
from burnscope_client.agents.codex import CodexAgent, CodexCredential
from burnscope_client.credentials import CredentialsError


KEYCHAIN_BLOB = json.dumps(
    {"claudeAiOauth": {"accessToken": "sk-keychain-token", "refreshToken": "r"}}
)
DUMMY_SERVICE = "test-keychain-service"
DUMMY_PATH = Path("/nonexistent/.credentials.json")


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


# ---------------------------------------------------------------------------
# ClaudeCredential.load
# ---------------------------------------------------------------------------


def test_claude_credential_load_from_macos_keychain(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed(KEYCHAIN_BLOB + "\n")
        cred = ClaudeCredential.load(
            keychain_service=DUMMY_SERVICE,
            credentials_path=DUMMY_PATH,
        )

    assert cred.access_token == "sk-keychain-token"
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["security", "find-generic-password", "-s"]
    assert cmd[3] == DUMMY_SERVICE


def test_claude_credential_falls_back_to_top_level_accessToken(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    blob = json.dumps({"accessToken": "sk-bare"})
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed(blob)
        cred = ClaudeCredential.load(
            keychain_service=DUMMY_SERVICE,
            credentials_path=DUMMY_PATH,
        )
    assert cred.access_token == "sk-bare"


def test_claude_credential_keychain_missing_raises(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed("", returncode=44)
        with pytest.raises(CredentialsError):
            ClaudeCredential.load(
                keychain_service=DUMMY_SERVICE,
                credentials_path=DUMMY_PATH,
            )


def test_claude_credential_load_from_linux_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-linux"}}))
    cred = ClaudeCredential.load(
        keychain_service=DUMMY_SERVICE,
        credentials_path=cred_file,
    )
    assert cred.access_token == "sk-linux"


def test_claude_credential_linux_missing_file_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(CredentialsError):
        ClaudeCredential.load(
            keychain_service=DUMMY_SERVICE,
            credentials_path=tmp_path / "missing.json",
        )


def test_claude_credential_malformed_json_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text("not json")
    with pytest.raises(CredentialsError):
        ClaudeCredential.load(
            keychain_service=DUMMY_SERVICE,
            credentials_path=cred_file,
        )


def test_claude_credential_no_access_token_in_blob_raises(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed(json.dumps({"claudeAiOauth": {}}))
        with pytest.raises(CredentialsError):
            ClaudeCredential.load(
                keychain_service=DUMMY_SERVICE,
                credentials_path=DUMMY_PATH,
            )


# ---------------------------------------------------------------------------
# CodexCredential.load
# ---------------------------------------------------------------------------


def test_codex_credential_load_with_account_id(tmp_path: Path):
    cred_file = tmp_path / "auth.json"
    cred_file.write_text(
        json.dumps({"tokens": {"access_token": "cx-abc", "account_id": "acct-1"}})
    )
    cred = CodexCredential.load(credentials_path=cred_file)
    assert cred == CodexCredential(access_token="cx-abc", account_id="acct-1")


def test_codex_credential_load_without_account_id(tmp_path: Path):
    cred_file = tmp_path / "auth.json"
    cred_file.write_text(json.dumps({"tokens": {"access_token": "cx-abc"}}))
    cred = CodexCredential.load(credentials_path=cred_file)
    assert cred == CodexCredential(access_token="cx-abc", account_id=None)


def test_codex_credential_missing_file_raises(tmp_path: Path):
    with pytest.raises(CredentialsError):
        CodexCredential.load(credentials_path=tmp_path / "missing.json")


def test_codex_credential_missing_access_token_raises(tmp_path: Path):
    cred_file = tmp_path / "auth.json"
    cred_file.write_text(json.dumps({"tokens": {"account_id": "acct-1"}}))
    with pytest.raises(CredentialsError):
        CodexCredential.load(credentials_path=cred_file)


def test_codex_credential_malformed_json_raises(tmp_path: Path):
    cred_file = tmp_path / "auth.json"
    cred_file.write_text("not json")
    with pytest.raises(CredentialsError):
        CodexCredential.load(credentials_path=cred_file)


# ---------------------------------------------------------------------------
# Per-agent storage locations
# ---------------------------------------------------------------------------


def test_claude_agent_class_constants_match_expected_locations():
    assert ClaudeAgent.KEYCHAIN_SERVICE == "Claude Code-credentials"
    assert ClaudeAgent.CREDENTIALS_PATH == Path.home() / ".claude" / ".credentials.json"


def test_codex_agent_class_constants_match_expected_location():
    assert CodexAgent.CREDENTIALS_PATH == Path.home() / ".codex" / "auth.json"
