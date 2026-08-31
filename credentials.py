"""Store the API key outside the project folder.

A packaged .app has no `.env` next to the code, so the key is stored in
`~/Library/Application Support/MistralSTT/.env`, written/read by the onboarding.

Key resolution order (see `get_api_key`):
  1. the MISTRAL_API_KEY environment variable (handy in dev / via launchd);
  2. the Application Support file (the normal case for the .app).

The format stays `MISTRAL_API_KEY=...` to remain compatible with a classic
`.env` and the `python-dotenv` already used in dev mode.
"""

import os

APP_SUPPORT_DIR = os.path.expanduser(
    "~/Library/Application Support/MistralSTT"
)
KEY_FILE = os.path.join(APP_SUPPORT_DIR, ".env")
_KEY_NAME = "MISTRAL_API_KEY"


def _read_key_file() -> str | None:
    """Return the key stored in the Application Support file, or None."""
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == _KEY_NAME:
                    return value.strip().strip("'\"") or None
    except OSError:
        return None
    return None


def get_api_key() -> str | None:
    """Effective API key: env first (dev/launchd), then the app file."""
    env = os.environ.get(_KEY_NAME)
    if env:
        return env.strip()
    return _read_key_file()


def set_api_key(key: str) -> None:
    """Write the key into the Application Support file (creates the folder).

    The file is restricted to the user (chmod 600): it holds a secret.
    """
    key = (key or "").strip()
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(f"{_KEY_NAME}={key}\n")
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass


def has_api_key() -> bool:
    """True if a non-empty key is available (env or file)."""
    return bool(get_api_key())
