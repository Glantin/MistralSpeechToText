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
from Quartz import CGColorCreateGenericRGB

_SIZE = 16.0          # diametre de la pastille en points
_MARGIN_BOTTOM = 8.0   # marge AU-DESSUS du Dock
_FADE_SECONDS = 0.45   # duree de l'estompage a l'annulation


# CGColor cree directement via CoreGraphics. NB: passer par NSColor.CGColor()
# echoue avec certaines versions de PyObjC (le pont renvoie un pointeur non
# type), le calque reste transparent et la pastille est invisible.
_RED = CGColorCreateGenericRGB(0.90, 0.16, 0.16, 1.0)    # enregistrement
_AMBER = CGColorCreateGenericRGB(1.00, 0.65, 0.05, 1.0)  # transcription


def _dock_visible_frame():
    """visibleFrame de l'ecran qui porte le Dock (en bas de l'ecran).

    NSScreen.mainScreen() suit le focus clavier : en multi-moniteur la pastille
    finit sur le mauvais ecran. On cible donc explicitement l'ecran dont le Dock
    occupe le bas (visibleFrame remonte par rapport au frame). A defaut, on
    retombe sur l'ecran principal.
    """
    for s in NSScreen.screens():
        f = s.frame()
        vf = s.visibleFrame()
        if vf.origin.y - f.origin.y > 1:   # Dock en bas -> zone visible remontee
            return vf
    return NSScreen.screens()[0].visibleFrame()


class Indicator:
    """Pastille flottante pilotee depuis le main thread."""

    def __init__(self) -> None:
        rect = self._target_rect()

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

    def _target_rect(self):
        """Position cible : bas-centre, juste au-dessus du Dock."""
        vf = _dock_visible_frame()
        x = vf.origin.x + (vf.size.width - _SIZE) / 2.0
        y = vf.origin.y + _MARGIN_BOTTOM
        return NSMakeRect(x, y, _SIZE, _SIZE)

    def _set_color(self, cgcolor) -> None:
        self._view.layer().setBackgroundColor_(cgcolor)

    def _show(self) -> None:
        # On recalcule la position a chaque affichage : robuste si la config
        # ecran a change (ecran branche/debranche, Dock deplace).
        self._panel.setFrameOrigin_(self._target_rect().origin)
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
