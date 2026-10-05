"""The recording worker must never die, and a mic failure resets the state."""

import mistral_stt as core
from audio import MicUnavailable


def test_mic_unavailable_resets_to_idle_and_warns(monkeypatch):
    def _fail():
        raise MicUnavailable("no mic")

    monkeypatch.setattr(core.recorder, "start", _fail)
    monkeypatch.setattr(core.recorder, "wedged", False)
    monkeypatch.setattr(core, "_recompute_ui", lambda: None)
    core.state = core.RECORDING_PTT
    core._recording_active = True
    while not core.errors.empty():
        core.errors.get_nowait()

    core._run_action("start")

    assert core.state == core.IDLE
    assert core._recording_active is False
    assert "Microphone unavailable" in core.errors.get_nowait()


def test_worker_survives_unexpected_error(monkeypatch):
    seen = []

    def _boom(action):
        seen.append(action)
        raise ValueError("unexpected")

    monkeypatch.setattr(core, "_run_action", _boom)
    monkeypatch.setattr(core, "_mic_failed", lambda exc: None)
    core._actions.put("stop")
    core._actions.put("stop")
    core._actions.put("__quit__")

    core._worker()  # returns only on __quit__: it did not die on the errors

    assert seen == ["stop", "stop"]


def test_start_skipped_when_already_released(monkeypatch):
    opened = []
    monkeypatch.setattr(core.recorder, "start", lambda: opened.append(1))
    core.state = core.IDLE

    core._run_action("start")

    assert opened == []
