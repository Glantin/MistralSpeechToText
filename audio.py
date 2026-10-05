"""Non-blocking mic capture via sounddevice, exported as 16 kHz mono WAV."""

import tempfile
import threading
import wave

import numpy as np
import sounddevice as sd

import applog
import config
import settings

# Max time (s) a CoreAudio call may take before we consider the stream WEDGED.
# PortAudio on macOS can deadlock in Pa_StopStream (AudioDeviceStop never
# returns, typically after a device change: AirPods, sleep...). Without a bound,
# the calling thread freezes forever — and if it is the main thread (Quit), the
# whole app freezes with its icon still in the menu bar.
STOP_TIMEOUT_SECONDS = 2.0
OPEN_TIMEOUT_SECONDS = 3.0


class MicUnavailable(RuntimeError):
    """The microphone could not be opened (or CoreAudio is wedged)."""


# Streams whose stop/close hung: kept referenced so the garbage collector never
# finalizes (closes) them on some other thread — that would hang it too.
_abandoned: list = []


def _call_with_timeout(fn, seconds: float) -> tuple[bool, BaseException | None]:
    """Run fn() on a disposable daemon thread, wait at most `seconds`.

    Returns (finished, exception). finished=False means fn is still blocked: the
    thread is abandoned (daemon, it never prevents the app from exiting)."""
    box: dict = {}

    def _run() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_run, daemon=True, name="audio-call")
    t.start()
    t.join(seconds)
    if t.is_alive():
        return False, None
    return True, box.get("exc")


class Recorder:
    """Records the mic continuously into a buffer while active.

    Usage:
        rec = Recorder()
        rec.start()
        ...
        wav_path = rec.stop()  # None if nothing was captured
    """

    def __init__(self):
        self._frames: list[np.ndarray] = []
        self._nsamples = 0
        self._max_samples = config.SAMPLE_RATE * settings.get_max_record_seconds()
        self._stream: sd.InputStream | None = None
        self._capturing = False
        # True once a stream stop/close hung: CoreAudio is stuck for this process,
        # touching it again would freeze another thread. Only a restart clears it.
        self.wedged = False

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        # Called from PortAudio's audio thread: copy and accumulate.
        if status:
            print(f"[audio] status: {status}")
        if not self._capturing:
            return
        # Memory guard-rail: past the recording limit we stop accumulating (the
        # already-captured start is still transcribed; avoids an unbounded buffer
        # on a forgotten continuous listen).
        if self._nsamples >= self._max_samples:
            return
        self._frames.append(indata.copy())
        self._nsamples += len(indata)

    def start(self) -> None:
        """Open the mic and start capturing. Raises MicUnavailable on failure.

        Never blocks more than a few seconds: each CoreAudio call is bounded."""
        if self.wedged:
            raise MicUnavailable("audio system wedged (restart the app)")
        self._frames = []
        self._nsamples = 0
        # Read the (user-adjustable) limit at each take start, so a settings
        # change applies to the next recording without a restart.
        self._max_samples = config.SAMPLE_RATE * settings.get_max_record_seconds()
        last_exc: BaseException | None = None
        for attempt in (1, 2):
            if attempt == 2:
                # Refresh PortAudio's device list: it is frozen at import time,
                # so a mic that appeared later (app launched at login before the
                # audio system was ready, AirPods plugged in) is invisible.
                done, _ = _call_with_timeout(_refresh_devices, OPEN_TIMEOUT_SECONDS)
                if not done:
                    self.wedged = True
                    applog.log("audio: device refresh hung -> wedged")
                    raise MicUnavailable("device refresh hung")
            box: dict = {}

            def _open(box: dict = box) -> None:
                stream = sd.InputStream(
                    samplerate=config.SAMPLE_RATE,
                    channels=config.CHANNELS,
                    dtype="int16",
                    callback=self._callback,
                )
                box["stream"] = stream
                stream.start()

            self._capturing = True
            done, exc = _call_with_timeout(_open, OPEN_TIMEOUT_SECONDS)
            if not done:
                self._capturing = False
                if "stream" in box:
                    _abandoned.append(box["stream"])
                self.wedged = True
                applog.log("audio: stream open hung -> wedged")
                raise MicUnavailable("stream open hung")
            if exc is None:
                self._stream = box["stream"]
                return
            self._capturing = False
            last_exc = exc
            applog.log(f"audio: open attempt {attempt} failed: {exc!r}")
            if "stream" in box:
                _call_with_timeout(box["stream"].close, STOP_TIMEOUT_SECONDS)
        raise MicUnavailable(str(last_exc))

    def stop(self) -> str | None:
        """Stop capture and write a temporary WAV. Returns its path.

        Never blocks more than STOP_TIMEOUT_SECONDS: if CoreAudio hangs, the
        stream is abandoned (self.wedged) but the audio already captured is still
        written, so the dictation is not lost."""
        self._capturing = False
        frames, self._frames = self._frames, []
        stream, self._stream = self._stream, None
        if stream is not None:

            def _close() -> None:
                stream.stop()
                stream.close()

            done, exc = _call_with_timeout(_close, STOP_TIMEOUT_SECONDS)
            if not done:
                _abandoned.append(stream)
                self.wedged = True
                applog.log("audio: stream stop hung -> abandoned, wedged")
            elif exc is not None:
                applog.log(f"audio: stream stop failed: {exc!r}")

        if not frames:
            return None

        audio = np.concatenate(frames, axis=0)

        # Ignore takes that are too short (accidental click < ~0.25 s).
        if len(audio) < config.SAMPLE_RATE // 4:
            return None

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="mistral-stt-")
        import os

        os.close(fd)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(config.CHANNELS)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(config.SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return path


def _refresh_devices() -> None:
    """Re-initialize PortAudio so it re-scans the audio devices."""
    sd._terminate()
    sd._initialize()


def list_input_devices() -> str:
    """Return a readable list of input devices (debug)."""
    lines = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            lines.append(f"  [{idx}] {dev['name']}")
    return "\n".join(lines) or "  (no microphone detected)"


if __name__ == "__main__":
    # Isolated test: record 3 seconds and write a WAV.
    import time

    print("Available microphones:")
    print(list_input_devices())
    print("\nRecording 3 s... speak!")
    r = Recorder()
    r.start()
    time.sleep(3)
    out = r.stop()
    print(f"Written: {out}")
