"""Insertion de texte au curseur via presse-papier + Cmd+V synthetique.

On passe par le presse-papier plutot que par une frappe caractere-par-caractere :
c'est instantane et ca gere proprement les accents / le franglais. Le contenu
precedent du presse-papier est sauvegarde puis restaure.
"""

import time

from AppKit import NSPasteboard, NSStringPboardType
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)

_V_KEYCODE = 9  # touche "v"


def set_clipboard(text: str) -> None:
    """Place `text` dans le presse-papier general (API publique reutilisable)."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSStringPboardType)


# Alias interne historique.
_set_clipboard = set_clipboard


def _get_clipboard() -> str | None:
    pb = NSPasteboard.generalPasteboard()
    return pb.stringForType_(NSStringPboardType)


def _send_cmd_v() -> None:
    down = CGEventCreateKeyboardEvent(None, _V_KEYCODE, True)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    up = CGEventCreateKeyboardEvent(None, _V_KEYCODE, False)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def insert_at_cursor(text: str, restore: bool = True) -> None:
    """Colle `text` la ou est le curseur.

    Si `restore` est vrai (defaut), l'ancien contenu du presse-papier est
    restaure apres le collage. Si faux, on laisse `text` dans le presse-papier
    comme filet de securite (cf. config.KEEP_LAST_IN_CLIPBOARD).
    """
    if not text:
        return
    previous = _get_clipboard()
    set_clipboard(text)
    # Petit delai pour laisser le presse-papier se propager avant le collage.
    time.sleep(0.05)
    _send_cmd_v()
    # Laisse l'app cible consommer le collage avant de restaurer.
    time.sleep(0.15)
    if restore and previous is not None:
        set_clipboard(previous)


if __name__ == "__main__":
    print("Insertion dans 2 s : place ton curseur dans un champ texte...")
    time.sleep(2)
    insert_at_cursor("mistral-stt test : c'est un quick check")
