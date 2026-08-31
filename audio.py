"""Non-blocking mic capture via sounddevice, exported as 16 kHz mono WAV."""

import tempfile
import wave

import numpy as np
import sounddevice as sd

import config
import settings


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

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        # Called from PortAudio's audio thread: copy and accumulate.
        if status:
            print(f"[audio] status: {status}")
        # Memory guard-rail: past the recording limit we stop accumulating (the
        # already-captured start is still transcribed; avoids an unbounded buffer
        # on a forgotten continuous listen).
        if self._nsamples >= self._max_samples:
            return
        self._frames.append(indata.copy())
        self._nsamples += len(indata)

    def start(self) -> None:
        self._frames = []
        self._nsamples = 0
        # Read the (user-adjustable) limit at each take start, so a settings
        # change applies to the next recording without a restart.
        self._max_samples = config.SAMPLE_RATE * settings.get_max_record_seconds()
        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> str | None:
        """Stop capture and write a temporary WAV. Returns its path."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if not self._frames:
            return None

        audio = np.concatenate(self._frames, axis=0)
        self._frames = []

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
