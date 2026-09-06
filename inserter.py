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

_V_KEYCODE = 9  # the "v" key

# Debug logging, gated by the same env var as mistral_stt (silent otherwise).
_DEBUG = bool(os.environ.get("MISTRAL_STT_DEBUG"))


def _log(msg: str) -> None:
    if _DEBUG:
        print(f"[inserter:debug] {msg}")


# Modifier-flag masks used to describe the ambient keyboard state at paste time.
# A paste fired while (say) Option is physically held can be swallowed by the
# target app -> empty paste. We only LOG this here (diagnostics); the guard that
# waits for release is added with the paste hardening.
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


def insert_at_cursor(text: str, restore: bool = True) -> None:
    """Paste `text` wherever the cursor is.

    If `restore` is true (default), the previous clipboard contents are restored
    after pasting. If false, `text` is left on the clipboard as a safety net
    (see config.KEEP_LAST_IN_CLIPBOARD).
    """
    if not text:
        return
    pb = NSPasteboard.generalPasteboard()
    before = pb.changeCount()
    previous = _get_clipboard()
    set_clipboard(text)
    after = pb.changeCount()
    _log(
        f"clipboard set ({len(text)} chars), changeCount {before} -> {after}, "
        f"ambient modifiers: {_ambient_modifiers()}"
    )
    # Small delay to let the clipboard propagate before pasting.
    time.sleep(0.05)
    _log(f"sending Cmd+V (ambient modifiers: {_ambient_modifiers()})")
    _send_cmd_v()
    # Let the target app consume the paste before restoring.
    time.sleep(0.15)
    if restore and previous is not None:
        set_clipboard(previous)


if __name__ == "__main__":
    print("Inserting in 2 s: place your cursor in a text field...")
    time.sleep(2)
    insert_at_cursor("mistral-stt test: this is a quick check")
