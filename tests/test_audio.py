"""Mic capture must never freeze the app (CoreAudio deadlocks, missing mic)."""

import threading
import time

import numpy as np
import pytest

import audio
import config


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    monkeypatch.setattr(audio, "STOP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(audio, "OPEN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(audio, "_abandoned", [])


class _HangingStream:
    """Mimics PortAudio's Pa_StopStream deadlock: stop() never returns."""

    def __init__(self):
        self._never = threading.Event()

    def stop(self):
        self._never.wait()

    def close(self):
        pass


def test_stop_returns_and_keeps_audio_when_stream_hangs():
    rec = audio.Recorder()
    rec._stream = _HangingStream()
    rec._frames = [np.zeros((config.SAMPLE_RATE, 1), dtype=np.int16)]  # 1 s

    t0 = time.monotonic()
    path = rec.stop()
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0  # bounded, never frozen
    assert path is not None  # captured audio still written: dictation not lost
    assert rec.wedged
    assert len(audio._abandoned) == 1  # stream kept alive, never finalized


def test_start_refuses_once_wedged(monkeypatch):
    rec = audio.Recorder()
    rec.wedged = True
    opened = []
    monkeypatch.setattr(audio.sd, "InputStream", lambda **kw: opened.append(kw))

    with pytest.raises(audio.MicUnavailable):
        rec.start()
    assert opened == []  # CoreAudio not touched again


def test_start_retries_once_after_device_refresh(monkeypatch):
    calls = {"open": 0, "refresh": 0}

    class _Stream:
        def __init__(self, **kw):  # noqa: ARG002
            calls["open"] += 1
            if calls["open"] == 1:
                raise RuntimeError("no default input device")

        def start(self):
            pass

    monkeypatch.setattr(audio.sd, "InputStream", _Stream)
    monkeypatch.setattr(
        audio, "_refresh_devices", lambda: calls.__setitem__("refresh", 1)
    )
    rec = audio.Recorder()

    rec.start()

    assert calls == {"open": 2, "refresh": 1}
    assert rec._stream is not None and not rec.wedged


def test_start_raises_when_mic_stays_unavailable(monkeypatch):
    def _fail(**kw):  # noqa: ARG001
        raise RuntimeError("no default input device")

    monkeypatch.setattr(audio.sd, "InputStream", _fail)
    monkeypatch.setattr(audio, "_refresh_devices", lambda: None)
    rec = audio.Recorder()

    with pytest.raises(audio.MicUnavailable):
        rec.start()
    assert not rec.wedged  # a missing mic is not a deadlock


def test_start_hanging_marks_wedged(monkeypatch):
    never = threading.Event()

    class _Stream:
        def __init__(self, **kw):  # noqa: ARG002
            pass

        def start(self):
            never.wait()

    monkeypatch.setattr(audio.sd, "InputStream", _Stream)
    rec = audio.Recorder()

    with pytest.raises(audio.MicUnavailable):
        rec.start()
    assert rec.wedged
