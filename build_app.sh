#!/usr/bin/env bash
# Construit MistralSTT.app (PyInstaller), la signe en ad-hoc et prepare un zip
# distribuable. Aucun compte Apple Developer requis (app NON signee/notariee :
# au 1er lancement l'utilisateur fait clic droit > Ouvrir).
#
# Usage :
#   bash build_app.sh              # build + signature + zip (comportement par defaut,
#                                  # sur pour un clone/CI : NE touche PAS a /Applications)
#   bash build_app.sh --install    # idem PUIS installe dans /Applications (dev local :
#                                  # quitte l'app en cours, remplace, relance)
# Sortie :  dist/MistralSTT.app  et  dist/MistralSTT.zip
set -euo pipefail

cd "$(dirname "$0")"

APP="dist/MistralSTT.app"
INSTALLED="/Applications/MistralSTT.app"

# Drapeau --install (installation locale dans /Applications). Absent par defaut
# pour que le repo reste une "vraie app" telechargeable sans effet de bord.
INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        *) echo "Argument inconnu : $arg (utilise --install)"; exit 2 ;;
    esac
done

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

if [ "$INSTALL" = "1" ]; then
    echo "==> Installation dans /Applications (--install)"
    # Quitte l'instance en cours (LSUIElement : osascript peut echouer -> pkill).
    osascript -e 'quit app "MistralSTT"' 2>/dev/null || true
    pkill -f "$INSTALLED/Contents/MacOS/MistralSTT" 2>/dev/null || true
    # Attend l'arret effectif : remplacer un bundle encore en cours d'execution
    # peut echouer (fichiers verrouilles).
    for _ in $(seq 1 20); do
        pgrep -f "$INSTALLED/Contents/MacOS/MistralSTT" >/dev/null || break
        sleep 0.3
    done
    rm -rf "$INSTALLED" 2>/dev/null || true
    ditto "$APP" "$INSTALLED"
    echo "    installe : $INSTALLED"
    open "$INSTALLED" && echo "    relance."
fi

echo
echo "Termine :"
echo "  $APP"
echo "  dist/MistralSTT.zip   (a publier en GitHub Release)"
if [ "$INSTALL" = "1" ]; then
    echo "  $INSTALLED   (installe et relance)"
else
    echo
    echo "Astuce : 'bash build_app.sh --install' construit ET remplace l'app dans"
    echo "         /Applications en une seule commande (plus de copie manuelle)."
fi
echo
echo "Test local : ouvre $APP (clic droit > Ouvrir la 1re fois)."
