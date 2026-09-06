"""Insert text at the cursor via clipboard + a synthetic Cmd+V.

We go through the clipboard rather than typing character by character: it is
instant and handles accents / mixed-language text cleanly. The previous
clipboard contents are saved and then restored.
"""

import os
import time

from AppKit import NSPasteboard, NSStringPboardType
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventSourceFlagsState,
    kCGEventFlagMaskCommand,
    kCGEventSourceStateCombinedSessionState,
    kCGHIDEventTap,
)

import config

_V_KEYCODE = 9  # the "v" key

# Debug logging, gated by the same env var as mistral_stt (silent otherwise).
_DEBUG = bool(os.environ.get("MISTRAL_STT_DEBUG"))


def _log(msg: str) -> None:
    if _DEBUG:
        print(f"[inserter:debug] {msg}")


# Modifier-flag masks used to describe the ambient keyboard state at paste time.
# A paste fired while (say) Option is physically held can be swallowed by the
# target app -> empty paste. insert_at_cursor() waits for these to be released
# before firing Cmd+V (see _wait_modifiers_released); we also LOG them here.
_MOD_MASKS = {
    "cmd": 0x00100000,    # kCGEventFlagMaskCommand
    "shift": 0x00020000,  # kCGEventFlagMaskShift
    "ctrl": 0x00040000,   # kCGEventFlagMaskControl
    "option": 0x00080000,  # kCGEventFlagMaskAlternate
}


def _ambient_modifiers() -> str:
    """Human-readable list of modifiers currently held on the real keyboard."""
    try:
        flags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
    except Exception as exc:  # noqa: BLE001
        return f"<unavailable: {exc}>"
    held = [name for name, mask in _MOD_MASKS.items() if flags & mask]
    return "+".join(held) if held else "none"


def _modifiers_held() -> bool:
    """True if ANY paste-breaking modifier is currently held on the keyboard.

    A Cmd+V fired while (say) Option is held becomes Cmd+Option+V and the target
    app can swallow it (empty paste). We treat that as unsafe. On any failure to
    read the state, assume nothing is held (do not block the paste needlessly).
    """
    try:
        flags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
    except Exception:  # noqa: BLE001
        return False
    return any(flags & mask for mask in _MOD_MASKS.values())


def _wait_modifiers_released(timeout: float) -> bool:
    """Poll until no modifier is held, or `timeout` seconds elapse.

    Returns True if the keyboard is clear (safe to paste), False on timeout.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while _modifiers_held():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def has_accessibility() -> bool:
    """Accessibility permission granted? (required to POST a synthetic Cmd+V).

    Without it macOS DROPS the paste event silently. We check with prompt=False
    (never trigger the system dialog from here). Returns True if the API is
    unavailable (e.g. off-macOS test host): the paste path then behaves as before.
    """
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}))
    except Exception:  # noqa: BLE001
        return True


def set_clipboard(text: str) -> None:
    """Put `text` on the general pasteboard (reusable public API)."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSStringPboardType)


# Historical internal alias.
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


def insert_at_cursor(text: str, restore: bool = True) -> bool:
    """Paste `text` wherever the cursor is. Return True if the paste was fired.

    The text is ALWAYS put on the clipboard first (safety net), before any check:
    even when we decline to paste, the dictation is never lost. We return False
    (WITHOUT sending Cmd+V) when the paste would be unsafe or futile, so the
    caller can fall back to a visible clipboard+notification delivery:
      - Accessibility permission missing -> macOS drops the event silently;
      - a modifier is still held after PASTE_MODIFIER_WAIT -> the paste would be
        swallowed (empty paste).

    If `restore` is true, the previous clipboard contents are restored after
    pasting. If false, `text` is left on the clipboard as a safety net (see
    config.KEEP_LAST_IN_CLIPBOARD).
    """
    if not text:
        return False
    pb = NSPasteboard.generalPasteboard()
    before = pb.changeCount()
    previous = _get_clipboard()
    set_clipboard(text)  # safety net FIRST: the text survives whatever follows.
    after = pb.changeCount()
    _log(
        f"clipboard set ({len(text)} chars), changeCount {before} -> {after}, "
        f"ambient modifiers: {_ambient_modifiers()}"
    )

    # Accessibility is required to POST the Cmd+V; without it the event is
    # dropped silently. Do not paste: let the caller surface it and keep the text
    # on the clipboard for a manual Cmd+V.
    if not has_accessibility():
        _log("accessibility NOT granted -> skip Cmd+V (text kept on clipboard)")
        return False

    # Wait for a clean keyboard: a modifier held at paste time swallows Cmd+V.
    if not _wait_modifiers_released(config.PASTE_MODIFIER_WAIT):
        _log(
            "modifiers still held after wait "
            f"({_ambient_modifiers()}) -> skip Cmd+V (text kept on clipboard)"
        )
        return False

    # Small delay to let the clipboard propagate before pasting.
    time.sleep(0.05)
    _log(f"sending Cmd+V (ambient modifiers: {_ambient_modifiers()})")
    _send_cmd_v()
    # Let the target app consume the paste before restoring.
    time.sleep(0.15)
    if restore and previous is not None:
        set_clipboard(previous)
    return True


if __name__ == "__main__":
    print("Inserting in 2 s: place your cursor in a text field...")
    time.sleep(2)
    insert_at_cursor("mistral-stt test: this is a quick check")
