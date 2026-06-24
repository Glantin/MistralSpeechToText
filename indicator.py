"""Indicateur visuel d'enregistrement : petite pastille flottante.

Une fenetre NSPanel sans bord, non activante, posee en bas-centre de l'ecran :
    rouge  = enregistrement en cours
    ambre  = transcription en cours (appel reseau)
    masquee = repos
A l'annulation, la pastille passe au rouge puis s'estompe doucement avant de
disparaitre (confirmation visuelle "jetee").

CONTRAINTE THREADING : AppKit n'est PAS thread-safe. TOUTES les methodes de
cette classe doivent etre appelees depuis le MAIN THREAD uniquement. Dans
mistral_stt.py c'est la boucle CFRunLoopRunInMode (main thread) qui lit l'etat
partage et appelle render().
"""

from AppKit import (
    NSAnimationContext,
    NSBackingStoreBuffered,
    NSColor,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)

_SIZE = 14.0          # diametre de la pastille en points
_MARGIN_BOTTOM = 80.0  # distance au bas de l'ecran
_FADE_SECONDS = 0.45   # duree de l'estompage a l'annulation


def _color(r: float, g: float, b: float):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


_RED = _color(0.90, 0.16, 0.16)    # enregistrement
_AMBER = _color(1.00, 0.65, 0.05)  # transcription


class Indicator:
    """Pastille flottante pilotee depuis le main thread."""

    def __init__(self) -> None:
        screen = NSScreen.mainScreen()
        frame = screen.frame()
        x = frame.origin.x + (frame.size.width - _SIZE) / 2.0
        y = frame.origin.y + _MARGIN_BOTTOM
        rect = NSMakeRect(x, y, _SIZE, _SIZE)

        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSFloatingWindowLevel)
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
        layer = view.layer()
        layer.setCornerRadius_(_SIZE / 2.0)
        panel.setContentView_(view)

        self._panel = panel
        self._view = view
        self._visible = False

    def _set_color(self, color) -> None:
        self._view.layer().setBackgroundColor_(color.CGColor())

    def _show(self) -> None:
        self._panel.setAlphaValue_(1.0)
        if not self._visible:
            self._panel.orderFrontRegardless()
            self._visible = True

    def _hide(self) -> None:
        if self._visible:
            self._panel.orderOut_(None)
            self._visible = False
        self._panel.setAlphaValue_(1.0)

    def _flash_out(self) -> None:
        """Rouge vif puis estompage jusqu'a disparition (confirmation annulation)."""
        self._set_color(_RED)
        self._show()

        def _done() -> None:
            self._hide()

        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE_SECONDS)
        NSAnimationContext.currentContext().setCompletionHandler_(_done)
        self._panel.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()

    def render(self, state: str) -> None:
        """Applique l'etat UI. A appeler uniquement sur changement d'etat."""
        if state == "recording":
            self._set_color(_RED)
            self._show()
        elif state == "transcribing":
            self._set_color(_AMBER)
            self._show()
        elif state == "cancelled":
            self._flash_out()
        else:  # "idle" ou inconnu
            self._hide()
