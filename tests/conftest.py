"""Shared test setup."""

import pytest

import applog


@pytest.fixture(autouse=True)
def _no_log_file(monkeypatch):
    """Never write the real log file in Application Support from tests."""
    monkeypatch.setattr(applog, "log", lambda msg: None)
