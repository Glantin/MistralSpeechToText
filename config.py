"""Shared constants for MistralSpeechToText.

The trigger key is isolated here so it can be swapped easily (Right Option by
default; fallbacks are listed below).
"""

import os

# --- Trigger key ----------------------------------------------------------
# macOS keycodes (virtual key codes) of the left/right modifiers.
#   58 = Left Option   61 = Right Option
#   59 = Left Control  62 = Right Control
#   56 = Left Shift    60 = Right Shift
# We trigger on the RIGHT Option: the Left Option stays free for accents
# (e, e, c...).
TRIGGER_KEYCODE = 61  # Right Option

# Flag mask for the Option family (kCGEventFlagMaskAlternate).
# Used to tell a press (down) from a release (up) on flagsChanged.
TRIGGER_FLAG_MASK = 0x00080000  # NSEventModifierFlagOption

# If you want to switch to Right Control:
#   TRIGGER_KEYCODE = 62
#   TRIGGER_FLAG_MASK = 0x00040000  # kCGEventFlagMaskControl

# Space key (to switch to continuous listening while held).
SPACE_KEYCODE = 49

# Escape key: cancels the in-progress recording (drops the take, no
# transcription or paste).
ESCAPE_KEYCODE = 53

# --- Audio ----------------------------------------------------------------
SAMPLE_RATE = 16000  # 16 kHz, enough and light for STT
CHANNELS = 1
# Max duration of a take. This is the ONLY place a long take is actually cut:
# past it we stop accumulating audio (the start is still transcribed) so the
# buffer cannot grow unbounded on a forgotten hands-free listen. It is USER
# CONFIGURABLE (menu > "Recording limit…", persisted per user): the default is
# kept low for RAM, but the user can raise it up to the API ceiling.
#   - default 10 min ~ 18 MB of RAM at 16 kHz mono int16;
#   - ceiling 60 min ~ 110 MB — the real Mistral Voxtral limit (60 min / 500 MB).
# The effective value is read via settings.get_max_record_seconds().
MAX_RECORD_DEFAULT_MINUTES = 10
MAX_RECORD_CEILING_MINUTES = 60  # hard cap: the Voxtral transcription API's limit

# --- Transcription --------------------------------------------------------
MISTRAL_MODEL = "voxtral-mini-latest"
# We do NOT set a language: auto-detect mixed FR/EN ("franglais").
MISTRAL_LANGUAGE = None

# --- Network / HTTP timeouts ---------------------------------------------
# Without an explicit timeout, a network change/loss leaves the request HANGING
# indefinitely: the transcription thread stays blocked and the dot freezes. So
# we bound the call.
#   - short CONNECT: a dead network fails fast -> we switch to retry;
#   - generous READ: covers the upload + transcription of a long audio.
HTTP_CONNECT_TIMEOUT = 10.0   # seconds
HTTP_READ_TIMEOUT = 180.0     # seconds

# --- Retry (persistent queue) --------------------------------------------
# When a transcription fails (network), the WAV is NOT dropped: it is kept and
# retried in the background on this back-off (seconds; the last value is then
# repeated), until success. So a long take is never lost, even after an app
# restart.
RETRY_BACKOFF_SECONDS = [2, 5, 15, 30, 60, 120, 300]
# Cap on TRANSIENT attempts: past it we give up even on network (anti-loop
# guard-rail, on top of max age). A PERMANENT error (400/401/422...) is given up
# on the very first attempt (see transcribe_queue).
RETRY_MAX_ATTEMPTS = 12
# Past this age, a pending job is purged (anti-accumulation guard-rail).
PENDING_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days

# --- Feedback -------------------------------------------------------------
# macOS system sounds played on transitions (None to disable).
SOUND_START = "/System/Library/Sounds/Tink.aiff"
SOUND_DONE = "/System/Library/Sounds/Pop.aiff"
# Sound played ONCE as a take nears the recording limit (a "wrap up" reminder).
# Distinct from the other two. None to disable.
SOUND_WARN = "/System/Library/Sounds/Sosumi.aiff"

# --- "Approaching the limit" warning --------------------------------------
# The warning is NOT an arbitrary duration: it is anchored to the real cut-off
# point (the recording limit above). This many seconds BEFORE that limit, the dot
# switches to "warn" mode (distinct color + pulse) and a sound plays ONCE. The
# recording CONTINUES normally; it is just a heads-up to wrap up before audio
# starts being dropped. Not user-tunable (the limit is what the user adjusts).
RECORD_WARN_LEAD_SECONDS = 90

# --- Visual indicator -----------------------------------------------------
# Small floating dot (NSPanel) at the bottom center of the screen:
#   red = recording, amber = transcription in progress, hidden = idle.
INDICATOR_ENABLED = True
# Two cadences for the main-thread loop that drives the UI (and yields to Ctrl+C
# in the CLI):
#   - FAST when the dot is VISIBLE (recording/transcription): it is re-asserted
#     to the front often, so it stays on top even if an app goes full-screen;
#   - IDLE: much slower, since there is then almost nothing to do. This avoids 10
#     needless wake-ups/s forever (a permanent CPU cost, noticeable on Intel
#     Macs). The dot still appears instantly: the core wakes the main thread on
#     each state change (core.on_ui_state_change).
INDICATOR_TICK_SECONDS = 0.1        # fast cadence (dot visible)
INDICATOR_TICK_IDLE_SECONDS = 0.75  # idle cadence
# Cursor following: the dot sticks near the mouse (so on the active window, where
# you type) instead of being pinned at the bottom center above the Dock. Set to
# False to go back to the fixed bottom-center point (one per screen).
INDICATOR_FOLLOW_CURSOR = True
# Offset (dx, dy) in points relative to the cursor, in screen coords (bottom-left
# origin): bottom-right so as not to hide the pointer.
INDICATOR_CURSOR_OFFSET = (14.0, -18.0)

# --- Clipboard ------------------------------------------------------------
# Safety net: if True (default), the last transcription STAYS on the clipboard
# (the previous content is NOT restored). This way each dictation is captured by
# a clipboard-history manager (e.g. Raycast) and can be re-pasted by hand (Cmd+V)
# if the automatic paste got lost. Set to False for the old behavior (clipboard
# restore).
KEEP_LAST_IN_CLIPBOARD = True

# --- History --------------------------------------------------------------
# Every successful transcription is logged here (one JSON line).
HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "history.jsonl"
)
# Number of entries shown by default by `history.py`.
HISTORY_DEFAULT_N = 20

# --- User storage (outside the project folder) ---------------------------
# We keep runtime data next to the API key, in Application Support (the packaged
# .app has no writable project folder).
import credentials  # noqa: E402  (avoids a cycle: credentials does not import config)

# Retry queue: WAVs awaiting transcription (+ .json sidecars).
PENDING_DIR = os.path.join(credentials.APP_SUPPORT_DIR, "pending")
# Custom vocabulary dictionary (one entry per line, '#' = comment). Passed as-is
# to the API via context_bias: no extra request/credit.
VOCAB_FILE = os.path.join(credentials.APP_SUPPORT_DIR, "vocabulary.txt")
