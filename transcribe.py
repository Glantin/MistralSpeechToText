"""Transcription via the Mistral API (Voxtral Mini Transcribe).

Endpoint: POST https://api.mistral.ai/v1/audio/transcriptions
We do NOT pass a language: auto-detect mixed FR/EN ("franglais").
"""

import os
import ssl
import sys

import httpx
from dotenv import load_dotenv
from mistralai.client import Mistral

import config
import credentials

# In dev mode, load a project-folder .env into the environment; the .app has no
# such file, but credentials.get_api_key() will then read the Application Support
# file. load_dotenv() is a no-op (and error-free) when there is no .env.
load_dotenv()

_client: Mistral | None = None


def _cato_bundle_path() -> str | None:
    """Path to the CA bundle shipped with the project / embedded in the .app.

    PyInstaller exposes resources via sys._MEIPASS; in dev we start from the
    module's folder.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "certs", "cato-bundle.pem")
    return p if os.path.exists(p) else None


def _ssl_context() -> ssl.SSLContext:
    """SSL context tolerant of corporate TLS proxies.

    Resolution order:
      1. SSL_CERT_FILE: explicit override (user/IT) -> we honor it;
      2. default: the macOS keychain via truststore. The proxy root certificate
         (Cato/Zscaler...) installed by IT is already there, so transcription
         works with no fiddling. This is the robust path (never expires);
      3. fallback: if truststore fails, fall back to the bundled
         certs/cato-bundle.pem if present, otherwise certifi (default).
    """
    env = os.environ.get("SSL_CERT_FILE")
    if env and os.path.exists(env):
        return ssl.create_default_context(cafile=env)
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 -- truststore unavailable/incompatible
        bundle = _cato_bundle_path()
        return ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()


def _make_client(api_key: str) -> Mistral:
    """Mistral client with an HTTP transport that validates via the macOS keychain.

    We set an explicit TIMEOUT: without it, a network change/loss leaves the
    request hanging indefinitely (the transcription thread blocks, the dot
    freezes). Short CONNECT -> a dead network fails fast and switches to retry;
    generous READ -> covers the upload + transcription of a long take.
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
                "MISTRAL_API_KEY missing. Enter your key in the app "
                "(menu > Enter API key), or via a .env in dev mode."
            )
        _client = _make_client(api_key)
    return _client


def reset_client() -> None:
    """Force the client to be recreated (after changing the key in the app)."""
    global _client
    _client = None


def http_status(exc: Exception) -> int | None:
    """The HTTP code carried by the SDK/HTTP exception, or None.

    mistralai errors inherit from MistralError which exposes `status_code` (int).
    We also try a few aliases just in case, but never guess from the text (a
    "400" in an error message is not necessarily a status code).
    """
    for attr in ("status_code", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val <= 599:
            return val
    return None


def is_retriable(exc: Exception) -> bool:
    """True if the error is TRANSIENT (retry), False if PERMANENT.

    Queue retry policy (transcribe_queue):
      - Transient -> retry with back-off: timeouts, network drops, 5xx, 429, 408.
      - Permanent -> give up: 400 (invalid args, e.g. context_bias), 401/403
        (auth), 404, 422 (validation)... and a corporate proxy SSL failure
        (a config to fix; retrying forever would just stay blue).
    Without a usable HTTP code, we rely on the nature of the error: network =
    transient; everything else (unknown) is treated as transient but capped by
    the attempt count / max age on the queue side.
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
    # "network" -> transient; "other" (unclassified) -> transient but bounded.
    return True


def classify_error(exc: Exception) -> tuple[str, str]:
    """Classify a network/API exception and return (kind, actionable message).

    We test SSL/certificate FIRST: behind a corporate proxy the failure is a
    CERTIFICATE_VERIFY_FAILED, NOT a key problem. Reversing it would wrongly say
    "key rejected". kind in {"ssl","auth","missing","network","other"}.
    """
    low = str(exc).lower()
    if "certificate" in low or "ssl" in low or "self-signed" in low or "self signed" in low:
        return "ssl", (
            "Corporate TLS proxy: the certificate is not trusted. "
            "(This is not your key.) See the proxy section of the README."
        )
    # Auth BEFORE "missing": "invalid api key" contains "api key" and would
    # otherwise be misclassified as a missing key.
    if "401" in low or "unauthorized" in low or ("invalid" in low and "key" in low):
        return "auth", "Key rejected (401). Check your Mistral key."
    if "missing" in low or "api_key" in low:
        return "missing", "API key missing. Enter it from the 🎙 menu."
    if (
        "connection" in low
        or "timeout" in low
        or "timed out" in low
        or "network" in low
        or "resolve" in low
        or "read operation" in low
    ):
        return "network", "No network or API unreachable. Try again."
    return "other", f"Transcription failed: {str(exc)[:140]}"


def test_api_key() -> tuple[bool, str]:
    """Check the current key with a lightweight call (list models).

    Returns (ok, message). Creates a throwaway client (same TLS transport as the
    real client) to test EXACTLY the stored key, without touching the cached
    client. An SSL failure is distinguished from a rejected key.
    """
    key = credentials.get_api_key()
    if not key:
        return False, "No key entered."
    try:
        _make_client(key).models.list()
        return True, "Key valid ✅"
    except Exception as exc:  # noqa: BLE001
        return False, classify_error(exc)[1]


_VOCAB_HEADER = (
    "# Custom vocabulary dictionary — MistralSpeechToText\n"
    "#\n"
    "# ONE WORD PER LINE, no space or comma. These terms are passed to the API\n"
    "# (context_bias) to BIAS the transcription toward your vocabulary — with no\n"
    "# extra request or credit, and without ever summarizing your text.\n"
    "#\n"
    "# IMPORTANT: the API only accepts a single token per entry. A line that\n"
    "# contains a space (e.g. « Mistral AI ») is IGNORED — split it into\n"
    "# separate words (one per line). Empty lines and lines starting with '#' are\n"
    "# ignored.\n"
    "#\n"
    "# Examples:\n"
    "#   Voxtral\n"
    "#   Mistral\n"
    "#   Kubernetes\n"
    "#   kubectl\n"
)

# Vocabulary cache, invalidated on the file's mtime (edited by hand).
# (mtime, valid terms, ignored lines).
_vocab_cache: tuple[float, list[str], list[str]] | None = None


def _valid_bias_term(line: str) -> str | None:
    """Normalize a line into a valid context_bias token, or None if unusable.

    The API requires a SINGLE token: no space, no comma. We strip edge commas as
    a safety measure; a line with an inner space or comma is rejected (None) — we
    do not guess how to split it (that would pollute the bias with common words).
    """
    term = line.strip().strip(",").strip()
    if not term:
        return None
    if any(ch.isspace() for ch in term) or "," in term:
        return None
    return term


def ensure_vocab_file() -> str:
    """Create the vocabulary file (with a help header) if it does not exist.

    Returns its path. Never overwrites it if it already exists.
    """
    path = config.VOCAB_FILE
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_VOCAB_HEADER)
    return path


def load_context_bias() -> list[str]:
    """Load the sanitized vocabulary for context_bias, with an mtime cache.

    Returns ONLY valid tokens (one word, no space or comma): a "dirty" entry
    (multi-word) is ignored rather than failing the WHOLE call with a 400.
    Returns [] if the file is absent/empty; robust: any error -> []. Ignored
    lines can be inspected via ignored_bias_terms().
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
    """Vocabulary lines that were ignored (space/comma -> invalid for the API).

    Reloads the cache if needed. Used to warn the user (UX)."""
    load_context_bias()
    return list(_vocab_cache[2]) if _vocab_cache is not None else []


def transcribe(wav_path: str) -> str:
    """Transcribe a WAV file and return the (stripped) text."""
    client = _get_client()
    with open(wav_path, "rb") as f:
        kwargs = {
            "model": config.MISTRAL_MODEL,
            "file": {"content": f, "file_name": os.path.basename(wav_path)},
        }
        if config.MISTRAL_LANGUAGE:
            kwargs["language"] = config.MISTRAL_LANGUAGE
        # Vocabulary bias: folded into the transcription call, so no extra
        # request/credit and no risk of summarization.
        terms = load_context_bias()
        if terms:
            kwargs["context_bias"] = terms
        resp = client.audio.transcriptions.complete(**kwargs)
    return (resp.text or "").strip()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python transcribe.py <file.wav>")
        raise SystemExit(1)
    print(transcribe(sys.argv[1]))
