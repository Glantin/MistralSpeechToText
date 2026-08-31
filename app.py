"""MistralSpeechToText — entry point of the macOS app (.app).

A menu-bar app (no Dock icon): hold **Right Option** to dictate, just like in CLI
mode. We REUSE the whole core of `mistral_stt.py` (keyboard event tap + worker +
shared `_ui_state`); only the way it runs changes:

  - `NSApplication.run()` (the AppKit loop) instead of the manual
    `CFRunLoopRunInMode` loop of CLI mode;
  - an `NSStatusItem` (menu): API key, launch at login, permissions, quit;
  - an **onboarding** on first launch, because a .app cannot self-grant
    Microphone / Input Monitoring / Accessibility: we trigger the system prompts
    and open the right Settings pane directly.

THREADING: AppKit is not thread-safe. The delegate, the menu, the onboarding and
the indicator live on the MAIN THREAD (the NSApp.run() loop). The worker runs in
a daemon thread (imported from mistral_stt) and only touches the shared state.
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import objc
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
from Foundation import NSBundle
from Quartz import CGPreflightListenEventAccess, CGRequestListenEventAccess

import config
import credentials
import mistral_stt as core
import settings
import transcribe

# System Settings panes (deep links). Open the right Privacy & Security tab
# directly instead of leaving the user to hunt for it.
_PANE_MIC = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
_PANE_INPUT = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
_PANE_AX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


def _open_pane(url: str) -> None:
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _notify(message: str, title: str = "MistralSTT") -> None:
    """Show a macOS notification (via osascript: no permission to request)."""
    msg = message.replace('\\', '').replace('"', "'")
    ttl = title.replace('"', "'")
    subprocess.Popen(
        ["osascript", "-e", f'display notification "{msg}" with title "{ttl}"'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --- Permission state ------------------------------------------------------

def has_input_monitoring() -> bool:
    """Input Monitoring granted? (needed to capture Right Option)."""
    return bool(CGPreflightListenEventAccess())


def request_input_monitoring() -> None:
    """Trigger the system Input Monitoring prompt (only once)."""
    CGRequestListenEventAccess()


def has_accessibility() -> bool:
    """Accessibility granted? (needed to paste via Cmd+V)."""
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}))


def request_accessibility() -> None:
    """Trigger the system Accessibility prompt."""
    AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})


def trigger_microphone_prompt() -> None:
    """Briefly open the mic to trigger the authorization prompt.

    We avoid an AVFoundation dependency: opening a sounddevice stream is enough
    to make the system prompt appear the first time. No effect afterwards.
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


# --- Launch at login (SMAppService) ----------------------------------------

def _login_service():
    """SMAppService for the current app, or None if unavailable (e.g. run outside a .app)."""
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
    """(De)activate launch at login. Returns (success, message)."""
    svc = _login_service()
    if svc is None:
        return False, "Unavailable (running outside the .app)."
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


# --- Application delegate ---------------------------------------------------

class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):  # noqa: N802, ARG002
        # First launch from Downloads: offer to move into /Applications (canonical
        # path, stable permissions). If we start the move, the app relaunches from
        # there and we stop here.
        if self._maybe_offer_move_to_applications():
            return

        # Visual indicator (dot). Created on the main thread.
        try:
            from indicator import Indicator

            self._indicator = Indicator()
        except Exception:  # noqa: BLE001
            self._indicator = None
        self._last_rendered = None
        self._onboarding = None

        # Main menu (invisible for an LSUIElement app, but essential: it is what
        # brings the Cmd+C/V/X/A shortcuts to the API-key input field; without
        # this menu, Cmd+V does not paste).
        self._install_main_menu()

        # Shared core: recording worker + transcription worker (separate thread,
        # persistent retry queue) + (attempt at) the event tap.
        core.start_worker()
        core.start_transcribe_worker()
        n = core.recover_pending()  # resume takes pending from a previous session
        if n:
            core.notices.put(
                f"{n} pending dictation(s) resumed — transcribing…"
            )
        core.install_event_tap()  # may fail while the permission is missing

        self._build_status_item()

        # Onboarding on first launch: no key OR missing permissions.
        if not credentials.has_api_key() or not self._all_permissions_ok():
            self.showOnboarding_(None)

        # ADAPTIVE-cadence timer: slow when idle, fast when the dot is visible
        # (see tick_). Starts on the idle cadence. The core wakes the main thread
        # on each state change (hook below) so the dot appears immediately despite
        # the idle cadence.
        self._timer = None
        self._tick_interval = None
        self._schedule_timer(config.INDICATOR_TICK_IDLE_SECONDS)
        core.on_ui_state_change = self._wake_main

        # Preflight: check the key / connection at startup to warn BEFORE
        # recording 5 min for nothing (TLS proxy, rejected key, network).
        self._preflight_key_check()

    @objc.python_method
    def _preflight_key_check(self) -> None:
        """Test the key in the background; notify if it fails (proxy/key/network).

        Does not block startup. Silent if all is well or if no key is entered yet
        (the onboarding then handles it)."""
        if not credentials.has_api_key():
            return

        def _go() -> None:
            ok, msg = transcribe.test_api_key()
            if not ok:
                # Drained by tick_ (main thread) into a macOS notification.
                core.errors.put(msg)

        threading.Thread(target=_go, daemon=True).start()

    # --- First launch: install into /Applications ---
    @objc.python_method
    def _maybe_offer_move_to_applications(self) -> bool:
        """Offer to move the app into /Applications. True if a move was started
        (the app is about to relaunch; the caller must stop)."""
        if not getattr(sys, "frozen", False):
            return False  # dev mode (python app.py): nothing to do
        bundle = NSBundle.mainBundle().bundlePath()
        if not bundle or not bundle.endswith(".app"):
            return False
        if bundle.startswith("/Applications/"):
            return False

        from AppKit import NSAlert

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Move MistralSTT to Applications?")
        alert.setInformativeText_(
            "For reliable operation (permissions that stick over time), MistralSTT "
            "should live in the Applications folder. I can move it there now."
        )
        alert.addButtonWithTitle_("Move to Applications")
        alert.addButtonWithTitle_("Not now")
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if alert.runModal() != 1000:  # 1000 = first button
            return False

        dest = os.path.join("/Applications", os.path.basename(bundle))
        try:
            if os.path.abspath(dest) != os.path.abspath(bundle):
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                shutil.move(bundle, dest)
        except Exception as exc:  # noqa: BLE001
            self._alert(
                "Move failed",
                f"Drag MistralSTT into Applications manually.\n\n{exc}",
            )
            return False

        # Launch the installed copy, then quit the current instance.
        subprocess.Popen(["open", dest])
        NSApplication.sharedApplication().terminate_(self)
        return True

    # --- Main menu (editing shortcuts) ---
    @objc.python_method
    def _install_main_menu(self) -> None:
        main = NSMenu.alloc().init()

        # Application menu (Quit with Cmd+Q).
        app_item = NSMenuItem.alloc().init()
        main.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_(
            "Quit MistralSTT", b"terminate:", "q"
        )
        app_item.setSubmenu_(app_menu)

        # Edit menu: carries the standard cut:/copy:/paste:/selectAll: selectors
        # with their shortcuts. This is what makes Cmd+V work.
        edit_item = NSMenuItem.alloc().init()
        main.addItem_(edit_item)
        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", b"undo:", "z")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", b"redo:", "Z")
        edit_menu.addItem_(NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", b"cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", b"copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", b"paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_(
            "Select All", b"selectAll:", "a"
        )
        edit_item.setSubmenu_(edit_menu)

        NSApplication.sharedApplication().setMainMenu_(main)

    # --- Menu bar ---
    @objc.python_method
    def _build_status_item(self) -> None:
        bar = NSStatusBar.systemStatusBar()
        self._status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        button = self._status_item.button()
        # Monochrome SF Symbol icon (adapts to the light/dark theme) rather than
        # an emoji. Fall back to a glyph if the symbol is unavailable.
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
            self._mk_item("Enter API key…", b"enterApiKey:")
        )
        self._login_item = self._mk_item(
            "Launch at login", b"toggleLogin:"
        )
        menu.addItem_(self._login_item)
        menu.addItem_(self._mk_item("Permissions…", b"showOnboarding:"))
        menu.addItem_(
            self._mk_item("Vocabulary dictionary…", b"openVocabulary:")
        )
        menu.addItem_(
            self._mk_item("Recording limit…", b"setRecordLimit:")
        )
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._mk_item("Quit", b"quitApp:"))

        # Delegate: the menu state (permissions, login) is recomputed ONLY when
        # the user opens the menu (menuNeedsUpdate:), no longer on every tick.
        # This keeps the costly system calls (AXIsProcessTrusted,
        # CGPreflightListenEventAccess, SMAppService.status) off the 10 Hz path.
        menu.setDelegate_(self)
        self._status_item.setMenu_(menu)
        self._refresh_menu()

    # NSMenuDelegate: recompute the state just before the menu is displayed.
    def menuNeedsUpdate_(self, menu):  # noqa: N802, ARG002
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
            self._state_item.setTitle_("MistralSTT — ready (right ⌥)")
        else:
            self._state_item.setTitle_("MistralSTT — permissions required")
        self._login_item.setState_(
            NSControlStateValueOn if login_enabled() else NSControlStateValueOff
        )

    # --- Main timer ---
    @objc.python_method
    def _schedule_timer(self, interval: float) -> None:
        """(Re)schedule the main timer at `interval` seconds."""
        from AppKit import NSTimer

        if self._timer is not None:
            self._timer.invalidate()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self, b"tick:", None, True
        )
        self._tick_interval = interval

    @objc.python_method
    def _wake_main(self) -> None:
        """Wake the main thread for an immediate render of the dot.

        Called by the core (core.on_ui_state_change) from the tap or worker
        thread, on every UI state change. We schedule a tick on the main thread
        (in common modes, so it passes even during a menu tracking loop)."""
        from Foundation import NSRunLoopCommonModes

        self.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
            b"tick:", None, False, [NSRunLoopCommonModes]
        )

    def tick_(self, timer):  # noqa: N802, ARG002
        # (Re)arm the tap as soon as Input Monitoring is granted. Short-circuit:
        # once the tap is in place, has_input_monitoring() (a system call) is
        # never evaluated again.
        if core._tap is None and has_input_monitoring():
            core.install_event_tap()

        # Warn as the take nears the limit, and auto-stop + send at the limit.
        core.tick_recording_limit()

        # Dot: we render only on a state change.
        if self._indicator is not None:
            s = core._ui_state
            if s != self._last_rendered:
                self._indicator.render(s)
                self._last_rendered = s
            # Re-assert the front (survives full-screen transitions).
            self._indicator.tick()

        # Worker errors -> macOS notification.
        try:
            while True:
                _notify(core.errors.get_nowait())
        except queue.Empty:
            pass

        # POSITIVE notifications (e.g. a deferred transcription recovered).
        try:
            while True:
                _notify(core.notices.get_nowait())
        except queue.Empty:
            pass

        # Onboarding open: refresh its state (the setup window, briefly).
        if self._onboarding is not None:
            self._update_onboarding_status()
            # Result of "Test the key" (computed in a network thread).
            result = getattr(self, "_keytest_result", None)
            if result is not None:
                self._ob_keytest.setStringValue_(result)
                self._keytest_result = None

        # Adaptive cadence: fast during recording/transcription (the dot is
        # re-asserted to the front often), slow otherwise. NB: the "cancelled" and
        # "recovered" states stay stuck after their flash (the animation ends on
        # its own via Core Animation), so we do NOT count them as active, or we
        # would stay on the fast cadence forever. "retrying" (network wait) can
        # last a long time: we leave it on the IDLE cadence too (0.75 s is enough
        # to re-assert the front; avoids a permanent 10 Hz). The menu refreshes
        # when it opens (menuNeedsUpdate:).
        desired = (
            config.INDICATOR_TICK_SECONDS
            if core._ui_state in ("recording", "recording_long", "transcribing")
            else config.INDICATOR_TICK_IDLE_SECONDS
        )
        if desired != self._tick_interval:
            self._schedule_timer(desired)

    # --- Menu actions ---
    def enterApiKey_(self, sender):  # noqa: N802, ARG002
        current = credentials.get_api_key() or ""
        value = self._prompt_secret(
            "Mistral API key",
            "Paste your Mistral API key (console.mistral.ai).",
            current,
        )
        if value is not None and value.strip():
            credentials.set_api_key(value.strip())
            transcribe.reset_client()
            self._preflight_key_check()

    def openVocabulary_(self, sender):  # noqa: N802, ARG002
        """Open the vocabulary dictionary (context_bias) in the editor.

        Creates it with a help header if it does not exist yet. One entry per
        line; these terms bias the transcription (no extra request/credit, no
        summarization)."""
        try:
            path = transcribe.ensure_vocab_file()
            subprocess.Popen(["open", "-t", path])
            # Warn if some lines are ignored (space/comma -> invalid for the API):
            # they do not influence the transcription, the user must split them
            # into words. Non-blocking (notification).
            ignored = transcribe.ignored_bias_terms()
            if ignored:
                sample = ", ".join(ignored[:3])
                more = "…" if len(ignored) > 3 else ""
                _notify(
                    f"{len(ignored)} term(s) ignored (space/comma): "
                    f"{sample}{more}. One word per line."
                )
        except Exception as exc:  # noqa: BLE001
            self._alert("Dictionary unavailable", str(exc))

    def setRecordLimit_(self, sender):  # noqa: N802, ARG002
        """Adjust the recording limit (minutes). The only place a long take is cut.

        Default is low for RAM; can be raised up to the Voxtral API ceiling
        (60 min). The warning still fires ~1.5 min before whatever the limit is."""
        current = settings.get_max_record_minutes()
        value = self._prompt_text(
            "Recording limit",
            f"Maximum length of a single take, in minutes (1–"
            f"{config.MAX_RECORD_CEILING_MINUTES}). Longer takes use more RAM; "
            f"audio past the limit isn't captured. Currently {current} min.",
            str(current),
        )
        if value is None:
            return
        try:
            minutes = int(value.strip())
        except (TypeError, ValueError):
            self._alert("Recording limit", "Please enter a whole number of minutes.")
            return
        stored = settings.set_max_record_minutes(minutes)
        _notify(f"Recording limit set to {stored} min (applies to the next take).")

    def toggleLogin_(self, sender):  # noqa: N802, ARG002
        ok, msg = set_login_enabled(not login_enabled())
        if not ok:
            self._alert("Launch at login", msg or "Failed.")
        self._refresh_menu()

    def quitApp_(self, sender):  # noqa: N802, ARG002
        try:
            core.recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        NSApplication.sharedApplication().terminate_(self)

    # --- Onboarding (a simple window) ---
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
        win.setTitle_("MistralSTT — Setup")
        # NORMAL window (not "always on top"): it shows on the desktop rather than
        # overlaid on a full-screen app. And it is not released on close (red
        # button) -> the dangling reference would otherwise crash a reopen from
        # the menu.
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self)
        # Center on the screen that holds the cursor (where the user works), not
        # the main screen: avoids opening on top of another app on another
        # monitor.
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

        label("Welcome to MistralSTT", 20, h - 36, w - 40, 22, bold=True)

        # Reminder of the commands: the user sees them nowhere else.
        label("Commands:", 20, h - 62, w - 40, 18, bold=True)
        label(
            "• Hold ⌥ Right Option, speak, release: the text is inserted.",
            20, h - 82, w - 40, 18,
        )
        label(
            "• ⌥ Right Option + Space: hands-free listening (⌥ to stop).",
            20, h - 102, w - 40, 18,
        )
        label(
            "• Esc: cancels the current recording.",
            20, h - 122, w - 40, 18,
        )

        # API key
        label("1. Mistral API key", 20, h - 160, 200, 18)
        self._ob_key = NSSecureTextField.alloc().initWithFrame_(
            NSMakeRect(20, h - 190, 300, 24)
        )
        self._ob_key.setStringValue_(credentials.get_api_key() or "")
        content.addSubview_(self._ob_key)
        button("Save", 330, h - 192, 110, b"saveKeyFromOnboarding:")
        button("Test the key", 20, h - 228, 140, b"testKey:")
        self._ob_keytest = label("", 170, h - 224, 270, 18)

        # Permissions
        label("2. macOS permissions (click, toggle on, come back here)", 20, h - 268, w - 40, 18)

        self._ob_mic = label("• Microphone: —", 20, h - 296, 230, 18)
        button("Allow", 250, h - 298, 190, b"reqMic:")

        self._ob_input = label("• Input Monitoring: —", 20, h - 328, 230, 18)
        button("Allow", 250, h - 330, 190, b"reqInput:")

        self._ob_ax = label("• Accessibility: —", 20, h - 360, 230, 18)
        button("Allow", 250, h - 362, 190, b"reqAx:")

        # Launch at login (checkbox).
        label("3. Startup", 20, h - 396, 200, 18)
        self._ob_login = NSButton.alloc().initWithFrame_(NSMakeRect(20, 54, 300, 22))
        self._ob_login.setButtonType_(NSButtonTypeSwitch)
        self._ob_login.setTitle_("Launch at login")
        self._ob_login.setTarget_(self)
        self._ob_login.setAction_(b"toggleLoginFromOnboarding:")
        content.addSubview_(self._ob_login)

        button("Done", w - 130, 18, 110, b"closeOnboarding:")

        self._onboarding = win
        self._update_onboarding_status()
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def _center_on_active_screen(self, win, w: int, h: int) -> None:
        """Place the window, centered, on the screen that holds the cursor.

        `NSWindow.center()` targets the main screen (the menu-bar one); on a
        multi-screen setup the window then opened far from the user (e.g. over the
        browser on another monitor). We pick the screen where the mouse is, and
        fall back to the main screen."""
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

        # The mic has no simple check API without AVFoundation: we just state the
        # action.
        self._ob_mic.setStringValue_("• Microphone: (click to allow)")
        self._ob_input.setStringValue_(
            f"• Input Monitoring: {mark(has_input_monitoring())}"
        )
        self._ob_ax.setStringValue_(
            f"• Accessibility: {mark(has_accessibility())}"
        )
        # Reflect the real launch-at-login state (may have changed via the menu).
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
        # Save what is typed first, then test in the background (network).
        value = self._ob_key.stringValue()
        if value and value.strip():
            credentials.set_api_key(value.strip())
            transcribe.reset_client()
        self._ob_keytest.setStringValue_("Testing…")
        self._keytest_result = None

        def _go() -> None:
            ok, msg = transcribe.test_api_key()
            # Read by tick_ (main thread) to update the label safely.
            self._keytest_result = msg

        threading.Thread(target=_go, daemon=True).start()

    def toggleLoginFromOnboarding_(self, sender):  # noqa: N802, ARG002
        want = sender.state() == NSControlStateValueOn
        ok, msg = set_login_enabled(want)
        if not ok:
            # Put the checkbox back to the real state and explain.
            sender.setState_(
                NSControlStateValueOn if login_enabled() else NSControlStateValueOff
            )
            self._alert("Launch at login", msg or "Failed.")
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

    # --- Small dialog boxes ---
    @objc.python_method
    def _prompt_secret(self, title: str, message: str, default: str):
        from AppKit import NSAlert

        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        field.setStringValue_(default)
        alert.setAccessoryView_(field)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        resp = alert.runModal()
        if resp == 1000:  # NSAlertFirstButtonReturn
            return field.stringValue()
        return None

    @objc.python_method
    def _prompt_text(self, title: str, message: str, default: str):
        from AppKit import NSAlert

        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
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
