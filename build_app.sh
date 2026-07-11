#!/usr/bin/env bash
# Construit MistralSTT.app (PyInstaller), la signe en ad-hoc et prepare un zip
# distribuable. Aucun compte Apple Developer requis (app NON signee/notariee :
# au 1er lancement l'utilisateur fait clic droit > Ouvrir).
#
# Usage :  bash build_app.sh
# Sortie :  dist/MistralSTT.app  et  dist/MistralSTT.zip
set -euo pipefail

cd "$(dirname "$0")"

APP="dist/MistralSTT.app"

echo "==> Nettoyage"
rm -rf build dist

echo "==> Synchronisation des dependances (dont PyInstaller)"
uv sync

echo "==> Build PyInstaller"
uv run pyinstaller MistralSTT.spec --noconfirm

echo "==> Signature"
# Identite auto-signee STABLE si disponible (cf. setup_signing.sh) : l'app garde
# la meme identite a chaque rebuild -> les autorisations macOS (TCC) persistent.
# Sinon, repli ad-hoc (-) : fonctionne mais l'empreinte change a chaque build et
# les autorisations sont a re-accorder.
IDENTITY="MistralSTT Self-Signed"
KEYCHAIN="$HOME/Library/Keychains/mistral-signing.keychain-db"
KEYCHAIN_PASS="mistral-stt-local"   # meme valeur que setup_signing.sh
if security find-identity -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
    SIGN_ID="$IDENTITY"
    echo "    identite stable : $IDENTITY"
    # Le trousseau dedie se reverrouille au redemarrage de la machine. Verrouille,
    # il laisse l'identite visible dans find-identity mais rend la cle privee
    # inaccessible -> codesign echoue avec errSecInternalComponent.
    if [ -f "$KEYCHAIN" ]; then
        security unlock-keychain -p "$KEYCHAIN_PASS" "$KEYCHAIN"
    fi
else
    SIGN_ID="-"
    echo "    ATTENTION : identite stable absente -> signature ad-hoc."
    echo "    Lance 'bash setup_signing.sh' pour des autorisations persistantes."
fi
# --deep signe aussi les dylibs embarquees (PortAudio, pydantic_core...).
codesign --deep --force -s "$SIGN_ID" "$APP"
codesign --verify --deep --strict "$APP" && echo "    signature OK"

echo "==> Création du zip distribuable"
( cd dist && ditto -c -k --sequesterRsrc --keepParent "MistralSTT.app" "MistralSTT.zip" )

echo
echo "Termine :"
echo "  $APP"
echo "  dist/MistralSTT.zip   (a publier en GitHub Release)"
echo
echo "Test local : ouvre $APP (clic droit > Ouvrir la 1re fois)."
