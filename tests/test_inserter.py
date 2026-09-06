"""Unit tests for the paste-hardening in inserter.insert_at_cursor.

We never send a real Cmd+V here: the Quartz calls and the clipboard are stubbed,
so we assert the DECISION (paste fired or not) and the safety net (text always on
the clipboard). Two guards must hold before pasting:
  - Accessibility granted (otherwise macOS drops the event silently);
  - no modifier held (otherwise the paste is swallowed -> empty paste).
"""

import inserter


class _Recorder:
    """Captures set_clipboard calls and counts Cmd+V sends."""

    def __init__(self):
        self.clipboard = None
        self.sends = 0


def _patch(monkeypatch, rec, *, accessibility, held_flags):
    monkeypatch.setattr(inserter, "set_clipboard", lambda t: setattr(rec, "clipboard", t))
    monkeypatch.setattr(inserter, "_get_clipboard", lambda: None)
    monkeypatch.setattr(inserter, "has_accessibility", lambda: accessibility)
    # Modifier state: a constant flags value the guards read.
    monkeypatch.setattr(
        inserter, "CGEventSourceFlagsState", lambda _state: held_flags, raising=False
    )
    monkeypatch.setattr(inserter, "_send_cmd_v", lambda: setattr(rec, "sends", rec.sends + 1))
    # No real sleeping in tests. The real NSPasteboard.changeCount() read is
    # harmless (read-only); set_clipboard/_get_clipboard are stubbed above so the
    # real clipboard is never touched.
    monkeypatch.setattr(inserter.time, "sleep", lambda _s: None)


def test_paste_fires_when_clear(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, accessibility=True, held_flags=0)
    assert inserter.insert_at_cursor("hello", restore=False) is True
    assert rec.sends == 1
    assert rec.clipboard == "hello"  # safety net, always


def test_no_paste_without_accessibility(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, accessibility=False, held_flags=0)
    assert inserter.insert_at_cursor("hello", restore=False) is False
    assert rec.sends == 0  # Cmd+V NOT fired (would be dropped silently)
    assert rec.clipboard == "hello"  # text preserved for a manual Cmd+V


def test_no_paste_while_modifier_held(monkeypatch):
    rec = _Recorder()
    option_mask = inserter._MOD_MASKS["option"]
    # Modifier stays held for the whole (zeroed) wait -> timeout -> no paste.
    _patch(monkeypatch, rec, accessibility=True, held_flags=option_mask)
    monkeypatch.setattr(inserter.config, "PASTE_MODIFIER_WAIT", 0.0)
    assert inserter.insert_at_cursor("hello", restore=False) is False
    assert rec.sends == 0
    assert rec.clipboard == "hello"


def test_empty_text_is_noop(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, accessibility=True, held_flags=0)
    assert inserter.insert_at_cursor("", restore=False) is False
    assert rec.sends == 0
