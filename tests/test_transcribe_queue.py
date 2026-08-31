"""Unit tests for the retry back-off schedule in transcribe_queue.py."""

import config
import transcribe_queue


def test_backoff_follows_schedule():
    schedule = config.RETRY_BACKOFF_SECONDS
    assert transcribe_queue._backoff_for(1) == float(schedule[0])
    assert transcribe_queue._backoff_for(2) == float(schedule[1])


def test_backoff_repeats_last_value_beyond_schedule():
    schedule = config.RETRY_BACKOFF_SECONDS
    assert transcribe_queue._backoff_for(len(schedule)) == float(schedule[-1])
    assert transcribe_queue._backoff_for(9999) == float(schedule[-1])


def test_backoff_clamps_low_attempts():
    schedule = config.RETRY_BACKOFF_SECONDS
    # attempts 0 (and below) clamp to the first slot, never index out of range.
    assert transcribe_queue._backoff_for(0) == float(schedule[0])


def test_backoff_default_when_schedule_empty(monkeypatch):
    monkeypatch.setattr(config, "RETRY_BACKOFF_SECONDS", [])
    assert transcribe_queue._backoff_for(3) == 30.0
