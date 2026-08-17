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
    """Client Mistral avec un transport HTTP qui valide via le trousseau macOS.

    On fixe un TIMEOUT explicite : sans lui, un changement/perte de reseau laisse
    la requete suspendue indefiniment (thread de transcription bloque, pastille
    figee). CONNECT court -> un reseau mort echoue vite et bascule en reprise ;
    READ genereux -> couvre l'upload + la transcription d'un audio long.
    """
    ctx = _ssl_context()
    timeout = httpx.Timeout(
        connect=config.HTTP_CONNECT_TIMEOUT,
        read=config.HTTP_READ_TIMEOUT,
        write=config.HTTP_READ_TIMEOUT,
        pool=10.0,
    )
    return Mistral(
        api_key=api_key,
        client=httpx.Client(verify=ctx, timeout=timeout),
        async_client=httpx.AsyncClient(verify=ctx, timeout=timeout),
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


def http_status(exc: Exception) -> int | None:
    """Code HTTP porte par l'exception SDK/HTTP, ou None.

    Les erreurs mistralai heritent de MistralError qui expose `status_code`
    (int). On tente aussi quelques alias au cas ou, sans jamais deviner a partir
    du texte (un "400" dans un message d'erreur n'est pas forcement un statut).
    """
    for attr in ("status_code", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val <= 599:
            return val
    return None


def is_retriable(exc: Exception) -> bool:
    """True si l'erreur est TRANSITOIRE (on re-tente), False si PERMANENTE.

    Politique de reprise de la file (transcribe_queue) :
      - Transitoire -> retry backoff : timeouts, coupures reseau, 5xx, 429, 408.
      - Permanent -> abandon : 400 (args invalides, ex. context_bias), 401/403
        (auth), 404, 422 (validation)... et l'echec SSL d'un proxy d'entreprise
        (config a corriger, re-tenter en boucle ne ferait que rester bleu).
    Sans code HTTP exploitable, on se fie a la nature de l'erreur : reseau =
    transitoire ; le reste (inconnu) est traite comme transitoire mais plafonne
    par le nombre de tentatives / l'age max cote file.
    """
    status = http_status(exc)
    if status is not None:
        if status in (408, 429) or 500 <= status <= 599:
            return True
        if 400 <= status <= 499:
            return False
    kind = classify_error(exc)[0]
    if kind in ("ssl", "auth", "missing"):
        return False
    # "network" -> transitoire ; "other" (non classe) -> transitoire mais borne.
    return True


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
    if (
        "connection" in low
        or "timeout" in low
        or "timed out" in low
        or "network" in low
        or "resolve" in low
        or "read operation" in low
    ):
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


_VOCAB_HEADER = (
    "# Dictionnaire de vocabulaire specifique — MistralSpeechToText\n"
    "#\n"
    "# UN SEUL MOT PAR LIGNE, sans espace ni virgule. Ces termes sont passes a\n"
    "# l'API (context_bias) pour BIAISER la transcription vers ton vocabulaire\n"
    "# — sans requete ni credit en plus, et sans jamais resumer ton texte.\n"
    "#\n"
    "# IMPORTANT : l'API n'accepte qu'un seul token par entree. Une ligne\n"
    "# contenant un espace (ex. « Mistral AI ») est IGNOREE — decoupe-la en\n"
    "# mots distincts (une ligne chacun). Les lignes vides et celles commencant\n"
    "# par '#' sont ignorees.\n"
    "#\n"
    "# Exemples :\n"
    "#   Voxtral\n"
    "#   Mistral\n"
    "#   Kubernetes\n"
    "#   kubectl\n"
)

# Cache du vocabulaire, invalide au mtime du fichier (edite a la main).
# (mtime, termes valides, lignes ignorees).
_vocab_cache: tuple[float, list[str], list[str]] | None = None


def _valid_bias_term(line: str) -> str | None:
    """Normalise une ligne en token context_bias valide, ou None si inexploitable.

    L'API exige UN SEUL token : pas d'espace, pas de virgule. On retire les
    virgules de bord par securite ; une ligne a espace ou a virgule interne est
    rejetee (None) — on ne devine pas comment la decouper (cela polluerait le
    biais avec des mots courants)."""
    term = line.strip().strip(",").strip()
    if not term:
        return None
    if any(ch.isspace() for ch in term) or "," in term:
        return None
    return term


def ensure_vocab_file() -> str:
    """Cree le fichier vocabulaire (avec en-tete d'aide) s'il n'existe pas.

    Renvoie son chemin. Ne l'ecrase jamais s'il existe deja.
    """
    path = config.VOCAB_FILE
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_VOCAB_HEADER)
    return path


def load_context_bias() -> list[str]:
    """Charge le vocabulaire assaini pour context_bias, avec cache mtime.

    Ne renvoie QUE des tokens valides (un mot, sans espace ni virgule) : une
    entree « sale » (multi-mots) est ignoree plutot que de faire echouer TOUT
    l'appel avec un 400. Renvoie [] si le fichier est absent/vide ; robuste :
    toute erreur -> []. Les lignes ignorees sont consultables via
    ignored_bias_terms().
    """
    global _vocab_cache
    path = config.VOCAB_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _vocab_cache = None
        return []
    if _vocab_cache is not None and _vocab_cache[0] == mtime:
        return _vocab_cache[1]
    terms: list[str] = []
    ignored: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                term = _valid_bias_term(line)
                if term is None:
                    ignored.append(line)
                else:
                    terms.append(term)
    except OSError:
        return []
    _vocab_cache = (mtime, terms, ignored)
    return terms


def ignored_bias_terms() -> list[str]:
    """Lignes de vocabulaire ignorees (espace/virgule -> invalides pour l'API).

    Recharge le cache si besoin. Sert a avertir l'utilisateur (UX)."""
    load_context_bias()
    return list(_vocab_cache[2]) if _vocab_cache is not None else []


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
        # Biais de vocabulaire : integre a l'appel de transcription, donc aucune
        # requete/credit supplementaire et aucun risque de resume.
        terms = load_context_bias()
        if terms:
            kwargs["context_bias"] = terms
        resp = client.audio.transcriptions.complete(**kwargs)
    return (resp.text or "").strip()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python transcribe.py <fichier.wav>")
        raise SystemExit(1)
    print(transcribe(sys.argv[1]))
