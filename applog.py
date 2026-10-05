"""Small rotating log file in Application Support (diagnostics only).

Why? The app had NO log: a frozen mic stream or a dead worker thread left no
trace, and diagnosing it required sampling the live process. This file records
lifecycle events (take start/stop, mic stream abandoned, transcription attempts,
give-ups) so the next incident can be explained after the fact.

NEVER log the dictated text or the API key.
"""

import logging
import logging.handlers
import os
import threading

import credentials

LOG_PATH = os.path.join(credentials.APP_SUPPORT_DIR, "mistral-stt.log")
_MAX_BYTES = 500_000
_BACKUPS = 2

_logger = logging.getLogger("mistral_stt")
_lock = threading.Lock()
_configured = False


def _ensure() -> None:
    """Attach the file handler once (lazy: tests never touch the real file
    unless they log)."""
    global _configured
    with _lock:
        if _configured:
            return
        _configured = True
        _logger.setLevel(logging.INFO)
        _logger.propagate = False
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(threadName)s] %(message)s")
            )
            _logger.addHandler(handler)
        except OSError:
            # Unwritable folder: stay silent rather than break the app.
            _logger.addHandler(logging.NullHandler())


def log(msg: str) -> None:
    """Append one line to the log file. Never raises."""
    try:
        _ensure()
        _logger.info(msg)
    except Exception:  # noqa: BLE001
        pass
