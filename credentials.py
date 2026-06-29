"""Stockage de la cle API hors du dossier projet.

Une .app packagee n'a pas de `.env` a cote du code : on range donc la cle dans
`~/Library/Application Support/MistralSTT/.env`, ecrit/lu par l'onboarding.

Ordre de resolution de la cle (cf. `get_api_key`) :
  1. variable d'environnement MISTRAL_API_KEY (pratique en dev / via launchd) ;
  2. fichier Application Support (le cas normal pour la .app).

Le format reste `MISTRAL_API_KEY=...` pour rester compatible avec un `.env`
classique et le `python-dotenv` deja utilise en mode dev.
"""

import os

APP_SUPPORT_DIR = os.path.expanduser(
    "~/Library/Application Support/MistralSTT"
)
KEY_FILE = os.path.join(APP_SUPPORT_DIR, ".env")
_KEY_NAME = "MISTRAL_API_KEY"


def _read_key_file() -> str | None:
    """Renvoie la cle stockee dans le fichier Application Support, ou None."""
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
    """Cle API effective : env d'abord (dev/launchd), puis fichier app."""
    env = os.environ.get(_KEY_NAME)
    if env:
        return env.strip()
    return _read_key_file()


def set_api_key(key: str) -> None:
    """Ecrit la cle dans le fichier Application Support (cree le dossier).

    Le fichier est restreint a l'utilisateur (chmod 600) : il contient un secret.
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
    """Vrai si une cle non vide est disponible (env ou fichier)."""
    return bool(get_api_key())
