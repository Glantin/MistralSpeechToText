"""Unit tests for API-key resolution (credentials.py).

Order under test: environment variable first, then the Application Support file.
"""

import credentials


def test_env_var_takes_precedence(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "  env-key  ")
    assert credentials.get_api_key() == "env-key"
    assert credentials.has_api_key() is True


def test_reads_from_key_file_when_no_env(tmp_path, monkeypatch):
    key_file = tmp_path / ".env"
    key_file.write_text(
        "# a comment\n"
        "OTHER=value\n"
        'MISTRAL_API_KEY="file-key"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "KEY_FILE", str(key_file))
    assert credentials.get_api_key() == "file-key"


def test_missing_everywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "KEY_FILE", str(tmp_path / "absent.env"))
    assert credentials.get_api_key() is None
    assert credentials.has_api_key() is False


def test_set_api_key_writes_and_locks_down(tmp_path, monkeypatch):
    import os

    key_file = tmp_path / "sub" / ".env"
    monkeypatch.setattr(credentials, "APP_SUPPORT_DIR", str(tmp_path / "sub"))
    monkeypatch.setattr(credentials, "KEY_FILE", str(key_file))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    credentials.set_api_key("written-key")
    assert credentials.get_api_key() == "written-key"
    # Secret file must be user-only readable/writable (0o600).
    assert (os.stat(key_file).st_mode & 0o777) == 0o600
