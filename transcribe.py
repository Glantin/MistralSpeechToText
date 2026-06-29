"""Transcription via l'API Mistral (Voxtral Mini Transcribe).

Endpoint: POST https://api.mistral.ai/v1/audio/transcriptions
On NE passe PAS de langue : auto-detection du franglais (melange FR/EN).
"""

import os

from dotenv import load_dotenv
from mistralai.client import Mistral

import config
import credentials

# En mode dev, charge un .env du dossier projet dans l'environnement ; la .app
# n'en a pas, mais credentials.get_api_key() lira alors le fichier Application
# Support. load_dotenv() est sans effet (et sans erreur) s'il n'y a pas de .env.
load_dotenv()

_client: Mistral | None = None


def _get_client() -> Mistral:
    global _client
    if _client is None:
        api_key = credentials.get_api_key()
        if not api_key:
            raise RuntimeError(
                "MISTRAL_API_KEY manquante. Renseigne ta cle dans l'app "
                "(menu > Saisir la cle API), ou via un .env en mode dev."
            )
        _client = Mistral(api_key=api_key)
    return _client


def reset_client() -> None:
    """Force la recreation du client (apres changement de cle dans l'app)."""
    global _client
    _client = None


def test_api_key() -> tuple[bool, str]:
    """Verifie la cle courante par un appel leger (liste des modeles).

    Renvoie (ok, message). Cree un client jetable pour tester EXACTEMENT la cle
    enregistree, sans toucher au client en cache.
    """
    key = credentials.get_api_key()
    if not key:
        return False, "Aucune clé renseignée."
    try:
        Mistral(api_key=key).models.list()
        return True, "Clé valide ✅"
    except Exception as exc:  # noqa: BLE001
        return False, f"Clé refusée : {str(exc)[:140]}"


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
