import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from burnscope_client.credentials import CredentialsError, load_claude_token


KEYCHAIN_BLOB = json.dumps(
    {"claudeAiOauth": {"accessToken": "sk-keychain-token", "refreshToken": "r"}}
)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_load_token_from_macos_keychain(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed(KEYCHAIN_BLOB + "\n")
        token = load_claude_token()

    assert token == "sk-keychain-token"
    assert run.call_args.args[0][:3] == [
        "security",
        "find-generic-password",
        "-s",
    ]


def test_load_token_falls_back_to_top_level_accessToken(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    blob = json.dumps({"accessToken": "sk-bare"})
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed(blob)
        assert load_claude_token() == "sk-bare"


def test_load_token_keychain_missing_raises(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed("", returncode=44)
        with pytest.raises(CredentialsError):
            load_claude_token()


def test_load_token_from_linux_credentials_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-linux"}}))
    assert load_claude_token(credentials_path=cred) == "sk-linux"


def test_load_token_linux_missing_file_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(CredentialsError):
        load_claude_token(credentials_path=tmp_path / "missing.json")


def test_load_token_malformed_json_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    cred = tmp_path / ".credentials.json"
    cred.write_text("not json")
    with pytest.raises(CredentialsError):
        load_claude_token(credentials_path=cred)


def test_load_token_no_access_token_in_blob_raises(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("burnscope_client.credentials.subprocess.run") as run:
        run.return_value = _completed(json.dumps({"claudeAiOauth": {}}))
        with pytest.raises(CredentialsError):
            load_claude_token()
