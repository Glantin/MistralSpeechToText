"""Unit tests for the recording-limit logic in mistral_stt (warn + auto-stop).

These drive the pure state logic directly (no mic, no network, no timer): they
set the module's recording flags, advance the virtual start time, and assert what
tick_recording_limit() does.
"""

import queue
import time

import mistral_stt as core
import settings


def _reset_core():
    core._recording_active = False
    core.state = core.IDLE
    core._recording_started_at = None
    core._warned_long = False
    core._ui_state = "idle"
    core._actions = queue.Queue()
    core.notices = queue.Queue()
    core.on_ui_state_change = None


def test_auto_stops_and_sends_at_the_limit(monkeypatch):
    _reset_core()
    monkeypatch.setattr(settings, "get_max_record_seconds", lambda: 600)
    # An active take that has run just past the 10-min limit.
    core._recording_active = True
    core.state = core.RECORDING_PTT
    core._recording_started_at = time.monotonic() - 601

    core.tick_recording_limit()

    # Stopped exactly like a manual stop.
    assert core._recording_active is False
    assert core.state == core.IDLE
    assert core._recording_started_at is None
    # The captured take is enqueued for transcription (a "stop" action)...
    assert core._actions.get_nowait() == "stop"
    # ...and the user is told it was sent (nothing silently dropped).
    assert not core.notices.empty()
    _reset_core()


def test_warns_but_does_not_stop_before_the_limit(monkeypatch):
    _reset_core()
    monkeypatch.setattr(settings, "get_max_record_seconds", lambda: 600)
    monkeypatch.setattr(core, "_play", lambda sound: None)  # no afplay in tests
    core._recording_active = True
    core.state = core.RECORDING_PTT
    # 9 minutes in: past the ~8.5-min warn threshold, before the 10-min limit.
    core._recording_started_at = time.monotonic() - 540

    core.tick_recording_limit()

    # Warned (dot pulses), but recording continues — no stop enqueued.
    assert core._ui_state == "recording_long"
    assert core._warned_long is True
    assert core._recording_active is True
    assert core._actions.empty()
    _reset_core()


def test_no_warn_far_from_the_limit(monkeypatch):
    _reset_core()
    monkeypatch.setattr(settings, "get_max_record_seconds", lambda: 600)
    monkeypatch.setattr(core, "_play", lambda sound: None)
    core._recording_active = True
    core.state = core.RECORDING_PTT
    core._recording_started_at = time.monotonic() - 120  # 2 min in: nothing

    core.tick_recording_limit()

    assert core._ui_state != "recording_long"
    assert core._warned_long is False
    assert core._actions.empty()
    _reset_core()


def test_idle_is_a_noop():
    _reset_core()
    core.tick_recording_limit()  # not recording -> returns immediately
    assert core._actions.empty()
    assert core._ui_state == "idle"


def test_warn_threshold_uses_lead_below_cap():
    # Default: 90 s before the cap.
    assert core._warn_threshold_seconds(600) == 600 - 90
    # Tiny custom cap (< the lead): fall back to 75% so the heads-up still lands.
    assert core._warn_threshold_seconds(60) == 45.0
