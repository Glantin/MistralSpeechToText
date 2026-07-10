"""MistralSpeechToText — point d'entree de l'application macOS (.app).

App en barre de menus (pas d'icone dans le Dock) : maintien **Option droite**
pour dicter, comme en mode CLI. On REUTILISE tout le coeur de `mistral_stt.py`
(event tap clavier + worker + etat partage `_ui_state`) ; seule la facon de
tourner change :

  - `NSApplication.run()` (boucle AppKit) au lieu de la boucle manuelle
    `CFRunLoopRunInMode` du mode CLI ;
  - un `NSStatusItem` (menu) : cle API, lancer au demarrage, permissions,
    quitter ;
  - un **onboarding** au 1er lancement, car une .app ne peut PAS s'auto-accorder
    Micro / Surveillance des entrees / Accessibilite : on declenche les pop-ups
    systeme et on ouvre directement le bon volet des Reglages.

THREADING : AppKit n'est pas thread-safe. Le delegue, le menu, l'onboarding et
l'indicateur vivent sur le MAIN THREAD (la boucle NSApp.run()). Le worker tourne
dans un thread daemon (importe de mistral_stt) et ne touche QUE l'etat partage.
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import objc
from Foundation import NSBundle

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSButton,
    NSButtonTypeSwitch,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSSecureTextField,
    NSStatusBar,
    NSTextField,
    NSVariableStatusItemLength,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    kAXTrustedCheckOptionPrompt,
)
from Quartz import CGPreflightListenEventAccess, CGRequestListenEventAccess

import config
import credentials
import mistral_stt as core
import transcribe

# Volets des Reglages Systeme (deep-links). Ouvrent directement le bon onglet
# Confidentialite & securite plutot que de laisser l'utilisateur chercher.
_PANE_MIC = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
_PANE_INPUT = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
_PANE_AX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


def _open_pane(url: str) -> None:
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _notify(message: str, title: str = "MistralSTT") -> None:
    """Affiche une notification macOS (via osascript : aucune permission a demander)."""
    msg = message.replace('\\', '').replace('"', "'")
    ttl = title.replace('"', "'")
    subprocess.Popen(
        ["osascript", "-e", f'display notification "{msg}" with title "{ttl}"'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --- Etat des permissions --------------------------------------------------

def has_input_monitoring() -> bool:
    """Surveillance des entrees accordee ? (necessaire pour capter Option droite)."""
    return bool(CGPreflightListenEventAccess())


def request_input_monitoring() -> None:
    """Declenche le pop-up systeme Surveillance des entrees (une seule fois)."""
    CGRequestListenEventAccess()


def has_accessibility() -> bool:
    """Accessibilite accordee ? (necessaire pour coller via Cmd+V)."""
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}))


def request_accessibility() -> None:
    """Declenche le pop-up systeme Accessibilite."""
    AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})


def trigger_microphone_prompt() -> None:
    """Ouvre brievement le micro pour declencher le pop-up d'autorisation.

    On evite une dependance AVFoundation : ouvrir un flux sounddevice suffit a
    faire apparaitre la demande systeme la premiere fois. Sans effet ensuite.
    """
    def _go() -> None:
        try:
            import sounddevice as sd

            s = sd.InputStream(
                samplerate=config.SAMPLE_RATE, channels=config.CHANNELS, dtype="int16"
            )
            s.start()
            time.sleep(0.2)
            s.stop()
            s.close()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_go, daemon=True).start()


# --- Lancer au demarrage (SMAppService) ------------------------------------

def _login_service():
    """SMAppService de l'app courante, ou None si indisponible (ex: lance hors .app)."""
    try:
        from ServiceManagement import SMAppService

        return SMAppService.mainAppService()
    except Exception:  # noqa: BLE001
        return None


def login_enabled() -> bool:
    svc = _login_service()
    if svc is None:
        return False
    try:
        # SMAppServiceStatusEnabled == 1
        return int(svc.status()) == 1
    except Exception:  # noqa: BLE001
        return False


def set_login_enabled(enabled: bool) -> tuple[bool, str]:
    """(Des)active le lancement au login. Renvoie (succes, message)."""
    svc = _login_service()
    if svc is None:
        return False, "Indisponible (lance hors de l'app .app)."
    try:
        if enabled:
            ok, err = svc.registerAndReturnError_(None)
        else:
            ok, err = svc.unregisterAndReturnError_(None)
        if ok:
            return True, ""
        return False, str(err)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --- Delegue de l'application ----------------------------------------------

class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):  # noqa: N802, ARG002
        # 1er lancement depuis Telechargements : proposer le deplacement dans
        # /Applications (chemin canonique, autorisations stables). Si on lance
        # le deplacement, l'app se relance de la-bas et on s'arrete ici.
        if self._maybe_offer_move_to_applications():
            return

        # Indicateur visuel (pastille). Cree sur le main thread.
        try:
            from indicator import Indicator

            self._indicator = Indicator()
        except Exception:  # noqa: BLE001
            self._indicator = None
        self._last_rendered = None
        self._onboarding = None

        # Menu principal (invisible pour une app LSUIElement, mais indispensable :
        # c'est lui qui apporte les raccourcis Cmd+C/V/X/A au champ de saisie de
        # la cle API ; sans ce menu, Cmd+V ne colle pas).
        self._install_main_menu()

        # Coeur partage : worker + (tentative) event tap.
        core.start_worker()
        core.install_event_tap()  # peut echouer tant que la permission manque

        self._build_status_item()

        # Onboarding au 1er lancement : pas de cle OU permissions manquantes.
        if not credentials.has_api_key() or not self._all_permissions_ok():
            self.showOnboarding_(None)

        # Timer unique : rafraichit la pastille, (re)arme le tap des que la
        # permission est accordee, et met a jour le menu/onboarding.
        from AppKit import NSTimer

        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            config.INDICATOR_TICK_SECONDS, self, b"tick:", None, True
        )

        # Pre-vol : verifie la cle / la connexion au demarrage pour prevenir
        # AVANT d'enregistrer 5 min pour rien (proxy TLS, cle refusee, reseau).
        self._preflight_key_check()

    @objc.python_method
    def _preflight_key_check(self) -> None:
        """Teste la cle en tache de fond ; notifie si elle echoue (proxy/cle/reseau).

        Ne bloque pas le lancement. Silencieux si tout va bien ou si aucune cle
        n'est encore renseignee (l'onboarding s'en charge alors)."""
        if not credentials.has_api_key():
            return

        def _go() -> None:
            ok, msg = transcribe.test_api_key()
            if not ok:
                # Draine par tick_ (main thread) en notification macOS.
                core.errors.put(msg)

        threading.Thread(target=_go, daemon=True).start()

    # --- Premier lancement : installation dans /Applications ---
    @objc.python_method
    def _maybe_offer_move_to_applications(self) -> bool:
        """Propose de deplacer l'app dans /Applications. True si un deplacement
        a ete lance (l'app va se relancer ; l'appelant doit s'arreter)."""
        if not getattr(sys, "frozen", False):
            return False  # mode dev (python app.py) : rien a faire
        bundle = NSBundle.mainBundle().bundlePath()
        if not bundle or not bundle.endswith(".app"):
            return False
        if bundle.startswith("/Applications/"):
            return False

        from AppKit import NSAlert

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Déplacer MistralSTT dans Applications ?")
        alert.setInformativeText_(
            "Pour un fonctionnement fiable (autorisations qui tiennent dans le "
            "temps), MistralSTT doit vivre dans le dossier Applications. "
            "Je peux l'y déplacer maintenant."
        )
        alert.addButtonWithTitle_("Déplacer dans Applications")
        alert.addButtonWithTitle_("Pas maintenant")
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if alert.runModal() != 1000:  # 1000 = premier bouton
            return False

        dest = os.path.join("/Applications", os.path.basename(bundle))
        try:
            if os.path.abspath(dest) != os.path.abspath(bundle):
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                shutil.move(bundle, dest)
        except Exception as exc:  # noqa: BLE001
            self._alert(
                "Déplacement impossible",
                f"Glisse MistralSTT dans Applications manuellement.\n\n{exc}",
            )
            return False

        # Relance la copie installee, puis quitte l'instance courante.
        subprocess.Popen(["open", dest])
        NSApplication.sharedApplication().terminate_(self)
        return True

    # --- Menu principal (raccourcis d'edition) ---
    @objc.python_method
    def _install_main_menu(self) -> None:
        main = NSMenu.alloc().init()

        # Menu applicatif (Quitter avec Cmd+Q).
        app_item = NSMenuItem.alloc().init()
        main.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_(
            "Quitter MistralSTT", b"terminate:", "q"
        )
        app_item.setSubmenu_(app_menu)

        # Menu Edition : porte les selecteurs standard cut:/copy:/paste:/selectAll:
        # avec leurs raccourcis. C'est ce qui rend Cmd+V fonctionnel.
        edit_item = NSMenuItem.alloc().init()
        main.addItem_(edit_item)
        edit_menu = NSMenu.alloc().initWithTitle_("Édition")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Annuler", b"undo:", "z")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Rétablir", b"redo:", "Z")
        edit_menu.addItem_(NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Couper", b"cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copier", b"copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Coller", b"paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_(
            "Tout sélectionner", b"selectAll:", "a"
        )
        edit_item.setSubmenu_(edit_menu)

        NSApplication.sharedApplication().setMainMenu_(main)

    # --- Barre de menus ---
    @objc.python_method
    def _build_status_item(self) -> None:
        bar = NSStatusBar.systemStatusBar()
        self._status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        button = self._status_item.button()
        # Icone monochrome SF Symbol (s'adapte au theme clair/sombre) plutot
        # qu'un emoji. Repli sur un glyphe si le symbole est indisponible.
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "mic.fill", "MistralSTT"
        )
        if img is not None:
            img.setTemplate_(True)
            button.setImage_(img)
        else:
            button.setTitle_("🎙")

        menu = NSMenu.alloc().init()

        self._state_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "MistralSTT", None, ""
        )
        self._state_item.setEnabled_(False)
        menu.addItem_(self._state_item)
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(
            self._mk_item("Saisir la clé API…", b"enterApiKey:")
        )
        self._login_item = self._mk_item(
            "Lancer au démarrage", b"toggleLogin:"
        )
        menu.addItem_(self._login_item)
        menu.addItem_(self._mk_item("Permissions…", b"showOnboarding:"))
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._mk_item("Quitter", b"quitApp:"))

        self._status_item.setMenu_(menu)
        self._refresh_menu()

    @objc.python_method
    def _mk_item(self, title: str, action: bytes) -> NSMenuItem:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        item.setTarget_(self)
        return item

    @objc.python_method
    def _all_permissions_ok(self) -> bool:
        return has_input_monitoring() and has_accessibility()

    @objc.python_method
    def _refresh_menu(self) -> None:
        if self._all_permissions_ok() and core._tap is not None:
            self._state_item.setTitle_("MistralSTT — prêt (⌥ droite)")
        else:
            self._state_item.setTitle_("MistralSTT — permissions requises")
        self._login_item.setState_(
            NSControlStateValueOn if login_enabled() else NSControlStateValueOff
        )

    # --- Timer principal ---
    def tick_(self, timer):  # noqa: N802, ARG002
        # (Re)arme le tap des que la Surveillance des entrees est accordee.
        if core._tap is None and has_input_monitoring():
            core.install_event_tap()

        # Pastille : on rend uniquement au changement d'etat.
        if self._indicator is not None:
            s = core._ui_state
            if s != self._last_rendered:
                self._indicator.render(s)
                self._last_rendered = s
            # Ré-affirme le premier plan (survit aux passages plein ecran).
            self._indicator.tick()

        # Erreurs du worker -> notification macOS.
        try:
            while True:
                _notify(core.errors.get_nowait())
        except queue.Empty:
            pass

        self._refresh_menu()
        if self._onboarding is not None:
            self._update_onboarding_status()
            # Resultat du "Tester la clé" (calcule dans un thread reseau).
            result = getattr(self, "_keytest_result", None)
            if result is not None:
                self._ob_keytest.setStringValue_(result)
                self._keytest_result = None

    # --- Actions du menu ---
    def enterApiKey_(self, sender):  # noqa: N802, ARG002
        current = credentials.get_api_key() or ""
        value = self._prompt_secret(
            "Clé API Mistral",
            "Colle ta clé API Mistral (console.mistral.ai).",
            current,
        )
        if value is not None and value.strip():
            credentials.set_api_key(value.strip())
            transcribe.reset_client()
            self._preflight_key_check()

    def toggleLogin_(self, sender):  # noqa: N802, ARG002
        ok, msg = set_login_enabled(not login_enabled())
        if not ok:
            self._alert("Lancer au démarrage", msg or "Échec.")
        self._refresh_menu()

    def quitApp_(self, sender):  # noqa: N802, ARG002
        try:
            core.recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        NSApplication.sharedApplication().terminate_(self)

    # --- Onboarding (fenetre simple) ---
    def showOnboarding_(self, sender):  # noqa: N802, ARG002
        if self._onboarding is not None:
            self._onboarding.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return

        w, h = 460, 500
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        win.setTitle_("MistralSTT — Configuration")
        # Fenetre NORMALE (pas "toujours au-dessus") : elle s'affiche sur le
        # bureau plutot qu'en surimpression d'une app en plein ecran.
        # Sinon la fenetre est liberee a la fermeture (bouton rouge) -> la
        # reference pendante ferait planter une reouverture via le menu.
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self)
        # Centre sur l'ecran ou se trouve le curseur (la ou l'utilisateur
        # travaille), pas sur l'ecran principal : evite de s'ouvrir par-dessus
        # une autre app sur un autre moniteur.
        self._center_on_active_screen(win, w, h)
        content = win.contentView()

        def label(text, x, y, lw, lh, bold=False):
            f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, lw, lh))
            f.setStringValue_(text)
            f.setBezeled_(False)
            f.setDrawsBackground_(False)
            f.setEditable_(False)
            f.setSelectable_(False)
            content.addSubview_(f)
            return f

        def button(title, x, y, bw, action):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, bw, 28))
            b.setTitle_(title)
            b.setBezelStyle_(1)  # rounded
            b.setTarget_(self)
            b.setAction_(action)
            content.addSubview_(b)
            return b

        label("Bienvenue dans MistralSTT", 20, h - 36, w - 40, 22, bold=True)

        # Rappel des commandes : l'utilisateur ne les voit nulle part ailleurs.
        label("Commandes :", 20, h - 62, w - 40, 18, bold=True)
        label(
            "• Maintiens ⌥ Option droite, parle, relâche : le texte s'insère.",
            20, h - 82, w - 40, 18,
        )
        label(
            "• ⌥ Option droite + Espace : écoute mains-libres (⌥ pour arrêter).",
            20, h - 102, w - 40, 18,
        )
        label(
            "• Échap : annule l'enregistrement en cours.",
            20, h - 122, w - 40, 18,
        )

        # Cle API
        label("1. Clé API Mistral", 20, h - 160, 200, 18)
        self._ob_key = NSSecureTextField.alloc().initWithFrame_(
            NSMakeRect(20, h - 190, 300, 24)
        )
        self._ob_key.setStringValue_(credentials.get_api_key() or "")
        content.addSubview_(self._ob_key)
        button("Enregistrer", 330, h - 192, 110, b"saveKeyFromOnboarding:")
        button("Tester la clé", 20, h - 228, 140, b"testKey:")
        self._ob_keytest = label("", 170, h - 224, 270, 18)

        # Permissions
        label("2. Autorisations macOS (clique, coche, reviens ici)", 20, h - 268, w - 40, 18)

        self._ob_mic = label("• Micro : —", 20, h - 296, 230, 18)
        button("Autoriser", 250, h - 298, 190, b"reqMic:")

        self._ob_input = label("• Surveillance des entrées : —", 20, h - 328, 230, 18)
        button("Autoriser", 250, h - 330, 190, b"reqInput:")

        self._ob_ax = label("• Accessibilité : —", 20, h - 360, 230, 18)
        button("Autoriser", 250, h - 362, 190, b"reqAx:")

        # Demarrage automatique (case a cocher).
        label("3. Démarrage", 20, h - 396, 200, 18)
        self._ob_login = NSButton.alloc().initWithFrame_(NSMakeRect(20, 54, 300, 22))
        self._ob_login.setButtonType_(NSButtonTypeSwitch)
        self._ob_login.setTitle_("Lancer à l'ouverture de session")
        self._ob_login.setTarget_(self)
        self._ob_login.setAction_(b"toggleLoginFromOnboarding:")
        content.addSubview_(self._ob_login)

        button("Terminé", w - 130, 18, 110, b"closeOnboarding:")

        self._onboarding = win
        self._update_onboarding_status()
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def _center_on_active_screen(self, win, w: int, h: int) -> None:
        """Place la fenetre, centree, sur l'ecran qui contient le curseur.

        `NSWindow.center()` vise l'ecran principal (celui de la barre de menus) ;
        sur un setup multi-ecrans la fenetre s'ouvrait alors loin de l'utilisateur
        (ex: par-dessus le navigateur d'un autre moniteur). On choisit plutot
        l'ecran ou se trouve la souris, et on se rabat sur l'ecran principal."""
        from AppKit import NSEvent, NSMakePoint, NSScreen

        mouse = NSEvent.mouseLocation()
        target = None
        for scr in NSScreen.screens():
            fr = scr.frame()
            if (fr.origin.x <= mouse.x <= fr.origin.x + fr.size.width
                    and fr.origin.y <= mouse.y <= fr.origin.y + fr.size.height):
                target = scr
                break
        if target is None:
            target = NSScreen.mainScreen()
        vf = target.visibleFrame()
        ox = vf.origin.x + (vf.size.width - w) / 2.0
        oy = vf.origin.y + (vf.size.height - h) / 2.0
        win.setFrameOrigin_(NSMakePoint(ox, oy))

    @objc.python_method
    def _update_onboarding_status(self) -> None:
        if self._onboarding is None:
            return

        def mark(ok):
            return "✅" if ok else "❌"

        # Le micro n'a pas d'API de check simple sans AVFoundation : on indique
        # juste l'action.
        self._ob_mic.setStringValue_("• Micro : (clique pour autoriser)")
        self._ob_input.setStringValue_(
            f"• Surveillance des entrées : {mark(has_input_monitoring())}"
        )
        self._ob_ax.setStringValue_(
            f"• Accessibilité : {mark(has_accessibility())}"
        )
        # Reflete l'etat reel du lancement au login (peut avoir change via le menu).
        self._ob_login.setState_(
            NSControlStateValueOn if login_enabled() else NSControlStateValueOff
        )

    def saveKeyFromOnboarding_(self, sender):  # noqa: N802, ARG002
        value = self._ob_key.stringValue()
        if value and value.strip():
            credentials.set_api_key(value.strip())
            transcribe.reset_client()
            self._preflight_key_check()

    def testKey_(self, sender):  # noqa: N802, ARG002
        # Enregistre d'abord ce qui est tape, puis teste en tache de fond (reseau).
        value = self._ob_key.stringValue()
        if value and value.strip():
            credentials.set_api_key(value.strip())
            transcribe.reset_client()
        self._ob_keytest.setStringValue_("Test en cours…")
        self._keytest_result = None

        def _go() -> None:
            ok, msg = transcribe.test_api_key()
            # Lu par tick_ (main thread) pour mettre a jour le label sans risque.
            self._keytest_result = msg

        threading.Thread(target=_go, daemon=True).start()

    def toggleLoginFromOnboarding_(self, sender):  # noqa: N802, ARG002
        want = sender.state() == NSControlStateValueOn
        ok, msg = set_login_enabled(want)
        if not ok:
            # Remet la case dans l'etat reel et explique.
            sender.setState_(
                NSControlStateValueOn if login_enabled() else NSControlStateValueOff
            )
            self._alert("Lancer au démarrage", msg or "Échec.")
        self._refresh_menu()

    def reqMic_(self, sender):  # noqa: N802, ARG002
        trigger_microphone_prompt()
        _open_pane(_PANE_MIC)

    def reqInput_(self, sender):  # noqa: N802, ARG002
        request_input_monitoring()
        _open_pane(_PANE_INPUT)

    def reqAx_(self, sender):  # noqa: N802, ARG002
        request_accessibility()
        _open_pane(_PANE_AX)

    def closeOnboarding_(self, sender):  # noqa: N802, ARG002
        if self._onboarding is not None:
            self._onboarding.close()
            self._onboarding = None

    def windowWillClose_(self, notification):  # noqa: N802, ARG002
        self._onboarding = None

    # --- Petites boites de dialogue ---
    @objc.python_method
    def _prompt_secret(self, title: str, message: str, default: str):
        from AppKit import NSAlert

        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Annuler")
        field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        field.setStringValue_(default)
        alert.setAccessoryView_(field)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        resp = alert.runModal()
        if resp == 1000:  # NSAlertFirstButtonReturn
            return field.stringValue()
        return None

    @objc.python_method
    def _alert(self, title: str, message: str) -> None:
        from AppKit import NSAlert

        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert.runModal()


def main() -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
