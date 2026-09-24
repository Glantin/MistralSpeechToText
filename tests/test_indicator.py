"""Tests for the floating dot's window setup (needs macOS AppKit, no display
interaction: panels are built but never shown to the user for long)."""

from AppKit import NSScreenSaverWindowLevel
from Foundation import NSDate, NSRunLoop

import indicator


def test_panels_sit_above_full_screen_apps():
    # setFloatingPanel_ used to reset the level to 3 after setLevel_: the dot
    # was then buried under full-screen apps and other Spaces.
    ind = indicator.Indicator()
    assert ind._panels
    for panel, _ in ind._panels:
        assert panel.level() == NSScreenSaverWindowLevel


def _run_loop(seconds):
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(seconds)
    )


def test_stale_flash_does_not_hide_a_new_recording():
    # Esc (cancel flash) then a new take within the fade: the fade's completion
    # used to hide the red dot for the whole take.
    ind = indicator.Indicator()
    ind.render("cancelled")
    _run_loop(0.1)
    ind.render("recording")
    _run_loop(0.8)  # let the cancel fade finish
    assert ind._visible is True
    for panel, _ in ind._panels:
        assert panel.isVisible()
        assert panel.alphaValue() == 1.0
    ind.render("idle")
    assert ind._visible is False


def test_flash_alone_ends_hidden():
    ind = indicator.Indicator()
    ind.render("recovered")
    _run_loop(0.8)
    assert ind._visible is False
