"""Visual recording indicator: a small floating dot, one PER screen.

On EACH monitor, a borderless, non-activating NSPanel placed at the bottom
center (or, by default, following the cursor — see config.INDICATOR_FOLLOW_CURSOR):
    red   = recording in progress
    pink-red (pulsing) = long take (reminder to wrap up; recording continues)
    amber = transcription in progress (network call)
    blue  = transcription waiting for the network (retried in the background)
    green (flash) = deferred transcription recovered (on the clipboard)
    bright orange (flash) = DEFINITIVE failure (permanent error: job given up)
    hidden = idle
On cancel, the dot turns red then fades out gently before disappearing (a visual
"dropped" confirmation); the green (recovery) and orange (definitive failure)
flashes follow the same animation.

Why one dot per screen + a front re-assertion on every tick? So it is ALWAYS
visible on the active window:
  - a single window cannot appear on two monitors at once;
  - an app that goes full-screen (green button) creates its own Space and can
    bury the dot: so we bring it back to the front on every iteration
    (orderFrontRegardless is a visual no-op if it is already there).

THREADING CONSTRAINT: AppKit is NOT thread-safe. ALL methods of this class must
be called from the MAIN THREAD only (the CFRunLoopRunInMode loop in the CLI, the
NSTimer in .app mode).
"""

import math
import time

from AppKit import (
    NSAnimationContext,
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSMakePoint,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSScreenSaverWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Quartz import CGColorCreateGenericRGB

import config

_SIZE = 16.0          # dot diameter in points
_MARGIN_BOTTOM = 8.0   # margin ABOVE the bottom of the visible area (Dock)
_FADE_SECONDS = 0.45   # fade-out duration on cancel
_PULSE_MIN_ALPHA = 0.35  # low opacity of the "long take" pulse
_PULSE_HZ = 1.1          # pulse frequency (cycles per second)


# CGColor created directly via CoreGraphics. NB: going through NSColor.CGColor()
# fails on some PyObjC versions (the bridge returns an untyped pointer), the
# layer stays transparent and the dot is invisible.
_RED = CGColorCreateGenericRGB(0.90, 0.16, 0.16, 1.0)    # recording
_AMBER = CGColorCreateGenericRGB(1.00, 0.65, 0.05, 1.0)  # transcription in progress
_BLUE = CGColorCreateGenericRGB(0.15, 0.50, 0.95, 1.0)   # retry pending (network)
_GREEN = CGColorCreateGenericRGB(0.20, 0.75, 0.35, 1.0)  # transcription recovered (flash)
# Bright orange reserved for the DEFINITIVE failure (flash), distinct from the
# "in progress" amber and the "recording" red: not confused with a mere wait.
_ERROR = CGColorCreateGenericRGB(1.00, 0.35, 0.00, 1.0)  # permanent failure (flash)
# Bright pink-red reserved for the "long take" reminder (a take that runs on):
# distinct from the recording red, the transcription amber and the failure orange.
_WARN = CGColorCreateGenericRGB(1.00, 0.20, 0.35, 1.0)   # long recording (pulse)


def _rect_for(visible_frame):
    """Target position: bottom center of a screen's visible area."""
    x = visible_frame.origin.x + (visible_frame.size.width - _SIZE) / 2.0
    y = visible_frame.origin.y + _MARGIN_BOTTOM
    return NSMakeRect(x, y, _SIZE, _SIZE)


class Indicator:
    """Floating dots (one per screen) driven from the main thread."""

    def __init__(self) -> None:
        self._panels = []        # list of (panel, view), one entry per screen
        self._screens_sig = None
        self._visible = False
        self._pulsing = False    # opacity pulse (the "recording_long" state)
        self._color = _RED
        self._build_panels()

    # --- Construction / screen tracking ---
    @staticmethod
    def _screens_signature():
        """Fingerprint of the screen config: we rebuild if it changes."""
        sig = []
        for s in NSScreen.screens():
            f = s.frame()
            sig.append((f.origin.x, f.origin.y, f.size.width, f.size.height))
        return tuple(sig)

    def _make_panel(self, screen):
        rect = _rect_for(screen.visibleFrame())
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        # Very high level (not NSFloatingWindowLevel): essential to rise ABOVE
        # another application's full-screen Space. Combined with the behaviors
        # below, the dot stays visible everywhere.
        panel.setLevel_(NSScreenSaverWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _SIZE, _SIZE))
        view.setWantsLayer_(True)
        view.layer().setCornerRadius_(_SIZE / 2.0)
        view.layer().setBackgroundColor_(self._color)
        panel.setContentView_(view)
        return panel, view

    def _build_panels(self) -> None:
        for panel, _ in self._panels:
            panel.orderOut_(None)
        self._panels = [self._make_panel(s) for s in NSScreen.screens()]
        self._screens_sig = self._screens_signature()

    def _ensure_panels(self) -> None:
        """Rebuild the dots if a screen was plugged/unplugged/moved."""
        if self._screens_signature() != self._screens_sig:
            self._build_panels()
            if self._visible:
                self._show()

    # --- Rendering ---
    def _set_color(self, cgcolor) -> None:
        self._color = cgcolor
        for _, view in self._panels:
            view.layer().setBackgroundColor_(cgcolor)

    def _apply_cursor_follow(self) -> None:
        """Stick the dot near the mouse cursor (if following is enabled).

        NSEvent.mouseLocation() is in GLOBAL screen coords (bottom-left origin),
        the same as setFrameOrigin_. We reposition ALL panels to that point: they
        stack where the mouse is, so a single dot stays visible, on the right
        screen, without us having to pick the screen ourselves."""
        if not config.INDICATOR_FOLLOW_CURSOR:
            return
        loc = NSEvent.mouseLocation()
        dx, dy = config.INDICATOR_CURSOR_OFFSET
        origin = NSMakePoint(loc.x + dx, loc.y + dy)
        for panel, _ in self._panels:
            panel.setFrameOrigin_(origin)

    def _show(self) -> None:
        self._apply_cursor_follow()
        for panel, _ in self._panels:
            panel.setAlphaValue_(1.0)
            panel.orderFrontRegardless()
        self._visible = True

    def _hide(self) -> None:
        for panel, _ in self._panels:
            panel.orderOut_(None)
            panel.setAlphaValue_(1.0)
        self._visible = False

    def _flash_out(self, color=_RED) -> None:
        """Bright color then fade to disappearance (a brief confirmation).

        Red by default (cancel); green for a recovered transcription."""
        self._set_color(color)
        self._show()

        def _done() -> None:
            self._hide()

        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE_SECONDS)
        NSAnimationContext.currentContext().setCompletionHandler_(_done)
        for panel, _ in self._panels:
            panel.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()

    def render(self, state: str) -> None:
        """Apply the UI state. Call only on a state change."""
        self._ensure_panels()
        self._pulsing = (state == "recording_long")
        if state == "recording":
            self._set_color(_RED)
            self._show()
        elif state == "recording_long":
            # "Long take" reminder: bright color + pulse (tick), without cutting off.
            self._set_color(_WARN)
            self._show()
        elif state == "transcribing":
            self._set_color(_AMBER)
            self._show()
        elif state == "retrying":
            # Blue: transcription waiting for the network, retried in the background.
            self._set_color(_BLUE)
            self._show()
        elif state == "recovered":
            # Green flash: a deferred transcription was recovered (clipboard).
            self._flash_out(_GREEN)
        elif state == "error":
            # Bright orange flash: DEFINITIVE failure (permanent error, job given up).
            self._flash_out(_ERROR)
        elif state == "cancelled":
            self._flash_out(_RED)
        else:  # "idle" or unknown
            self._hide()

    def tick(self) -> None:
        """Call on EVERY iteration of the main loop.

        WHEN IDLE (dot hidden, ~99% of the time): return immediately. This AVOIDS
        enumerating the screens (NSScreen.screens()) on every tick, which used to
        be a needless permanent CPU cost.

        VISIBLE (recording/transcription): bring the dot back to the front (it
        reappears after an app went full-screen) and detect a screen-config
        change. A screen change that happened WHILE idle is caught anyway by
        render(), which calls _ensure_panels() before each display.
        """
        if not self._visible:
            return
        self._ensure_panels()
        self._apply_cursor_follow()
        # Opacity pulse for the "long take" state (a gentle reminder).
        alpha = 1.0
        if self._pulsing:
            phase = math.sin(time.monotonic() * _PULSE_HZ * 2.0 * math.pi)
            alpha = _PULSE_MIN_ALPHA + (1.0 - _PULSE_MIN_ALPHA) * (0.5 + 0.5 * phase)
        for panel, _ in self._panels:
            if self._pulsing:
                panel.setAlphaValue_(alpha)
            panel.orderFrontRegardless()
