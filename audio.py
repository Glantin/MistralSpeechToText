"""Capture micro non bloquante via sounddevice, export WAV 16 kHz mono."""

import tempfile
import wave

import numpy as np
import sounddevice as sd

import config


class Recorder:
    """Enregistre le micro en continu dans un buffer tant qu'il est actif.

    Usage:
        rec = Recorder()
        rec.start()
        ...
        wav_path = rec.stop()  # None si rien n'a ete capte
    """

    def __init__(self):
        self._frames: list[np.ndarray] = []
        self._nsamples = 0
        self._max_samples = config.SAMPLE_RATE * config.MAX_RECORD_SECONDS
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        # Appele depuis le thread audio de PortAudio : on copie, on accumule.
        if status:
            print(f"[audio] status: {status}")
        # Garde-fou memoire : au-dela de MAX_RECORD_SECONDS on cesse d'accumuler
        # (le debut deja capte reste transcrit ; evite un buffer sans borne en
        # ecoute continue oubliee).
        if self._nsamples >= self._max_samples:
            return
        self._frames.append(indata.copy())
        self._nsamples += len(indata)

    def start(self) -> None:
        self._frames = []
        self._nsamples = 0
        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> str | None:
        """Arrete la capture et ecrit un WAV temporaire. Retourne son chemin."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if not self._frames:
            return None

        audio = np.concatenate(self._frames, axis=0)
        self._frames = []

        # Ignore les captures trop courtes (clic accidentel < ~0.25 s).
        if len(audio) < config.SAMPLE_RATE // 4:
            return None

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="mistral-stt-")
        import os

        os.close(fd)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(config.CHANNELS)
            wf.setsampwidth(2)  # int16 = 2 octets
            wf.setframerate(config.SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return path


def list_input_devices() -> str:
    """Renvoie la liste lisible des peripheriques d'entree (debug)."""
    lines = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            lines.append(f"  [{idx}] {dev['name']}")
    return "\n".join(lines) or "  (aucun micro detecte)"


if __name__ == "__main__":
    # Test isole : enregistre 3 secondes et ecrit un WAV.
    import time

    print("Micros disponibles:")
    print(list_input_devices())
    print("\nEnregistrement 3 s... parle !")
    r = Recorder()
    r.start()
    time.sleep(3)
    out = r.stop()
    print(f"Ecrit: {out}")
