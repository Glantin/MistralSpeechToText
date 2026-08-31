"""User-adjustable settings, persisted outside the project folder.

Currently holds a single knob: the recording limit (minutes). It is stored as
JSON in `~/Library/Application Support/MistralSTT/settings.json`, next to the API
key, so it survives across launches and lives outside the (read-only) packaged
.app bundle.

Why a setting and not a constant? The recording limit is the ONLY place a long
take is cut. Its default is kept low for RAM, but a user who dictates for a long
time can raise it — up to the Voxtral API ceiling — from the menu, without
editing code.
"""

import json
import os

import config
import credentials

_SETTINGS_FILE = os.path.join(credentials.APP_SUPPORT_DIR, "settings.json")
_KEY_MAX_MINUTES = "max_record_minutes"


def _read() -> dict:
    """Return the settings dict, or {} on any problem (best-effort)."""
    try:
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    os.makedirs(credentials.APP_SUPPORT_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _clamp_minutes(minutes: int) -> int:
    """Keep the limit within [1, ceiling]; the ceiling is the API's hard max."""
    return max(1, min(int(minutes), config.MAX_RECORD_CEILING_MINUTES))


def get_max_record_minutes() -> int:
    """Effective recording limit in minutes (default if unset/invalid), clamped."""
    raw = _read().get(_KEY_MAX_MINUTES, config.MAX_RECORD_DEFAULT_MINUTES)
    try:
        return _clamp_minutes(raw)
    except (TypeError, ValueError):
        return config.MAX_RECORD_DEFAULT_MINUTES


def get_max_record_seconds() -> int:
    """Effective recording limit in seconds (see get_max_record_minutes)."""
    return get_max_record_minutes() * 60


def set_max_record_minutes(minutes: int) -> int:
    """Persist the recording limit (clamped to [1, ceiling]). Returns the value stored."""
    value = _clamp_minutes(minutes)
    data = _read()
    data[_KEY_MAX_MINUTES] = value
    _write(data)
    return value
