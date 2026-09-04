#!/usr/bin/env bash
# Construit MistralSTT.app en UNIVERSAL2 (Intel x86_64 + Apple Silicon arm64), la
# signe et prepare un zip distribuable.
#
# Pourquoi ce script (et pas target_arch="universal2" dans le .spec) :
# PyInstaller exige que chaque binaire embarque soit deja "fat" (universal2). Or
# numpy et pydantic_core ne publient PAS de wheels universal2 (x86_64 et arm64
# separes seulement) -> le build universal echouerait. On construit donc l'app
# DEUX fois (un venv arm64, un venv x86_64) puis on FUSIONNE les deux .app avec
# `lipo`. Resultat : une app native sur les deux architectures, sans Rosetta.
#
# Chaque venv est cree avec un interpreteur Python de l'architecture voulue,
# fourni par uv (`--python cpython-3.12-macos-<arch>`). uv installe alors les
# wheels de la bonne architecture. Le Python x86_64 s'execute via Rosetta 2
# pendant le build (transparent) ; l'app finale, elle, est native sur les 2 archs.
#
# Usage :  bash build_universal.sh
# Sortie :  dist/MistralSTT.app  (universal2)  et  dist/MistralSTT.zip
set -euo pipefail

cd "$(dirname "$0")"

APP="dist/MistralSTT.app"
ARM_DIST="dist/arm64"
X64_DIST="dist/x86_64"
PY_VER="3.12"
ARM_PY="cpython-${PY_VER}-macos-aarch64"
X64_PY="cpython-${PY_VER}-macos-x86_64"

echo "==> Verification des pre-requis"
if ! command -v uv >/dev/null 2>&1; then
    echo "ERREUR : 'uv' est introuvable." >&2
    echo "  Installe-le en natif arm64 :  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

echo "==> Installation des interpreteurs Python (arm64 + x86_64)"
uv python install "$ARM_PY" "$X64_PY"

# Le build x86_64 execute un Python x86_64 -> Rosetta 2 doit etre present.
if ! "$(uv python find "$X64_PY")" -c "import sys" 2>/dev/null; then
    echo "ERREUR : impossible d'executer le Python x86_64 (Rosetta 2 manquant ?)." >&2
    echo "  Installe Rosetta 2 :  softwareupdate --install-rosetta --agree-to-license" >&2
    exit 1
fi

echo "==> Nettoyage"
rm -rf build dist

# --- Build 1 : arm64 -------------------------------------------------------
echo "==> [arm64] Synchronisation des dependances"
UV_PROJECT_ENVIRONMENT=".venv" uv sync --python "$ARM_PY"
echo "==> [arm64] Build PyInstaller"
UV_PROJECT_ENVIRONMENT=".venv" uv run --no-sync pyinstaller MistralSTT.spec \
    --noconfirm --distpath "$ARM_DIST" --workpath "build/arm64"

# --- Build 2 : x86_64 (execute sous Rosetta 2) -----------------------------
echo "==> [x86_64] Synchronisation des dependances"
UV_PROJECT_ENVIRONMENT=".venv-x64" uv sync --python "$X64_PY"
echo "==> [x86_64] Build PyInstaller"
UV_PROJECT_ENVIRONMENT=".venv-x64" uv run --no-sync pyinstaller MistralSTT.spec \
    --noconfirm --distpath "$X64_DIST" --workpath "build/x86_64"

# --- Verification que les 2 builds sont bien d'architectures differentes ---
echo "==> Verification des architectures des 2 builds"
A_ARM="$(lipo -archs "$ARM_DIST/MistralSTT.app/Contents/MacOS/MistralSTT")"
A_X64="$(lipo -archs "$X64_DIST/MistralSTT.app/Contents/MacOS/MistralSTT")"
echo "    arm64 build : $A_ARM"
echo "    x86_64 build: $A_X64"
if [ "$A_ARM" = "$A_X64" ]; then
    echo "ERREUR : les deux builds ont la meme architecture ($A_ARM) -> fusion impossible." >&2
    exit 1
fi

# --- Fusion universal2 -----------------------------------------------------
echo "==> Fusion universal2 (lipo)"
python3 - "$ARM_DIST/MistralSTT.app" "$X64_DIST/MistralSTT.app" "$APP" <<'PY'
import os, shutil, subprocess, sys

arm_app, x64_app, out_app = sys.argv[1], sys.argv[2], sys.argv[3]

# Magies Mach-O (thin 32/64 bits, big/little endian, et fat).
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",   # 64/32 little-endian
    b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",   # 64/32 big-endian
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",   # fat
}

def is_macho(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4) in MACHO_MAGICS
    except OSError:
        return False

def already_fat(path):
    out = subprocess.run(["lipo", "-archs", path], capture_output=True, text=True)
    return len(out.stdout.split()) > 1

if os.path.exists(out_app):
    shutil.rmtree(out_app)

fused = copied = 0
for root, dirs, files in os.walk(arm_app):
    rel = os.path.relpath(root, arm_app)
    out_root = os.path.join(out_app, rel)
    os.makedirs(out_root, exist_ok=True)
    # Preserve les symlinks de repertoires (frameworks : Versions/Current, etc.).
    for d in list(dirs):
        src = os.path.join(root, d)
        if os.path.islink(src):
            dirs.remove(d)
            os.symlink(os.readlink(src), os.path.join(out_root, d))
    for name in files:
        arm = os.path.join(root, name)
        out = os.path.join(out_root, name)
        if os.path.islink(arm):
            os.symlink(os.readlink(arm), out)
            continue
        x64 = os.path.join(x64_app, rel, name)
        if is_macho(arm) and os.path.isfile(x64) and is_macho(x64) \
                and not already_fat(arm) and not already_fat(x64):
            subprocess.run(["lipo", "-create", arm, x64, "-output", out], check=True)
            shutil.copymode(arm, out)
            fused += 1
        else:
            # Deja fat (ex. PortAudio), ou binaire/data sans equivalent : on prend
            # la version arm64 telle quelle (les fat contiennent deja les 2 archs).
            if is_macho(arm) and not already_fat(arm) \
                    and not (os.path.isfile(x64) and is_macho(x64)):
                print(f"    WARN mach-o thin sans equivalent x86_64 : {os.path.join(rel, name)}")
            shutil.copy2(arm, out)
            copied += 1

# Fichiers presents uniquement cote x86_64 (rare) -> copies tels quels.
extra = 0
for root, _dirs, files in os.walk(x64_app):
    rel = os.path.relpath(root, x64_app)
    for name in files:
        out = os.path.join(out_app, rel, name)
        if not os.path.lexists(out):
            src = os.path.join(root, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if os.path.islink(src):
                os.symlink(os.readlink(src), out)
            else:
                shutil.copy2(src, out)
            extra += 1

print(f"    fusionnes: {fused}  copies: {copied}  extra-x64: {extra}")
PY

# --- Signature (reprise de build_app.sh) -----------------------------------
echo "==> Signature"
IDENTITY="MistralSTT Self-Signed"
KEYCHAIN="$HOME/Library/Keychains/mistral-signing.keychain-db"
KEYCHAIN_PASS="mistral-stt-local"   # meme valeur que setup_signing.sh
if security find-identity -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
    SIGN_ID="$IDENTITY"
    echo "    identite stable : $IDENTITY"
    if [ -f "$KEYCHAIN" ]; then
        security unlock-keychain -p "$KEYCHAIN_PASS" "$KEYCHAIN"
    fi
else
    SIGN_ID="-"
    echo "    ATTENTION : identite stable absente -> signature ad-hoc."
    echo "    Lance 'bash setup_signing.sh' pour des autorisations persistantes."
fi
codesign --deep --force -s "$SIGN_ID" "$APP"
codesign --verify --deep --strict "$APP" && echo "    signature OK"

echo "==> Architecture finale"
lipo -archs "$APP/Contents/MacOS/MistralSTT"

echo "==> Création du zip distribuable"
( cd dist && ditto -c -k --sequesterRsrc --keepParent "MistralSTT.app" "MistralSTT.zip" )

echo
echo "Termine :"
echo "  $APP   (universal2 : x86_64 + arm64)"
echo "  dist/MistralSTT.zip   (a publier en GitHub Release)"
echo
echo "Test local : ouvre $APP (clic droit > Ouvrir la 1re fois)."
echo "Nouveau binaire -> re-accorde Surveillance des entrees + Accessibilite + Micro."
