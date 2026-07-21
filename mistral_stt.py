"""MistralSpeechToText : dictee vocale locale (speech-to-text).

Maintiens Option DROITE pour parler -> relache -> le texte s'insere au curseur.
Pendant le maintien, appuie sur Espace pour passer en ecoute continue (mains
libres) ; un nouvel appui sur Option droite stoppe et transcrit.

Lancer :        uv run python mistral_stt.py
Mode debug :    MISTRAL_STT_DEBUG=1 uv run python mistral_stt.py
Quitter :       Ctrl+C dans le terminal.
"""

import os
import queue
import signal
import subprocess
import threading

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
)
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopRunInMode,
    CFRunLoopStop,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CGEventMaskBit,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
    kCFRunLoopDefaultMode,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGEventTapDisabledByTimeout,
    kCGEventTapDisabledByUserInput,
    kCGEventTapOptionDefault,
    kCGHeadInsertEventTap,
    kCGKeyboardEventKeycode,
    kCGSessionEventTap,
)

import config
import history
from audio import Recorder, list_input_devices
from inserter import insert_at_cursor
from transcribe import classify_error, transcribe

DEBUG = bool(os.environ.get("MISTRAL_STT_DEBUG"))

# Etats
IDLE = "idle"
RECORDING_PTT = "ptt"
RECORDING_CONTINUOUS = "continuous"

state = IDLE
recorder = Recorder()
_actions: "queue.Queue[str]" = queue.Queue()
_tap = None
_tap_thread = None
_running = True

# Etat de l'indicateur visuel, partage entre threads (lecture: main thread).
# Valeurs: "idle" | "recording" | "transcribing" | "cancelled".
# Une simple affectation de string est atomique sous le GIL et chaque transition
# a un seul ecrivain, donc pas de lock necessaire ; le main thread ne fait que
# le LIRE pour piloter la pastille.
_ui_state = "idle"

# Hook optionnel invoque (depuis N'IMPORTE quel thread) a chaque changement de
# _ui_state. app.py y branche un reveil du main thread (performSelectorOnMainThread)
# pour que la pastille apparaisse IMMEDIATEMENT, sans attendre le prochain tick.
# Ainsi la cadence repos du timer peut etre lente sans latence visible.
on_ui_state_change = None


def _set_ui_state(value: str) -> None:
    """Affecte l'etat UI partage et notifie l'UI (si un hook est branche)."""
    global _ui_state
    _ui_state = value
    cb = on_ui_state_change
    if cb is not None:
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass


# File des erreurs utilisateur (ex: cle invalide, reseau, proxy TLS). Le worker
# y depose un message ; l'app (app.py) la draine sur le main thread pour afficher
# une notification macOS. En mode CLI, l'erreur reste seulement imprimee.
errors: "queue.Queue[str]" = queue.Queue()


def _log(msg: str) -> None:
    if DEBUG:
        print(f"[mistral-stt:debug] {msg}")


def _friendly_error(exc: Exception) -> str:
    """Traduit une exception de transcription en message court et actionnable.

    Delegue au classifieur partage (transcribe.classify_error) : le SSL/proxy y
    est teste AVANT l'auth, donc un blocage proxy n'est plus etiquete "cle".
    """
    return classify_error(exc)[1]


def _play(sound: str | None) -> None:
    if sound:
        subprocess.Popen(
            ["afplay", sound],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _worker() -> None:
    """Execute start/stop/transcription HORS du thread du tap.

    Le callback du tap doit rester ultra-rapide : s'il bloque (ouverture du
    flux micro, appel reseau...), macOS desactive le tap et plus aucun
    evenement n'arrive (dont le relachement). On deporte donc tout ici.
    """
    global _ui_state
    while True:
        action = _actions.get()
        if action == "__quit__":
            return
        if action == "start":
            recorder.start()
            _play(config.SOUND_START)
            print("[mistral-stt] enregistrement...")
        elif action == "cancel":
            # Annulation (Echap) : on jette la prise, sans transcrire ni coller.
            wav_path = recorder.stop()
            if wav_path:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
            print("[mistral-stt] annule")
            # Le flash visuel a deja ete arme par le callback (_ui_state).
        elif action == "stop":
            wav_path = recorder.stop()
            _play(config.SOUND_DONE)
            if not wav_path:
                print("[mistral-stt] (rien a transcrire)")
                _set_ui_state("idle")
                continue
            _set_ui_state("transcribing")
            try:
                text = transcribe(wav_path)
                if text:
                    # On imprime le texte complet dans le terminal : il sert
                    # d'historique copiable a la main si le collage s'est perdu.
                    print(f"[mistral-stt] insere ({len(text)} caracteres):")
                    print(text)
                    # Journalise AVANT le collage : la trace existe meme si le
                    # collage se perd (aucun champ texte focalise).
                    history.append(text)
                    insert_at_cursor(
                        text, restore=not config.KEEP_LAST_IN_CLIPBOARD
                    )
                else:
                    print("[mistral-stt] (transcription vide)")
            except Exception as exc:  # noqa: BLE001
                print(f"[mistral-stt] erreur transcription: {exc}")
                errors.put(_friendly_error(exc))
            finally:
                _set_ui_state("idle")
                try:
                    os.remove(wav_path)
                except OSError:
                    pass


def _tap_callback(proxy, type_, event, refcon):  # noqa: ARG001
    global state, _ui_state

    # Le systeme peut desactiver le tap (callback trop lent, evenement clavier
    # special). Il faut le reactiver, sinon plus aucun evenement n'arrive.
    if type_ in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        _log("tap desactive par le systeme -> reactivation")
        if _tap is not None:
            CGEventTapEnable(_tap, True)
        return event

    try:
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)

        # --- Option droite (flagsChanged) ---
        if type_ == kCGEventFlagsChanged and keycode == config.TRIGGER_KEYCODE:
            flags = CGEventGetFlags(event)
            is_down = bool(flags & config.TRIGGER_FLAG_MASK)
            _log(f"trigger {'DOWN' if is_down else 'UP'} (state={state})")
            if is_down:
                if state == IDLE:
                    state = RECORDING_PTT
                    # Reste ultra rapide : on note l'etat + on reveille l'UI
                    # (la pastille apparait des l'appui, latence minimale).
                    _set_ui_state("recording")
                    _actions.put("start")
                elif state == RECORDING_CONTINUOUS:
                    state = IDLE
                    _actions.put("stop")
            else:  # relachement
                if state == RECORDING_PTT:
                    state = IDLE
                    _actions.put("stop")
            return event

        # --- Espace pendant un maintien PTT -> bascule mains libres ---
        if type_ == kCGEventKeyDown and keycode == config.SPACE_KEYCODE:
            if state == RECORDING_PTT:
                state = RECORDING_CONTINUOUS
                print(
                    "[mistral-stt] ecoute continue (mains libres) — "
                    "Option droite pour stopper"
                )
                return None  # on avale l'espace declencheur

        # --- Echap pendant un enregistrement -> annulation ---
        if type_ == kCGEventKeyDown and keycode == config.ESCAPE_KEYCODE:
            if state in (RECORDING_PTT, RECORDING_CONTINUOUS):
                state = IDLE
                _set_ui_state("cancelled")  # arme le flash de confirmation
                _actions.put("cancel")
                return None  # on avale l'Echap (uniquement pendant un enregistrement)

        if DEBUG and type_ == kCGEventFlagsChanged:
            _log(f"flagsChanged keycode={keycode} (ignore)")
    except Exception as exc:  # noqa: BLE001
        _log(f"erreur callback: {exc}")

    return event


def _sigint(signum, frame):  # noqa: ARG001
    global _running
    _running = False


# --- Coeur partage (reutilise par mistral_stt.py CLI ET par app.py) --------

def start_worker() -> "threading.Thread":
    """Demarre le thread worker (traitement start/stop/transcription)."""
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def _tap_thread_main(ready: "threading.Event", result: dict) -> None:
    """Cree le tap et POMPE sa propre run loop, sur un thread DEDIE.

    Le tap doit etre cree et sa source ajoutee sur le thread qui pompe la run
    loop. On isole ainsi la livraison des frappes du thread principal AppKit :
    meme si celui-ci est occupe (timer) ou bloque dans une fenetre modale
    (NSAlert.runModal, onboarding), le callback clavier continue d'etre servi
    ici -> plus de latence clavier a l'echelle du systeme.
    """
    global _tap

    mask = CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown)
    tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,
        mask,
        _tap_callback,
        None,
    )
    if tap is None:
        # Permission "Surveillance des entrees" manquante : on previent l'appelant
        # et le thread se termine (il sera relance quand la permission arrivera).
        result["ok"] = False
        ready.set()
        return

    source = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    _tap = tap
    result["ok"] = True
    ready.set()
    # Bloque ici pour la vie du process (thread daemon) : c'est cette run loop
    # dediee qui sert le callback clavier.
    CFRunLoopRun()


def install_event_tap() -> bool:
    """Demarre (si besoin) le thread dedie qui porte l'event tap clavier.

    Renvoie True si le tap est en place, False si macOS l'a refuse (permission
    "Surveillance des entrees" manquante). Idempotent : ne recree pas un tap
    deja installe et ne relance pas un thread deja en cours d'initialisation.
    Peut etre appele depuis n'importe quel thread (typiquement le main thread).
    """
    global _tap_thread
    if _tap is not None:
        return True
    # Thread deja demarre mais tap pas encore pret : ne pas en lancer un 2e.
    if _tap_thread is not None and _tap_thread.is_alive():
        return _tap is not None

    ready = threading.Event()
    result: dict = {"ok": False}
    _tap_thread = threading.Thread(
        target=_tap_thread_main, args=(ready, result), daemon=True
    )
    _tap_thread.start()
    # Attente bornee : la creation du tap est quasi instantanee ; le timeout
    # evite tout blocage du main thread si quelque chose tourne mal.
    ready.wait(timeout=2.0)
    return bool(result.get("ok"))


def main() -> None:
    global _tap

    print("MistralSpeechToText — dictee vocale (Mistral Voxtral)")
    print("Micros detectes:")
    print(list_input_devices())
    print(
        "\nMaintiens Option DROITE pour parler. "
        "Option droite + Espace = ecoute continue. Ctrl+C pour quitter."
    )
    if DEBUG:
        print("[mistral-stt] mode debug actif (MISTRAL_STT_DEBUG)")
    print()

    signal.signal(signal.SIGINT, _sigint)

    # Indicateur visuel (NSPanel). AppKit doit vivre sur le main thread ; on
    # initialise NSApplication SANS appeler run() : c'est la boucle
    # CFRunLoopRunInMode ci-dessous qui pompe le run loop (et donc dessine /
    # anime la pastille), ce qui preserve l'arret propre Ctrl+C.
    indicator = None
    if config.INDICATOR_ENABLED:
        try:
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            app.finishLaunching()
            from indicator import Indicator

            indicator = Indicator()
        except Exception as exc:  # noqa: BLE001
            print(f"[mistral-stt] indicateur visuel desactive ({exc})")
            indicator = None

    start_worker()

    if not install_event_tap():
        print(
            "[mistral-stt] Impossible de creer l'event tap.\n"
            "  -> Reglages Systeme > Confidentialite et securite >\n"
            "     Surveillance des entrees ET Accessibilite : autorise ton "
            "terminal,\n"
            "     puis relance."
        )
        raise SystemExit(1)

    # Boucle qui rend la main a Python a chaque tick : c'est ce qui permet au
    # Ctrl+C (SIGINT) d'etre traite (CFRunLoopRun() pur le bloquerait) ET ce qui
    # pilote l'indicateur visuel (on lit l'etat partage et on rafraichit la
    # pastille uniquement quand il change).
    #
    # Cadence adaptative : lente au repos (rien a faire), rapide quand la
    # pastille est visible (reaffirmation premier plan). Un changement d'etat
    # (thread du tap/worker) reveille cette boucle immediatement via
    # CFRunLoopStop -> pas de latence d'apparition malgre la cadence repos.
    if indicator is not None:
        main_loop = CFRunLoopGetCurrent()

        global on_ui_state_change
        on_ui_state_change = lambda: CFRunLoopStop(main_loop)  # noqa: E731

    last_rendered = None
    while _running:
        if indicator is None:
            tick = 0.25
        elif _ui_state in ("recording", "transcribing"):
            tick = config.INDICATOR_TICK_SECONDS
        else:
            # Repos (y compris "cancelled" apres le flash) : cadence lente.
            tick = config.INDICATOR_TICK_IDLE_SECONDS
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, tick, False)
        if indicator is not None:
            s = _ui_state
            if s != last_rendered:
                indicator.render(s)
                last_rendered = s
            # Ré-affirme le premier plan (survit aux passages plein ecran).
            indicator.tick()

    print("\n[mistral-stt] arret.")
    try:
        recorder.stop()
    except Exception:  # noqa: BLE001
        pass
    if indicator is not None:
        try:
            indicator.render("idle")
        except Exception:  # noqa: BLE001
            pass
    _actions.put("__quit__")


if __name__ == "__main__":
    main()
