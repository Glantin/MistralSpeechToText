"""PERSISTENT transcription queue with automatic retry.

Why this module? On a weak/changing network a transcription could fail and the
audio was lost (temp WAV deleted, no retry). Here:

  - recording (mic) and transcription (network) live on TWO distinct threads: a
    slow/blocked network call no longer freezes recording;
  - each take is written to DISK (PENDING_DIR) before transcription, so it is
    kept until a transcript is obtained — even after an app restart
    (recover_pending);
  - on failure, we RETRY in the background, at most RETRY_MAX_ATTEMPTS times;
    then the audio moves to UNRESOLVED_DIR (kept, retried only on demand from
    the menu): never an endless loop.

Delivery (callbacks injected by mistral_stt.py):
  - success on the first try (never deferred) -> deliver_immediate (paste at cursor);
  - success after a failure (deferred)         -> deliver_deferred (clipboard +
                                                   green dot + notification).

THREADING CONSTRAINT: a single worker consumes the queue; shared state is
protected by a Condition. Callbacks are invoked OUTSIDE the lock.
"""

import json
import os
import shutil
import threading
import time
import uuid

import applog
import config
import history
import transcribe as _transcribe

# Debug echo on the console, gated by the same env var as mistral_stt. The log
# FILE (applog) always gets the line.
_DEBUG = bool(os.environ.get("MISTRAL_STT_DEBUG"))


def _log(msg: str) -> None:
    applog.log(f"queue: {msg}")
    if _DEBUG:
        print(f"[transcribe_queue:debug] {msg}")

# --- Shared state ---------------------------------------------------------
_cond = threading.Condition(threading.RLock())
# jobid -> {"wav_path", "created_ts", "attempts", "next_try_ts", "ever_deferred"}
_jobs: dict[str, dict] = {}
_active_jobid: str | None = None  # job currently being attempted (None when idle)
_started = False

# --- Callbacks (wired by mistral_stt.py) ----------------------------------
# Invoked OUTSIDE the lock. Each may stay None (e.g. a test context).
on_state_change = None   # () -> None: recompute the dot
deliver_immediate = None  # (text: str) -> None: paste at cursor
deliver_deferred = None   # (text: str) -> None: clipboard + flash + notif
on_error = None           # (message: str) -> None: first TRANSIENT failure (optional)
on_permanent_error = None  # (message: str) -> None: job GIVEN UP (permanent error)
on_empty = None           # (message: str) -> None: EMPTY transcription, audio preserved


def _notify_state() -> None:
    cb = on_state_change
    if cb is not None:
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass


# --- Counters read by the dot DERIVATION (main thread) --------------------
def active_count() -> int:
    """1 if a transcription attempt is in progress, else 0 (-> amber)."""
    with _cond:
        return 1 if _active_jobid is not None else 0


def pending_count() -> int:
    """Number of jobs awaiting a (new) attempt (-> blue 'retrying').

    Excludes the job currently being attempted (it counts as 'amber' instead).
    """
    with _cond:
        n = len(_jobs)
        if _active_jobid is not None:
            n -= 1
        return n


# --- Persistence (JSON sidecar next to the WAV) ---------------------------
def _sidecar_path(jobid: str) -> str:
    return os.path.join(config.PENDING_DIR, f"{jobid}.json")


def _wav_path(jobid: str) -> str:
    return os.path.join(config.PENDING_DIR, f"{jobid}.wav")


def _write_sidecar(jobid: str, meta: dict) -> None:
    try:
        data = {
            "created_ts": meta["created_ts"],
            "attempts": meta["attempts"],
            "next_try_ts": meta["next_try_ts"],
            "ever_deferred": meta["ever_deferred"],
        }
        with open(_sidecar_path(jobid), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _remove_job(jobid: str) -> None:
    """Remove the job from the queue AND from disk (WAV + sidecar)."""
    meta = _jobs.pop(jobid, None)
    wav = meta["wav_path"] if meta else _wav_path(jobid)
    for p in (wav, _sidecar_path(jobid)):
        try:
            os.remove(p)
        except OSError:
            pass


def _wav_size(wav: str) -> int:
    try:
        return os.path.getsize(wav)
    except OSError:
        return 0


def _move_to_unresolved(jobid: str) -> None:
    """Preserve a job's WAV in UNRESOLVED_DIR and drop it from the queue.

    Used when a transcription keeps coming back EMPTY, or is GIVEN UP (permanent
    error / attempt cap): the audio is kept (never lost) but OUT of the
    auto-retry queue, so it is never re-run in a loop. The user re-runs it on
    demand via retry_unresolved()."""
    meta = _jobs.pop(jobid, None)
    wav = meta["wav_path"] if meta else _wav_path(jobid)
    try:
        os.makedirs(config.UNRESOLVED_DIR, exist_ok=True)
        dest = os.path.join(config.UNRESOLVED_DIR, f"{jobid}.wav")
        shutil.move(wav, dest)
    except OSError:
        pass
    try:
        os.remove(_sidecar_path(jobid))
    except OSError:
        pass


def _backoff_for(attempts: int) -> float:
    """Delay (s) before the next attempt; the last value is repeated."""
    schedule = config.RETRY_BACKOFF_SECONDS
    if not schedule:
        return 30.0
    idx = min(max(attempts - 1, 0), len(schedule) - 1)
    return float(schedule[idx])


# --- Public API -----------------------------------------------------------
def enqueue(wav_path: str) -> None:
    """Register a new take: move the WAV into PENDING_DIR and wake the worker
    (transcription due immediately)."""
    if not wav_path or not os.path.exists(wav_path):
        return
    os.makedirs(config.PENDING_DIR, exist_ok=True)
    jobid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    dest = _wav_path(jobid)
    try:
        shutil.move(wav_path, dest)
    except OSError:
        # The move failed (e.g. different volumes): copy as a last resort.
        try:
            shutil.copyfile(wav_path, dest)
            os.remove(wav_path)
        except OSError:
            return
    now = time.time()
    meta = {
        "wav_path": dest,
        "created_ts": now,
        "attempts": 0,
        "next_try_ts": now,  # due immediately
        "ever_deferred": False,
    }
    with _cond:
        _jobs[jobid] = meta
        _write_sidecar(jobid, meta)
        _cond.notify_all()
    _notify_state()


def recover_pending() -> int:
    """Re-register the WAVs present in PENDING_DIR (resume after a restart).

    A recovered job is considered 'deferred' (ever_deferred): its delivery goes
    through the clipboard, never a paste at the cursor (the context has changed).
    Purges takes that are too old along the way. Returns the number resumed.
    """
    d = config.PENDING_DIR
    if not os.path.isdir(d):
        return 0
    now = time.time()
    recovered = 0
    for name in os.listdir(d):
        if not name.endswith(".wav"):
            continue
        jobid = name[:-4]
        wav = os.path.join(d, name)
        if jobid in _jobs:
            continue
        # Load the sidecar if present, otherwise default values.
        meta = {
            "wav_path": wav,
            "created_ts": now,
            "attempts": 0,
            "next_try_ts": now,
            "ever_deferred": True,
        }
        try:
            with open(_sidecar_path(jobid), encoding="utf-8") as f:
                saved = json.load(f)
            meta["created_ts"] = saved.get("created_ts", now)
            meta["attempts"] = saved.get("attempts", 0)
            meta["ever_deferred"] = True  # survival = deferred
            meta["next_try_ts"] = now  # retry right away on startup
        except (OSError, ValueError):
            pass
        # Attempt cap already reached (e.g. the app was killed mid-give-up):
        # never resume the loop, keep the audio for a manual retry.
        if meta["attempts"] >= config.RETRY_MAX_ATTEMPTS:
            with _cond:
                _jobs[jobid] = meta
                _move_to_unresolved(jobid)
            _log(f"recovered job {jobid} already at attempt cap -> unresolved/")
            continue
        # Purge takes that are too old.
        if now - meta["created_ts"] > config.PENDING_MAX_AGE_SECONDS:
            for p in (wav, _sidecar_path(jobid)):
                try:
                    os.remove(p)
                except OSError:
                    pass
            continue
        with _cond:
            _jobs[jobid] = meta
        recovered += 1
    if recovered:
        with _cond:
            _cond.notify_all()
        _notify_state()
    return recovered


def unresolved_count() -> int:
    """Number of preserved (empty-transcription) takes awaiting a manual retry."""
    d = config.UNRESOLVED_DIR
    if not os.path.isdir(d):
        return 0
    return sum(1 for n in os.listdir(d) if n.endswith(".wav"))


def purge_unresolved() -> int:
    """Delete preserved takes older than PENDING_MAX_AGE_SECONDS. Returns count."""
    d = config.UNRESOLVED_DIR
    if not os.path.isdir(d):
        return 0
    now = time.time()
    purged = 0
    for name in os.listdir(d):
        if not name.endswith(".wav"):
            continue
        p = os.path.join(d, name)
        try:
            if now - os.path.getmtime(p) > config.PENDING_MAX_AGE_SECONDS:
                os.remove(p)
                purged += 1
        except OSError:
            pass
    return purged


def retry_unresolved() -> int:
    """Re-enqueue every preserved (empty) take for a fresh transcription attempt.

    Called on demand (menu / CLI). Each WAV is moved back into the normal queue
    via enqueue() (new jobid), so a manual retry can rescue a dictation whose
    first transcription came back empty. Returns the number re-enqueued."""
    d = config.UNRESOLVED_DIR
    if not os.path.isdir(d):
        return 0
    n = 0
    for name in os.listdir(d):
        if not name.endswith(".wav"):
            continue
        src = os.path.join(d, name)
        before = _wav_size(src)
        enqueue(src)  # moves the WAV into PENDING_DIR under a fresh jobid
        if before and not os.path.exists(src):
            n += 1
    return n


def start() -> threading.Thread | None:
    """Start the transcription worker (idempotent)."""
    global _started
    with _cond:
        if _started:
            return None
        _started = True
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# --- Worker ---------------------------------------------------------------
def _pick_due_job(now: float) -> tuple[str | None, float | None]:
    """Return (jobid due for transcription, or None) and (next deadline, or None).

    Called UNDER the lock. Purges jobs that are too old first.
    """
    expired = [
        jid
        for jid, m in _jobs.items()
        if now - m["created_ts"] > config.PENDING_MAX_AGE_SECONDS
    ]
    for jid in expired:
        _remove_job(jid)

    due_jid = None
    due_ts = None
    next_ts = None
    for jid, m in _jobs.items():
        t = m["next_try_ts"]
        if t <= now and (due_ts is None or t < due_ts):
            due_jid, due_ts = jid, t
        if next_ts is None or t < next_ts:
            next_ts = t
    return due_jid, next_ts


def _run() -> None:
    global _active_jobid
    while True:
        with _cond:
            now = time.time()
            jobid, next_ts = _pick_due_job(now)
            if jobid is None:
                # Nothing due: sleep until the next deadline (or forever if there
                # is no job), wakeable by enqueue()/recover_pending().
                wait = None if next_ts is None else max(0.05, next_ts - now)
                _cond.wait(timeout=wait)
                continue
            meta = _jobs[jobid]
            wav = meta["wav_path"]
            _active_jobid = jobid
        try:
            _attempt(jobid, wav)
        except Exception as exc:  # noqa: BLE001
            # Never let this thread die silently: drop the active mark, keep the
            # job (it is retried on its schedule, bounded by the attempt cap).
            _log(f"job {jobid} unexpected error: {exc!r}")
            with _cond:
                _active_jobid = None
                m = _jobs.get(jobid)
                if m is not None:
                    m["attempts"] = m.get("attempts", 0) + 1
                    m["next_try_ts"] = time.time() + _backoff_for(m["attempts"])
                    if m["attempts"] >= config.RETRY_MAX_ATTEMPTS:
                        _move_to_unresolved(jobid)
            _notify_state()


def _attempt(jobid: str, wav: str) -> None:
    """One transcription attempt for a job already marked active (_active_jobid),
    then its outcome: deliver, reschedule, or give up (audio kept)."""
    global _active_jobid
    _log(f"picked job {jobid} ({wav}) -> transcribing (amber)")
    _notify_state()  # -> amber dot (transcription in progress)

    # Network call OUTSIDE the lock (blocking, bounded by the HTTP timeout).
    text = None
    ok = False
    err_msg = None
    retriable = True
    try:
        text = _transcribe.transcribe(wav)
        ok = True
    except Exception as exc:  # noqa: BLE001
        err_msg = _transcribe.classify_error(exc)[1]
        retriable = _transcribe.is_retriable(exc)
        _log(
            f"job {jobid} attempt failed: {type(exc).__name__} "
            f"status={_transcribe.http_status(exc)} retriable={retriable}"
        )

    first_failure = False
    gave_up = False
    empty_retry = False
    empty_preserved = False
    with _cond:
        _active_jobid = None
        if ok:
            ever_deferred = _jobs.get(jobid, {}).get("ever_deferred", False)
            if text:
                _remove_job(jobid)
            else:
                # EMPTY transcription. A tiny take is a genuine (accidental)
                # empty -> drop quietly. A non-trivial take almost never is:
                # do at most EMPTY_RETRY_ATTEMPTS immediate re-attempts (never
                # a loop), then PRESERVE the audio so it is never lost.
                m = _jobs.get(jobid)
                trivial = _wav_size(wav) < config.EMPTY_MIN_WAV_BYTES
                if m is None or trivial:
                    _remove_job(jobid)
                else:
                    m["empty_attempts"] = m.get("empty_attempts", 0) + 1
                    if m["empty_attempts"] <= config.EMPTY_RETRY_ATTEMPTS:
                        m["next_try_ts"] = time.time()  # bounded immediate retry
                        _write_sidecar(jobid, m)
                        empty_retry = True
                    else:
                        _move_to_unresolved(jobid)
                        empty_preserved = True
        else:
            m = _jobs.get(jobid)
            if m is not None:
                m["attempts"] += 1
                first_failure = m["attempts"] == 1
                m["ever_deferred"] = True
                # Give up if the error is PERMANENT (400/401/422...: retrying
                # cannot help) or if the attempt cap is reached. The audio is
                # KEPT in unresolved/ (manual retry from the menu), never
                # re-run automatically. Otherwise reschedule with back-off.
                if not retriable or m["attempts"] >= config.RETRY_MAX_ATTEMPTS:
                    gave_up = True
                    _move_to_unresolved(jobid)
                else:
                    m["next_try_ts"] = time.time() + _backoff_for(m["attempts"])
                    _write_sidecar(jobid, m)
    _notify_state()  # -> idle / blue (depending on remaining jobs)

    if ok:
        _log(
            f"job {jobid} transcribed ok: {len(text or '')} chars, "
            f"deferred={ever_deferred}"
        )
        if text:
            # Log BEFORE delivery: the trace exists no matter what.
            try:
                history.append(text)
            except Exception:  # noqa: BLE001
                pass
            cb = deliver_deferred if ever_deferred else deliver_immediate
            _log(
                "delivering via "
                + ("deliver_deferred (clipboard)" if ever_deferred
                   else "deliver_immediate (paste)")
            )
            if cb is not None:
                try:
                    cb(text)
                except Exception:  # noqa: BLE001
                    pass
        elif empty_retry:
            _log(f"job {jobid} empty -> bounded immediate re-attempt")
        elif empty_preserved:
            _log(f"job {jobid} empty -> preserved in unresolved/ (audio kept)")
            cb = on_empty or on_permanent_error or on_error
            if cb is not None:
                try:
                    cb("Empty transcription — audio kept, retry from the 🎙 menu")
                except Exception:  # noqa: BLE001
                    pass
        # trivial empty take: nothing to deliver (job already removed).
    elif gave_up:
        # DEFINITIVE failure (audio kept in unresolved/): we report it every
        # time (it is a give-up, not a mere wait) via on_permanent_error ->
        # error flash + notification. Fall back to on_error if not wired.
        _log(f"job {jobid} given up -> unresolved/ (audio kept)")
        cb = on_permanent_error or on_error
        if err_msg and cb is not None:
            try:
                cb(f"{err_msg} — audio kept, retry from the 🎙 menu")
            except Exception:  # noqa: BLE001
                pass
    elif first_failure and err_msg and on_error is not None:
        # First TRANSIENT failure only (not every retry, to avoid spam).
        # The retry continues in the background; the dot stays blue.
        try:
            on_error(err_msg)
        except Exception:  # noqa: BLE001
            pass
