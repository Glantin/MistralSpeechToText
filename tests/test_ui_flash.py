"""Unit tests for the dot's flash states in mistral_stt (no mic, no network).

A flash (green recovered / orange error / red cancel) must never hide a
recording, and once its fade is over the real state must come back."""

import queue
import time

import mistral_stt as core
import transcribe_queue


def _reset_core(monkeypatch):
    core._recording_active = False
    core._recording_started_at = None
    core._warned_long = False
    core._ui_state = "idle"
    core._flash_until = 0.0
    core.notices = queue.Queue()
    core.errors = queue.Queue()
    core.on_ui_state_change = None
    monkeypatch.setattr(core, "set_clipboard", lambda text: None)
    monkeypatch.setattr(transcribe_queue, "active_count", lambda: 0)
    monkeypatch.setattr(transcribe_queue, "pending_count", lambda: 0)


def test_deferred_delivery_keeps_the_recording_dot(monkeypatch):
    _reset_core(monkeypatch)
    core._recording_active = True
    core._ui_state = "recording"

    core._deliver_deferred("hello")

    assert core._ui_state == "recording"
    assert "Cmd+V" in core.notices.get_nowait()


def test_deferred_delivery_flashes_when_idle(monkeypatch):
    _reset_core(monkeypatch)

    core._deliver_deferred("hello")

    assert core._ui_state == "recovered"


def test_flash_end_restores_the_real_state(monkeypatch):
    _reset_core(monkeypatch)
    core._deliver_error("boom")
    assert core._ui_state == "error"

    # Fade not over yet: the flash stays.
    core.tick_recording_limit()
    assert core._ui_state == "error"

    # Fade over while another job waits for the network: back to blue.
    core._flash_until = time.monotonic() - 1
    monkeypatch.setattr(transcribe_queue, "pending_count", lambda: 1)
    core.tick_recording_limit()
    assert core._ui_state == "retrying"


def test_first_transient_failure_says_it_retries(monkeypatch):
    _reset_core(monkeypatch)

    core._deliver_retrying("No network or API unreachable. Try again.")

    msg = core.errors.get_nowait()
    assert "retrying automatically" in msg
    assert "Try again" not in msg
