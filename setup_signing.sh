#!/usr/bin/env bash
# Cree (une seule fois) une identite de signature de code AUTO-SIGNEE et stable,
# pour que MistralSTT.app garde la MEME identite a chaque rebuild. Sans ca, la
# signature ad-hoc change d'empreinte (cdhash) a chaque build et macOS invalide
# les autorisations TCC (Surveillance des entrees, Accessibilite...) accordees.
#
# Aucun compte Apple Developer requis. L'identite vit dans un trousseau dedie
# (mot de passe connu) pour eviter toute boite de dialogue pendant le build.
#
# Usage :  bash setup_signing.sh        (idempotent)
set -euo pipefail

CN="MistralSTT Self-Signed"
KC="$HOME/Library/Keychains/mistral-signing.keychain-db"
KCPASS="mistral-stt-local"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if security find-identity -p codesigning 2>/dev/null | grep -q "$CN"; then
    echo "Identité '$CN' déjà présente — rien à faire."
    exit 0
fi

echo "==> Génération du certificat auto-signé (Code Signing)"
cat > "$WORK/cs.cnf" <<'CNF'
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = MistralSTT Self-Signed
[v3]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$WORK/key.pem" -out "$WORK/cert.pem" -days 3650 \
    -config "$WORK/cs.cnf" -extensions v3 >/dev/null 2>&1
# PBE/MAC "legacy" (SHA1-3DES) : le `security` d'Apple ne sait pas lire les
# PKCS12 a MAC moderne (echec "MAC verification failed").
openssl pkcs12 -export -out "$WORK/id.p12" \
    -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -passout pass:mistral \
    -macalg sha1 -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES >/dev/null 2>&1

echo "==> Trousseau dédié + import de l'identité"
security delete-keychain "$KC" 2>/dev/null || true
security create-keychain -p "$KCPASS" "$KC"
security set-keychain-settings "$KC"            # pas de verrouillage auto
security unlock-keychain -p "$KCPASS" "$KC"
# -A : accessible par tous les outils -> codesign ne demande pas l'autorisation.
security import "$WORK/id.p12" -k "$KC" -P mistral -T /usr/bin/codesign -A >/dev/null
security set-key-partition-list \
    -S apple-tool:,apple:,codesign: -s -k "$KCPASS" "$KC" >/dev/null 2>&1 || true

echo "==> Ajout du trousseau à la liste de recherche (les autres sont conservés)"
EXISTING=$(security list-keychains -d user | sed 's/[" ]//g' | tr '\n' ' ')
# shellcheck disable=SC2086
security list-keychains -d user -s "$KC" $EXISTING

echo
echo "==> Vérification (signature test)"
# Le certificat est auto-signe donc non "trusted" : il n'apparait PAS dans
# `find-identity -v` (identites valides), mais codesign sait quand meme signer
# avec. On valide donc par une vraie signature.
cp /bin/echo "$WORK/probe"
if codesign --force -s "$CN" "$WORK/probe" >/dev/null 2>&1; then
    echo "OK : signature avec '$CN' fonctionnelle."
else
    echo "ERREUR : impossible de signer avec '$CN'." >&2
    exit 1
fi
echo
echo "Tu peux maintenant (re)builder : bash build_app.sh"
