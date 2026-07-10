"""Transcription via l'API Mistral (Voxtral Mini Transcribe).

Endpoint: POST https://api.mistral.ai/v1/audio/transcriptions
On NE passe PAS de langue : auto-detection du franglais (melange FR/EN).
"""

import os
import ssl
import sys

import httpx
from dotenv import load_dotenv
from mistralai.client import Mistral

import config
import credentials

# En mode dev, charge un .env du dossier projet dans l'environnement ; la .app
# n'en a pas, mais credentials.get_api_key() lira alors le fichier Application
# Support. load_dotenv() est sans effet (et sans erreur) s'il n'y a pas de .env.
load_dotenv()

_client: Mistral | None = None


def _cato_bundle_path() -> str | None:
    """Chemin du bundle CA livre avec le projet / embarque dans la .app.

    PyInstaller expose les ressources via sys._MEIPASS ; en dev on part du
    dossier du module.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "certs", "cato-bundle.pem")
    return p if os.path.exists(p) else None


def _ssl_context() -> ssl.SSLContext:
    """Contexte SSL tolerant aux proxys TLS d'entreprise.

    Ordre de resolution :
      1. SSL_CERT_FILE : override explicite (utilisateur/IT) -> on le respecte ;
      2. defaut : trousseau macOS via truststore. Le certificat racine du proxy
         (Cato/Zscaler...) installe par l'IT y est deja, donc la transcription
         marche sans manip. C'est le chemin robuste (jamais perime) ;
      3. filet de secours : si truststore echoue, on retombe sur le bundle
         certs/cato-bundle.pem s'il existe, sinon sur certifi (defaut).
    """
    env = os.environ.get("SSL_CERT_FILE")
    if env and os.path.exists(env):
        return ssl.create_default_context(cafile=env)
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 -- truststore indisponible/incompatible
        bundle = _cato_bundle_path()
        return ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()


def _make_client(api_key: str) -> Mistral:
    """Client Mistral avec un transport HTTP qui valide via le trousseau macOS."""
    ctx = _ssl_context()
    return Mistral(
        api_key=api_key,
        client=httpx.Client(verify=ctx),
        async_client=httpx.AsyncClient(verify=ctx),
    )


def _get_client() -> Mistral:
    global _client
    if _client is None:
        api_key = credentials.get_api_key()
        if not api_key:
            raise RuntimeError(
                "MISTRAL_API_KEY manquante. Renseigne ta cle dans l'app "
                "(menu > Saisir la cle API), ou via un .env en mode dev."
            )
        _client = _make_client(api_key)
    return _client


def reset_client() -> None:
    """Force la recreation du client (apres changement de cle dans l'app)."""
    global _client
    _client = None


def classify_error(exc: Exception) -> tuple[str, str]:
    """Classe une exception reseau/API et renvoie (kind, message actionnable).

    On teste le SSL/certificat EN PREMIER : derriere un proxy d'entreprise,
    l'echec est un CERTIFICATE_VERIFY_FAILED, PAS un probleme de cle. L'inverser
    ferait dire a tort "cle refusee". kind ∈ {"ssl","auth","missing","network",
    "other"}.
    """
    low = str(exc).lower()
    if "certificate" in low or "ssl" in low or "self-signed" in low or "self signed" in low:
        return "ssl", (
            "Proxy TLS d'entreprise : le certificat n'est pas reconnu. "
            "(Ce n'est pas ta clé.) Voir la section proxy du README."
        )
    # Auth AVANT "manquante" : "invalid api key" contient "api key" et serait
    # sinon classe a tort comme cle manquante.
    if "401" in low or "unauthorized" in low or ("invalid" in low and "key" in low):
        return "auth", "Clé refusée (401). Vérifie ta clé Mistral."
    if "manquante" in low or "api_key" in low:
        return "missing", "Clé API manquante. Renseigne-la dans le menu 🎙."
    if "connection" in low or "timeout" in low or "network" in low or "resolve" in low:
        return "network", "Pas de réseau ou API injoignable. Réessaie."
    return "other", f"Échec de la transcription : {str(exc)[:140]}"


def test_api_key() -> tuple[bool, str]:
    """Verifie la cle courante par un appel leger (liste des modeles).

    Renvoie (ok, message). Cree un client jetable (meme transport TLS que le
    client reel) pour tester EXACTEMENT la cle enregistree, sans toucher au
    client en cache. Un echec SSL est distingue d'une cle refusee.
    """
    key = credentials.get_api_key()
    if not key:
        return False, "Aucune clé renseignée."
    try:
        _make_client(key).models.list()
        return True, "Clé valide ✅"
    except Exception as exc:  # noqa: BLE001
        return False, classify_error(exc)[1]


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
