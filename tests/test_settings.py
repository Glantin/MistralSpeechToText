"""Unit tests for the user-adjustable recording limit (settings.py)."""

import config
import settings


def _point_to_tmp(tmp_path, monkeypatch):
    """Redirect the settings file into a temp dir (no real Application Support)."""
    monkeypatch.setattr(settings, "_SETTINGS_FILE", str(tmp_path / "settings.json"))


def test_default_when_unset(tmp_path, monkeypatch):
    _point_to_tmp(tmp_path, monkeypatch)
    assert settings.get_max_record_minutes() == config.MAX_RECORD_DEFAULT_MINUTES
    assert settings.get_max_record_seconds() == config.MAX_RECORD_DEFAULT_MINUTES * 60


def test_set_and_get_roundtrip(tmp_path, monkeypatch):
    _point_to_tmp(tmp_path, monkeypatch)
    assert settings.set_max_record_minutes(25) == 25
    assert settings.get_max_record_minutes() == 25
    assert settings.get_max_record_seconds() == 25 * 60


def test_clamps_to_ceiling(tmp_path, monkeypatch):
    _point_to_tmp(tmp_path, monkeypatch)
    stored = settings.set_max_record_minutes(999)
    assert stored == config.MAX_RECORD_CEILING_MINUTES
    assert settings.get_max_record_minutes() == config.MAX_RECORD_CEILING_MINUTES


def test_clamps_to_floor(tmp_path, monkeypatch):
    _point_to_tmp(tmp_path, monkeypatch)
    assert settings.set_max_record_minutes(0) == 1
    assert settings.set_max_record_minutes(-5) == 1


def test_corrupted_file_falls_back_to_default(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(settings, "_SETTINGS_FILE", str(path))
    assert settings.get_max_record_minutes() == config.MAX_RECORD_DEFAULT_MINUTES


def test_non_numeric_stored_value_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"max_record_minutes": "lots"}', encoding="utf-8")
    monkeypatch.setattr(settings, "_SETTINGS_FILE", str(path))
    assert settings.get_max_record_minutes() == config.MAX_RECORD_DEFAULT_MINUTES
