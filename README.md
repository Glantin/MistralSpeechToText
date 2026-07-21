# MistralSpeechToText

A tiny, local, "Wispr Flow light" style speech-to-text tool for macOS. Hold a
key, speak, release, and the text is typed in wherever your cursor is, in any
app. Transcription runs through the Mistral Voxtral API.

It does not lock you to a single language. No language is forced on the
transcriber, so it auto-detects what you speak and handles many languages. You
can even switch languages mid sentence (for example "franglais", French and
English mixed) and it still works.

## What it does

- **Hold the RIGHT Option key (⌥)**, speak, then **release**. The text is
  inserted at your cursor.
- **Right Option + Space** starts **hands free continuous listening**. Press
  **Right Option** again to stop and insert.
- **Esc** while recording **cancels** the take (nothing is transcribed or
  pasted).
- The **left Option key stays free** so you can still type accents (é, è, ç...).

A small **floating dot** appears at the bottom center of your screen while in
use: **red** = recording, **amber** = transcribing, **hidden** = idle. On
cancel it flashes red and fades out. It stays visible across all Spaces,
including over full-screen apps.

## Install the app (recommended, ~5 minutes)

Download the prebuilt macOS app — no terminal, no Python, no `uv`.

1. **Download** `MistralSTT.zip` from the
   [Releases](https://github.com/Glantin/MistralSpeechToText/releases) page, then
   double-click it to unzip.
2. **Open** `MistralSTT.app`. The first time, macOS shows an "unidentified
   developer" warning (the app isn't notarized yet): **right-click the app →
   Open → Open**.
3. The app **offers to move itself into Applications** — accept (recommended).
   It then lives in the **menu bar** (🎙), not the Dock.
4. A **setup window** opens. Do these three things:
   - **API key** — paste your Mistral key (see
     [Get a Mistral API key](#get-a-mistral-api-key-free)), click **Enregistrer**,
     then **Tester la clé** to confirm.
   - **Permissions** — click each **Autoriser** button (it opens the right
     System Settings pane), turn the switch on, and come back: the line turns ✅.
     The three are Microphone, Input Monitoring (to detect Right Option) and
     Accessibility (to paste).
   - **Launch at login** (optional) — tick the box to start it automatically.
5. Hold **Right Option**, speak, release — the text appears at your cursor.

From the menu-bar icon you can re-enter the API key, reopen the setup window, and
toggle launch at login. If a transcription fails (bad key, network, corporate
TLS proxy), a macOS **notification** tells you why.

> If macOS refuses to open the app even via right-click → Open, clear the
> download quarantine once: `xattr -dr com.apple.quarantine /Applications/MistralSTT.app`

> **Why the app?** It bundles its own Python, so the macOS permissions stick to
> the app itself. They no longer break when your system Python changes — which is
> the main pain of the from-source setup below.

---

# Run from source (developer)

The sections below are for running from source or hacking on the project. If you
just want to use the tool, the app above is all you need.

## Requirements

- **macOS**
- **uv** (the Python package manager). Install it from
  https://docs.astral.sh/uv/ if you do not have it yet.
- A **free Mistral API key** (see the next section).

## Get a Mistral API key (free)

The transcription uses Mistral's Voxtral model. You need an API key, which is
free to create:

1. Go to https://console.mistral.ai/ and create an account (or sign in).
2. Open the **API Keys** section and click **Create new key**.
3. Copy the key. You will paste it into a `.env` file in step 3 below.

Keep this key private. It is like a password for your account.

## Install

1. Clone the project and enter the folder:

   ```bash
   git clone https://github.com/Glantin/MistralSpeechToText.git
   cd MistralSpeechToText
   ```

2. Install the dependencies (this also creates the `.venv` folder):

   ```bash
   uv sync
   ```

3. Create your `.env` file from the example, then paste your key into it:

   ```bash
   cp .env.example .env
   ```

   Open `.env` in any editor and fill in the line so it reads:

   ```
   MISTRAL_API_KEY=your_key_here
   ```

## Grant macOS permissions (one time)

The app needs three permissions. Without them, either the key will not trigger
recording or the text will not paste.

Open **System Settings → Privacy & Security**, then grant these to **the app
that launches Python** (your terminal, for example Terminal, iTerm, or
Ghostty):

1. **Microphone** (you are usually prompted automatically on the first
   recording).
2. **Input Monitoring** (so it can detect the Right Option key).
3. **Accessibility** (so it can paste the text with Cmd+V).

After ticking these, restart the app.

## Run

```bash
uv run python mistral_stt.py
```

Leave the terminal window open (the daemon runs inside it). Press `Ctrl+C` to
quit.

That is it. Put your cursor in any text field, hold Right Option, speak,
release, and watch the text appear.

## Build the app (.app)

First, create a **stable signing identity** once (no Apple Developer account
needed):

```bash
bash setup_signing.sh
```

This generates a self-signed code-signing certificate in a dedicated keychain.
Without it the build falls back to an *ad-hoc* signature, whose fingerprint
changes on every rebuild — which makes macOS **forget the permissions** you
granted (Input Monitoring, Accessibility…) each time. With the stable identity,
the app keeps the same identity across rebuilds and permissions persist.

Then build the app:

```bash
bash build_app.sh
```

This syncs dependencies, runs PyInstaller (`MistralSTT.spec`), signs the bundle
(with the stable identity if present), and writes `dist/MistralSTT.app` and
`dist/MistralSTT.zip`. The app embeds its own Python, so the end user needs
nothing installed. Inside the app, the API key is stored in
`~/Library/Application Support/MistralSTT/.env` (entered via the onboarding
window) instead of a project `.env`.

> If permissions still won't stick after a rebuild, the macOS privacy database
> may hold stale entries from older builds. Reset them with
> `tccutil reset All com.glantin.mistral-stt` (the app must be installed in
> `/Applications`), then grant again.

Notes:

- The build targets **your machine's architecture** (Intel or Apple Silicon).
  An **Intel (x86_64) build runs fine on Apple Silicon via Rosetta 2**, which
  macOS offers to install automatically the first time you open the app. For a
  lightweight menu-bar app the performance difference is imperceptible, so a
  single Intel build already covers both platforms — which is how the published
  releases are shipped.
- If you ever want a **native `universal2` binary** (no Rosetta): it requires a
  `universal2` Python from python.org (not the single-arch Python that `uv`
  provides) and either `universal2` wheels or manually fused x86_64 + arm64
  wheels for every native dependency (numpy, pydantic-core, PortAudio, pyobjc…).
  Then add `target_arch="universal2"` to the `EXE(...)` in `MistralSTT.spec`.
- The app is **not notarized**. With an Apple Developer ID you can sign and
  notarize it later (no code changes needed) to remove the first-launch warning.

## Transcription history

Every successful transcription is logged to `history.jsonl` (one JSON line per
entry: `ts`, `text`, `chars`) and also **printed in your terminal**. This is
handy when a paste is lost because no text field had focus: the text is still
recoverable.

```bash
uv run python history.py            # show the last 20 entries
uv run python history.py -n 5       # show the last 5
uv run python history.py --copy     # copy the last one to the clipboard
uv run python history.py --last     # alias of --copy
```

### Clipboard safety net

By default (`KEEP_LAST_IN_CLIPBOARD = True` in `config.py`), each transcription
**stays** in your clipboard (the previous clipboard content is not restored).
Two benefits:

- a plain `Cmd+V` re-pastes the last dictation if the automatic paste was lost;
- a clipboard history manager (for example **Raycast**) automatically captures
  all your dictations.

Set `KEEP_LAST_IN_CLIPBOARD = False` to restore the old clipboard content after
each paste instead.

## Cost

The model is `voxtral-mini-latest`, which costs about **0.003 $ per minute** of
audio. In normal personal use that is a few cents per day.

## Customize the trigger key

Everything lives in `config.py`:

- `TRIGGER_KEYCODE` sets the trigger key (default `61`, Right Option). A common
  fallback is Right Control (`62`, with `TRIGGER_FLAG_MASK = 0x00040000`).
- `INDICATOR_ENABLED` turns the floating dot on or off.
- `INDICATOR_TICK_SECONDS` controls how responsive the dot is (lower it if the
  dot is slow to appear).

## Run at login (optional, background)

To have the app start by itself when you log in, in the background and with no
terminal window (you just press Right Option to dictate), use the included
LaunchAgent.

```bash
cp launchd/com.mistral-stt.plist ~/Library/LaunchAgents/com.mistral-stt.plist
launchctl load -w ~/Library/LaunchAgents/com.mistral-stt.plist     # start
launchctl unload -w ~/Library/LaunchAgents/com.mistral-stt.plist   # stop (uninstall)
```

The `.plist` launches the venv's Python directly (`.venv/bin/python
mistral_stt.py`), which is more reliable under launchd than `uv run`. Logs go to
`mistral_stt.log`.

> **Important: edit the paths first.** The `.plist` ships with placeholder paths
> (`/Users/YOUR_NAME/path/to/mistral-speech-to-text`). They will not match your
> setup. Open `launchd/com.mistral-stt.plist` and replace every path with your
> own. To find the right values, run `pwd` inside the project folder (that is
> your `WorkingDirectory`), and use that same folder plus `/.venv/bin/python`
> for the Python path.

> **Important: permissions move to the Python binary.** When launched via
> launchd, the process responsible for permissions is no longer your terminal
> but the venv's Python binary. You must grant the same three permissions
> (**Microphone**, **Accessibility**, **Input Monitoring**) to **that binary**.
> Input Monitoring is added by hand: in System Settings click the `+` button,
> then in the file picker press `Cmd+Shift+G` and paste the path to
> `.venv/bin/python`. After ticking the permissions, reload the agent
> (`unload` then `load`). If anything misbehaves, check `mistral_stt.log`.

## Troubleshooting

- **Nothing happens when I press the key** → Input Monitoring is missing. The
  log will show `Impossible de creer l'event tap` ("could not create the event
  tap"). Grant Input Monitoring to your terminal (or to the Python binary if
  you use the LaunchAgent), then restart.
- **Recording works but nothing gets pasted** → Accessibility is missing. Grant
  it, then restart.
- **No audio is captured** → Microphone is missing, or the wrong input device
  is selected. The app prints the detected microphones on startup.
- **`MISTRAL_API_KEY manquante`** → your `.env` file is missing or the key line
  is empty. Re-check step 3 of Install.
- **Transcription fails with `CERTIFICATE_VERIFY_FAILED` / `self-signed
  certificate in certificate chain`** → you are on a managed network behind a
  TLS-inspecting proxy. See [Behind a corporate proxy](#behind-a-corporate-proxy-ssl-errors)
  below.
- **The dot does not appear (multi-monitor)** → the indicator now anchors itself
  just above the Dock, on the screen that has the Dock. If you hid the Dock or
  want a different spot, tweak `_MARGIN_BOTTOM` / `_SIZE` in `indicator.py`.

When in doubt, run in debug mode for more detail:

```bash
MISTRAL_STT_DEBUG=1 uv run python mistral_stt.py
```

The log file `mistral_stt.log` (when running via the LaunchAgent) contains the
same output. Tip: under launchd, Python output is buffered — add
`PYTHONUNBUFFERED=1` to the agent's `EnvironmentVariables` to see logs live.

### Behind a corporate proxy (SSL errors)

On a work or managed Mac, transcription may fail with:

```
erreur transcription: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify
failed: self-signed certificate in certificate chain
```

This means your company runs a **TLS-inspecting proxy** (Zscaler, Netskope, Cato
Networks, a corporate firewall...) that re-signs HTTPS traffic with its own root
certificate. Your browser and `curl` trust it because IT installed it in the
macOS Keychain, but Python ships its own certificate list (`certifi`) which does
not include it — so Python refuses the connection.

The fix is to give Python a CA bundle that **also** contains your company's root
certificate.

1. **Identify the proxy's root certificate.** Inspect the chain the proxy
   presents for the API:

   ```bash
   echo | openssl s_client -connect api.mistral.ai:443 -servername api.mistral.ai \
     2>/dev/null | openssl x509 -noout -issuer
   ```

   The issuer name (e.g. `Cato-Networks-...`, `Zscaler Root CA`...) points to the
   root CA your IT installed. Find its exact label in the System keychain:

   ```bash
   security find-certificate -a /Library/Keychains/System.keychain \
     | grep -i "<your vendor>"
   ```

2. **Build a combined CA bundle** (certifi + your corporate root) inside the
   project:

   ```bash
   mkdir -p certs
   .venv/bin/python -c "import certifi, shutil; shutil.copy(certifi.where(), 'certs/corp-bundle.pem')"
   security find-certificate -a -c "Cato Networks Root CA" -p /Library/Keychains/System.keychain \
     >> certs/corp-bundle.pem
   ```

   Replace `"Cato Networks Root CA"` with the exact certificate name you found in
   step 1. The `certs/` folder is git-ignored, so your bundle stays local.

3. **Point Python at the bundle** with the `SSL_CERT_FILE` environment variable:

   - Running from a terminal:

     ```bash
     SSL_CERT_FILE="$PWD/certs/corp-bundle.pem" uv run python mistral_stt.py
     ```

   - Running via the LaunchAgent: add it to the `EnvironmentVariables` block of
     `~/Library/LaunchAgents/com.mistral-stt.plist`, then reload the agent
     (`unload` then `load`):

     ```xml
     <key>EnvironmentVariables</key>
     <dict>
         <key>SSL_CERT_FILE</key>
         <string>/Users/YOUR_NAME/path/to/MistralSpeechToText/certs/corp-bundle.pem</string>
     </dict>
     ```

## Test individual modules

```bash
uv run python audio.py             # record 3 s and write a WAV
uv run python transcribe.py x.wav  # transcribe a WAV file
uv run python inserter.py          # paste a test string at the cursor
uv run python history.py           # print the history
```

## Privacy

Audio is recorded locally to a temporary WAV file, sent to the Mistral API for
transcription, and the temporary file is deleted right after. Your API key
lives only in `.env`, which is git-ignored and never committed. Your
transcription history (`history.jsonl`) and logs (`mistral_stt.log`) also stay
local and git-ignored.

## License

MIT. See [LICENSE](LICENSE).
