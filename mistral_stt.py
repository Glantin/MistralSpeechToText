"""MistralSpeechToText: local voice dictation (speech-to-text).

Hold RIGHT Option to speak -> release -> the text is inserted at the cursor.
While holding, press Space to switch to continuous (hands-free) listening; a new
press of Right Option stops and transcribes.

Run:        uv run python mistral_stt.py
Debug mode: MISTRAL_STT_DEBUG=1 uv run python mistral_stt.py
Quit:       Ctrl+C in the terminal.
"""

import os
import queue
import signal
import subprocess
import threading
import time

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
)
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopRunInMode,
    CFRunLoopStop,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CGEventMaskBit,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
    kCFRunLoopDefaultMode,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGEventTapDisabledByTimeout,
    kCGEventTapDisabledByUserInput,
    kCGEventTapOptionDefault,
    kCGHeadInsertEventTap,
    kCGKeyboardEventKeycode,
    kCGSessionEventTap,
)

import config
import settings
import transcribe_queue
from audio import Recorder, list_input_devices
from inserter import insert_at_cursor, set_clipboard

DEBUG = bool(os.environ.get("MISTRAL_STT_DEBUG"))

# States
IDLE = "idle"
RECORDING_PTT = "ptt"
RECORDING_CONTINUOUS = "continuous"

state = IDLE
recorder = Recorder()
_actions: "queue.Queue[str]" = queue.Queue()
_tap = None
_tap_thread = None
_running = True

# Visual-indicator state, shared across threads (read: main thread).
# Values: "idle" | "recording" | "recording_long" | "transcribing" | "retrying"
#         | "recovered" | "error" | "cancelled".
# A plain string assignment is atomic under the GIL; the main thread only READS
# it to drive the dot.
#
# Most states are DERIVED from the real state (see _recompute_ui): the recording
# flag + the transcription queue's counters. This avoids the old bug where the
# keyboard callback wrote "recording" optimistically and ONLY the worker
# (sometimes frozen on a network call) could reset it to "idle" -> dot stuck red.
# Now the "recording" state is backed by a flag the tap RAISES and LOWERS itself.
_ui_state = "idle"

# Flag: a mic recording is active (raised/lowered by the keyboard tap).
_recording_active = False

# Duration tracking of the current take (the "long take" warning).
# _recording_started_at: monotonic timestamp of the take's start (None when idle).
# _warned_long: guard so the warning sound plays only ONCE.
_recording_started_at: "float | None" = None
_warned_long = False

# Optional hook invoked (from ANY thread) on every change of _ui_state. app.py
# wires a main-thread wake-up (performSelectorOnMainThread) so the dot appears
# IMMEDIATELY, without waiting for the next tick. This way the timer's idle
# cadence can be slow with no visible latency.
on_ui_state_change = None


def _set_ui_state(value: str) -> None:
    """Set the shared UI state and notify the UI (if a hook is wired)."""
    global _ui_state
    _ui_state = value
    cb = on_ui_state_change
    if cb is not None:
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass


def _recompute_ui() -> None:
    """Recompute the dot from the REAL state, by priority order.

    recording (tap flag) > transcribing (attempt in progress) > retrying (jobs
    waiting for the network) > idle. The transient "cancelled" and "recovered"
    (flash) states are set directly elsewhere and do NOT go through here (their
    animation ends on its own; no recompute overwrites them until another event
    happens)."""
    if _recording_active:
        state = "recording"
    elif transcribe_queue.active_count() > 0:
        state = "transcribing"
    elif transcribe_queue.pending_count() > 0:
        state = "retrying"
    else:
        state = "idle"
    _set_ui_state(state)


def _warn_threshold_seconds() -> float:
    """When (seconds into a take) to warn: RECORD_WARN_LEAD_SECONDS before the cap.

    Anchored to the real cut-off (the user-adjustable recording limit), never an
    arbitrary duration. For a very short custom limit (< the lead), we fall back
    to 75% of it so the reminder still lands before audio is dropped."""
    cap = settings.get_max_record_seconds()
    lead = config.RECORD_WARN_LEAD_SECONDS
    return cap - lead if cap > lead else cap * 0.75


def maybe_warn_long_recording() -> None:
    """Warn as a take nears the recording limit (never cuts it off).

    Call on EVERY tick of both UI drivers (the CLI loop and the .app NSTimer).
    Recording is NEVER interrupted: about RECORD_WARN_LEAD_SECONDS before the
    (user-adjustable) limit we play a sound ONCE and switch the dot to
    "recording_long" (distinct color + pulse) as a heads-up to wrap up. Returns
    immediately when idle (nothing to do outside a take)."""
    global _warned_long
    if not _recording_active or _recording_started_at is None:
        return
    if time.monotonic() - _recording_started_at < _warn_threshold_seconds():
        return
    if not _warned_long:
        _warned_long = True
        _play(config.SOUND_WARN)
    if _ui_state != "recording_long":
        _set_ui_state("recording_long")


# Queue of user errors (e.g. invalid key, network, TLS proxy). The worker drops a
# message here; the app (app.py) drains it on the main thread to show a macOS
# notification. In CLI mode, the error is only printed.
errors: "queue.Queue[str]" = queue.Queue()

# Queue of POSITIVE notifications (e.g. a deferred transcription recovered).
# Drained like `errors` by the app (app.py) into a macOS notification.
notices: "queue.Queue[str]" = queue.Queue()


def _deliver_immediate(text: str) -> None:
    """Delivery of a transcription that succeeded on the first try: paste at cursor."""
    print(f"[mistral-stt] inserted ({len(text)} characters):")
    print(text)
    insert_at_cursor(text, restore=not config.KEEP_LAST_IN_CLIPBOARD)


def _deliver_deferred(text: str) -> None:
    """Delivery of a transcription RECOVERED after the fact (network retry).

    We do NOT paste at the cursor (it has moved since): we put the text on the
    clipboard, signal it via the dot (green flash) and a notification."""
    print(f"[mistral-stt] transcription recovered ({len(text)} characters):")
    print(text)
    set_clipboard(text)
    _set_ui_state("recovered")  # green flash (visual confirmation)
    notices.put("Transcription recovered — on the clipboard ✅")


def _deliver_error(message: str) -> None:
    """DEFINITIVE failure of a job (permanent error: args/auth/validation).

    We do NOT stay blue (network wait): a bright orange flash (error dot) + a
    clear notification, then back to idle. Distinct from a mere wait."""
    print(f"[mistral-stt] definitive failure: {message}")
    _set_ui_state("error")  # orange flash (failure, distinct from the 'waiting' blue)
    errors.put(message)


def _log(msg: str) -> None:
    if DEBUG:
        print(f"[mistral-stt:debug] {msg}")


def _play(sound: str | None) -> None:
    if sound:
        subprocess.Popen(
            ["afplay", sound],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _worker() -> None:
    """Run mic start/stop/cancel OFF the tap thread.

    The tap callback must stay ultra-fast: if it blocks (opening the mic
    stream...), macOS disables the tap and no more events arrive (including the
    release). So we offload recording here. Transcription lives on yet another
    thread (transcribe_queue): we ENQUEUE the take and return immediately.
    """
    while True:
        action = _actions.get()
        if action == "__quit__":
            return
        if action == "start":
            recorder.start()
            _play(config.SOUND_START)
            print("[mistral-stt] recording...")
        elif action == "cancel":
            # Cancel (Esc): drop the take, without transcribing or pasting.
            wav_path = recorder.stop()
            if wav_path:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
            print("[mistral-stt] cancelled")
            # The visual flash was already armed by the callback (_ui_state).
        elif action == "stop":
            wav_path = recorder.stop()
            _play(config.SOUND_DONE)
            if not wav_path:
                print("[mistral-stt] (nothing to transcribe)")
                _recompute_ui()
                continue
            # We ENQUEUE the take (separate transcription thread + persistent
            # retry). Transcription no longer blocks this worker: a slow/dropped
            # network call no longer prevents a new recording, and the audio is
            # kept on disk until a transcript is obtained.
            print("[mistral-stt] transcription in progress...")
            transcribe_queue.enqueue(wav_path)


def _tap_callback(proxy, type_, event, refcon):  # noqa: ARG001
    global state, _ui_state, _recording_active
    global _recording_started_at, _warned_long

    # The system can disable the tap (callback too slow, special key event). We
    # must re-enable it, otherwise no more events arrive.
    if type_ in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        _log("tap disabled by the system -> re-enabling")
        if _tap is not None:
            CGEventTapEnable(_tap, True)
        return event

    try:
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)

        # --- Right Option (flagsChanged) ---
        if type_ == kCGEventFlagsChanged and keycode == config.TRIGGER_KEYCODE:
            flags = CGEventGetFlags(event)
            is_down = bool(flags & config.TRIGGER_FLAG_MASK)
            _log(f"trigger {'DOWN' if is_down else 'UP'} (state={state})")
            if is_down:
                if state == IDLE:
                    state = RECORDING_PTT
                    # Stay ultra-fast: RAISE the recording flag and recompute the
                    # dot (red on press, minimal latency). The flag is LOWERED by
                    # this same callback on release, so the dot can no longer stay
                    # stuck red even if transcription (another thread) is
                    # slow/blocked.
                    _recording_active = True
                    _recording_started_at = time.monotonic()
                    _warned_long = False
                    _recompute_ui()
                    _actions.put("start")
                elif state == RECORDING_CONTINUOUS:
                    state = IDLE
                    _recording_active = False
                    _recording_started_at = None
                    _recompute_ui()
                    _actions.put("stop")
            else:  # release
                if state == RECORDING_PTT:
                    state = IDLE
                    _recording_active = False
                    _recording_started_at = None
                    _recompute_ui()
                    _actions.put("stop")
            return event

        # --- Space during a PTT hold -> switch to hands-free ---
        if type_ == kCGEventKeyDown and keycode == config.SPACE_KEYCODE:
            if state == RECORDING_PTT:
                state = RECORDING_CONTINUOUS
                print(
                    "[mistral-stt] continuous listening (hands-free) — "
                    "Right Option to stop"
                )
                return None  # swallow the triggering Space

        # --- Esc during a recording -> cancel ---
        if type_ == kCGEventKeyDown and keycode == config.ESCAPE_KEYCODE:
            if state in (RECORDING_PTT, RECORDING_CONTINUOUS):
                state = IDLE
                _recording_active = False
                _recording_started_at = None
                _set_ui_state("cancelled")  # arm the confirmation flash (direct)
                _actions.put("cancel")
                return None  # swallow Esc (only during a recording)

        if DEBUG and type_ == kCGEventFlagsChanged:
            _log(f"flagsChanged keycode={keycode} (ignored)")
    except Exception as exc:  # noqa: BLE001
        _log(f"callback error: {exc}")

    return event


def _sigint(signum, frame):  # noqa: ARG001
    global _running
    _running = False


# --- Shared core (reused by mistral_stt.py CLI AND by app.py) --------------

def start_worker() -> "threading.Thread":
    """Start the RECORDING worker thread (mic start/stop/cancel).

    Transcription lives on a separate thread (start_transcribe_worker): this
    worker only does fast operations (opening/closing the mic, writing the WAV,
    enqueuing), it never blocks on the network."""
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def start_transcribe_worker() -> "threading.Thread | None":
    """Wire the callbacks (state/delivery) and start the transcription worker.

    Idempotent. Call once at startup (CLI and .app)."""
    transcribe_queue.on_state_change = _recompute_ui
    transcribe_queue.deliver_immediate = _deliver_immediate
    transcribe_queue.deliver_deferred = _deliver_deferred
    transcribe_queue.on_error = errors.put
    transcribe_queue.on_permanent_error = _deliver_error
    # Create the vocabulary dictionary (with a help header) if it is missing.
    try:
        import transcribe as _t

        _t.ensure_vocab_file()
    except Exception:  # noqa: BLE001
        pass
    return transcribe_queue.start()


def recover_pending() -> int:
    """Resume the pending takes left by a previous session."""
    return transcribe_queue.recover_pending()


def _tap_thread_main(ready: "threading.Event", result: dict) -> None:
    """Create the tap and PUMP its own run loop, on a DEDICATED thread.

    The tap must be created and its source added on the thread that pumps the run
    loop. This isolates keystroke delivery from the main AppKit thread: even if
    that one is busy (timer) or blocked in a modal window (NSAlert.runModal,
    onboarding), the keyboard callback keeps being served here -> no more
    system-wide keyboard latency.
    """
    global _tap

    mask = CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown)
    tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,
        mask,
        _tap_callback,
        None,
    )
    if tap is None:
        # "Input Monitoring" permission missing: warn the caller and let the
        # thread end (it will be restarted once the permission is granted).
        result["ok"] = False
        ready.set()
        return

    source = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    _tap = tap
    result["ok"] = True
    ready.set()
    # Block here for the life of the process (daemon thread): this dedicated run
    # loop is what serves the keyboard callback.
    CFRunLoopRun()


def install_event_tap() -> bool:
    """Start (if needed) the dedicated thread that carries the keyboard event tap.

    Returns True if the tap is in place, False if macOS refused it ("Input
    Monitoring" permission missing). Idempotent: does not recreate a tap already
    installed and does not restart a thread already initializing. Can be called
    from any thread (typically the main thread).
    """
    global _tap_thread
    if _tap is not None:
        return True
    # Thread already started but tap not ready yet: do not launch a 2nd one.
    if _tap_thread is not None and _tap_thread.is_alive():
        return _tap is not None

    ready = threading.Event()
    result: dict = {"ok": False}
    _tap_thread = threading.Thread(
        target=_tap_thread_main, args=(ready, result), daemon=True
    )
    _tap_thread.start()
    # Bounded wait: creating the tap is near-instant; the timeout avoids any
    # blocking of the main thread if something goes wrong.
    ready.wait(timeout=2.0)
    return bool(result.get("ok"))


def main() -> None:
    global _tap

    print("MistralSpeechToText — voice dictation (Mistral Voxtral)")
    print("Detected microphones:")
    print(list_input_devices())
    print(
        "\nHold RIGHT Option to speak. "
        "Right Option + Space = continuous listening. Ctrl+C to quit."
    )
    if DEBUG:
        print("[mistral-stt] debug mode on (MISTRAL_STT_DEBUG)")
    print()

    signal.signal(signal.SIGINT, _sigint)

    # Visual indicator (NSPanel). AppKit must live on the main thread; we
    # initialize NSApplication WITHOUT calling run(): it is the
    # CFRunLoopRunInMode loop below that pumps the run loop (and thus draws /
    # animates the dot), which preserves the clean Ctrl+C shutdown.
    indicator = None
    if config.INDICATOR_ENABLED:
        try:
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            app.finishLaunching()
            from indicator import Indicator

            indicator = Indicator()
        except Exception as exc:  # noqa: BLE001
            print(f"[mistral-stt] visual indicator disabled ({exc})")
            indicator = None

    start_worker()
    start_transcribe_worker()
    n = recover_pending()
    if n:
        print(f"[mistral-stt] {n} pending take(s) resumed (network).")

    if not install_event_tap():
        print(
            "[mistral-stt] Could not create the event tap.\n"
            "  -> System Settings > Privacy & Security >\n"
            "     Input Monitoring AND Accessibility: allow your terminal,\n"
            "     then relaunch."
        )
        raise SystemExit(1)

    # Loop that yields to Python on every tick: this is what lets Ctrl+C (SIGINT)
    # be handled (a bare CFRunLoopRun() would block it) AND what drives the visual
    # indicator (we read the shared state and refresh the dot only when it
    # changes).
    #
    # Adaptive cadence: slow when idle (nothing to do), fast when the dot is
    # visible (front re-assertion). A state change (tap/worker thread) wakes this
    # loop immediately via CFRunLoopStop -> no appearance latency despite the idle
    # cadence.
    if indicator is not None:
        main_loop = CFRunLoopGetCurrent()

        global on_ui_state_change
        on_ui_state_change = lambda: CFRunLoopStop(main_loop)  # noqa: E731

    last_rendered = None
    while _running:
        # "Long take" reminder (never cuts off the in-progress take).
        maybe_warn_long_recording()
        if indicator is None:
            tick = 0.25
        elif _ui_state in ("recording", "recording_long", "transcribing"):
            # "retrying"/"recovered" (network wait / flash) stay on the idle
            # cadence: the transition renders the dot immediately (via
            # on_ui_state_change), no need to force a permanent 10 Hz.
            tick = config.INDICATOR_TICK_SECONDS
        else:
            # Idle (including "cancelled" after the flash): slow cadence.
            tick = config.INDICATOR_TICK_IDLE_SECONDS
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, tick, False)
        if indicator is not None:
            s = _ui_state
            if s != last_rendered:
                indicator.render(s)
                last_rendered = s
            # Re-assert the front (survives full-screen transitions).
            indicator.tick()

    print("\n[mistral-stt] shutting down.")
    try:
        recorder.stop()
    except Exception:  # noqa: BLE001
        pass
    if indicator is not None:
        try:
            indicator.render("idle")
        except Exception:  # noqa: BLE001
            pass
    _actions.put("__quit__")


if __name__ == "__main__":
    main()
