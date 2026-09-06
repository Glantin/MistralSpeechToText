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
