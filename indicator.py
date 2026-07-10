"""Indicateur visuel d'enregistrement : une petite pastille flottante PAR ecran.

Sur CHAQUE moniteur, une fenetre NSPanel sans bord, non activante, posee en
bas-centre :
    rouge  = enregistrement en cours
    ambre  = transcription en cours (appel reseau)
    masquee = repos
A l'annulation, la pastille passe au rouge puis s'estompe doucement avant de
disparaitre (confirmation visuelle "jetee").

Pourquoi une pastille par ecran + une reaffirmation du premier plan a chaque
tick ? Pour qu'elle soit TOUJOURS visible sur la fenetre active :
  - une seule fenetre ne peut pas apparaitre sur deux moniteurs a la fois ;
  - une app passee en plein ecran (bouton vert) cree son propre Space et peut
    enterrer la pastille : on la remet donc au premier plan a chaque iteration
    (orderFrontRegardless est un no-op visuel si elle y est deja).

CONTRAINTE THREADING : AppKit n'est PAS thread-safe. TOUTES les methodes de
cette classe doivent etre appelees depuis le MAIN THREAD uniquement (la boucle
CFRunLoopRunInMode en CLI, le NSTimer en mode .app).
"""

from AppKit import (
    NSAnimationContext,
    NSBackingStoreBuffered,
    NSColor,
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

_SIZE = 16.0          # diametre de la pastille en points
_MARGIN_BOTTOM = 8.0   # marge AU-DESSUS du bas de la zone visible (Dock)
_FADE_SECONDS = 0.45   # duree de l'estompage a l'annulation


# CGColor cree directement via CoreGraphics. NB: passer par NSColor.CGColor()
# echoue avec certaines versions de PyObjC (le pont renvoie un pointeur non
# type), le calque reste transparent et la pastille est invisible.
_RED = CGColorCreateGenericRGB(0.90, 0.16, 0.16, 1.0)    # enregistrement
_AMBER = CGColorCreateGenericRGB(1.00, 0.65, 0.05, 1.0)  # transcription


def _rect_for(visible_frame):
    """Position cible : bas-centre de la zone visible d'un ecran."""
    x = visible_frame.origin.x + (visible_frame.size.width - _SIZE) / 2.0
    y = visible_frame.origin.y + _MARGIN_BOTTOM
    return NSMakeRect(x, y, _SIZE, _SIZE)


class Indicator:
    """Pastilles flottantes (une par ecran) pilotees depuis le main thread."""

    def __init__(self) -> None:
        self._panels = []        # liste de (panel, view), une entree par ecran
        self._screens_sig = None
        self._visible = False
        self._color = _RED
        self._build_panels()

    # --- Construction / suivi des ecrans ---
    @staticmethod
    def _screens_signature():
        """Empreinte de la config ecran : on reconstruit si elle change."""
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
        # Niveau tres eleve (et non NSFloatingWindowLevel) : indispensable pour
        # passer AU-DESSUS du Space plein ecran d'une AUTRE application. Combine
        # aux comportements ci-dessous, la pastille reste visible partout.
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
        """Reconstruit les pastilles si un ecran a ete branche/debranche/deplace."""
        if self._screens_signature() != self._screens_sig:
            self._build_panels()
            if self._visible:
                self._show()

    # --- Rendu ---
    def _set_color(self, cgcolor) -> None:
        self._color = cgcolor
        for _, view in self._panels:
            view.layer().setBackgroundColor_(cgcolor)

    def _show(self) -> None:
        for panel, _ in self._panels:
            panel.setAlphaValue_(1.0)
            panel.orderFrontRegardless()
        self._visible = True

    def _hide(self) -> None:
        for panel, _ in self._panels:
            panel.orderOut_(None)
            panel.setAlphaValue_(1.0)
        self._visible = False

    def _flash_out(self) -> None:
        """Rouge vif puis estompage jusqu'a disparition (confirmation annulation)."""
        self._set_color(_RED)
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
        """Applique l'etat UI. A appeler uniquement sur changement d'etat."""
        self._ensure_panels()
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

    def tick(self) -> None:
        """A appeler a CHAQUE iteration de la boucle principale.

        Tant que la pastille est visible, on la remet au premier plan : c'est ce
        qui la fait reapparaitre apres qu'une app soit passee en plein ecran
        (nouveau Space) et qu'elle ait ete enterree. Sans effet visible sinon.
        Detecte aussi un changement de config ecran.
        """
        self._ensure_panels()
        if self._visible:
            for panel, _ in self._panels:
                panel.orderFrontRegardless()
