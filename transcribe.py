"""Transcription via l'API Mistral (Voxtral Mini Transcribe).

Endpoint: POST https://api.mistral.ai/v1/audio/transcriptions
On NE passe PAS de langue : auto-detection du franglais (melange FR/EN).
"""

import os

from dotenv import load_dotenv
from mistralai.client import Mistral

import config

load_dotenv()

_client: Mistral | None = None


def _get_client() -> Mistral:
    global _client
    if _client is None:
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise RuntimeError(
                "MISTRAL_API_KEY manquante. Copie .env.example en .env "
                "et renseigne ta cle."
            )
        _client = Mistral(api_key=api_key)
    return _client


def transcribe(wav_path: str) -> str:
    """Transcrit un fichier WAV et retourne le texte (strippe)."""
    client = _get_client()
    with open(wav_path, "rb") as f:
        kwargs = {
            "model": config.MISTRAL_MODEL,
            "file": {"content": f, "file_name": os.path.basename(wav_path)},
        }
        if config.MISTRAL_LANGUAGE:
            kwargs["language"] = config.MISTRAL_LANGUAGE
        resp = client.audio.transcriptions.complete(**kwargs)
    return (resp.text or "").strip()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python transcribe.py <fichier.wav>")
        raise SystemExit(1)
    print(transcribe(sys.argv[1]))
