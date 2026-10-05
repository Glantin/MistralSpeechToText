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


# --- Empty-transcription preservation (audio never lost) ------------------

def _point_dirs(tmp_path, monkeypatch):
    """Redirect the queue's on-disk dirs into a temp folder."""
    pending = tmp_path / "pending"
    unresolved = tmp_path / "unresolved"
    pending.mkdir()
    monkeypatch.setattr(config, "PENDING_DIR", str(pending))
    monkeypatch.setattr(config, "UNRESOLVED_DIR", str(unresolved))
    return pending, unresolved


def test_move_to_unresolved_preserves_wav(tmp_path, monkeypatch):
    _point_dirs(tmp_path, monkeypatch)
    jobid = "job-1"
    wav = transcribe_queue._wav_path(jobid)
    with open(wav, "wb") as f:
        f.write(b"RIFFfake-audio")
    # Register a minimal job + sidecar, as the worker would.
    transcribe_queue._jobs[jobid] = {"wav_path": wav}
    transcribe_queue._write_sidecar(
        jobid,
        {"created_ts": 0, "attempts": 0, "next_try_ts": 0, "ever_deferred": False},
    )

    transcribe_queue._move_to_unresolved(jobid)

    # Job gone from the queue and from pending; audio kept in unresolved/.
    assert jobid not in transcribe_queue._jobs
    assert not __import__("os").path.exists(wav)
    kept = tmp_path / "unresolved" / f"{jobid}.wav"
    assert kept.exists()
    assert transcribe_queue.unresolved_count() == 1


def test_retry_unresolved_re_enqueues(tmp_path, monkeypatch):
    import os

    _point_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(transcribe_queue, "_notify_state", lambda: None)
    transcribe_queue._jobs.clear()
    os.makedirs(config.UNRESOLVED_DIR, exist_ok=True)
    kept = os.path.join(config.UNRESOLVED_DIR, "old.wav")
    with open(kept, "wb") as f:
        f.write(b"RIFFfake-audio")

    n = transcribe_queue.retry_unresolved()

    assert n == 1
    assert not os.path.exists(kept)  # moved back out of unresolved/
    assert len(transcribe_queue._jobs) == 1  # a fresh job now awaits transcription
    transcribe_queue._jobs.clear()


def test_purge_unresolved_drops_old(tmp_path, monkeypatch):
    import os

    _point_dirs(tmp_path, monkeypatch)
    os.makedirs(config.UNRESOLVED_DIR, exist_ok=True)
    old = os.path.join(config.UNRESOLVED_DIR, "old.wav")
    with open(old, "wb") as f:
        f.write(b"x")
    past = __import__("time").time() - (config.PENDING_MAX_AGE_SECONDS + 10)
    os.utime(old, (past, past))

    assert transcribe_queue.purge_unresolved() == 1
    assert not os.path.exists(old)


# --- Attempt cap: never an endless loop, audio kept -----------------------

class _Transient(Exception):
    status_code = 503


class _Permanent(Exception):
    status_code = 401


def _register_job(jobid, attempts=0):
    wav = transcribe_queue._wav_path(jobid)
    with open(wav, "wb") as f:
        f.write(b"RIFF" + b"\0" * 20000)
    transcribe_queue._jobs[jobid] = {
        "wav_path": wav,
        "created_ts": __import__("time").time(),
        "attempts": attempts,
        "next_try_ts": 0,
        "ever_deferred": False,
    }
    return wav


def _failing_transcribe(monkeypatch, exc_cls):
    def _boom(path):  # noqa: ARG001
        raise exc_cls("fail")

    monkeypatch.setattr(transcribe_queue._transcribe, "transcribe", _boom)


def test_gives_up_after_max_attempts_and_keeps_audio(tmp_path, monkeypatch):
    _point_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(transcribe_queue, "_notify_state", lambda: None)
    errors = []
    monkeypatch.setattr(transcribe_queue, "on_error", lambda m: None)
    monkeypatch.setattr(transcribe_queue, "on_permanent_error", errors.append)
    _failing_transcribe(monkeypatch, _Transient)
    transcribe_queue._jobs.clear()
    wav = _register_job("job-cap")

    for _ in range(config.RETRY_MAX_ATTEMPTS):
        assert "job-cap" in transcribe_queue._jobs  # still retrying
        transcribe_queue._attempt("job-cap", wav)

    assert config.RETRY_MAX_ATTEMPTS == 3
    assert transcribe_queue._jobs == {}  # no more automatic retries
    assert (tmp_path / "unresolved" / "job-cap.wav").exists()
    assert len(errors) == 1 and "audio kept" in errors[0]


def test_permanent_error_gives_up_at_once_and_keeps_audio(tmp_path, monkeypatch):
    _point_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(transcribe_queue, "_notify_state", lambda: None)
    monkeypatch.setattr(transcribe_queue, "on_permanent_error", lambda m: None)
    _failing_transcribe(monkeypatch, _Permanent)
    transcribe_queue._jobs.clear()
    wav = _register_job("job-401")

    transcribe_queue._attempt("job-401", wav)

    assert transcribe_queue._jobs == {}
    assert (tmp_path / "unresolved" / "job-401.wav").exists()


def test_recover_pending_does_not_resume_capped_job(tmp_path, monkeypatch):
    import json
    import time

    pending, unresolved = _point_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(transcribe_queue, "_notify_state", lambda: None)
    transcribe_queue._jobs.clear()
    (pending / "job-old.wav").write_bytes(b"RIFFfake-audio")
    (pending / "job-old.json").write_text(
        json.dumps({"created_ts": time.time(), "attempts": config.RETRY_MAX_ATTEMPTS})
    )

    assert transcribe_queue.recover_pending() == 0
    assert transcribe_queue._jobs == {}
    assert (unresolved / "job-old.wav").exists()
    assert not (pending / "job-old.json").exists()
