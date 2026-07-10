# -*- mode: python ; coding: utf-8 -*-
"""Spec PyInstaller pour MistralSTT.app (app macOS en barre de menus).

Build :
    bash build_app.sh          # (recommande : sync + build + signature ad-hoc)
ou directement :
    uv run pyinstaller MistralSTT.spec --noconfirm

L'app embarque son propre Python : l'utilisateur final n'installe rien, et les
autorisations macOS se collent au bundle (chemin stable) -> elles survivent aux
mises a jour de Python.
"""

import os

VERSION = "0.1.0"
_icon = "assets/AppIcon.icns"
ICON = _icon if os.path.exists(_icon) else None

# Bundle CA d'entreprise embarque (repli si SSL_CERT_FILE absent) : transcribe.py
# le cherche dans sys._MEIPASS/certs/. Ignore s'il n'existe pas.
_DATAS = []
if os.path.exists("certs/cato-bundle.pem"):
    _DATAS.append(("certs/cato-bundle.pem", "certs"))

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=_DATAS,
    # Modules locaux + paquets a imports dynamiques : forces par securite
    # (PyInstaller suit deja la plupart des imports statiques de app.py).
    hiddenimports=[
        "config",
        "credentials",
        "transcribe",
        "audio",
        "inserter",
        "indicator",
        "history",
        "mistral_stt",
        "sounddevice",
        "numpy",
        "mistralai",
        "httpx",
        # truststore : valide le TLS via le trousseau macOS (proxy d'entreprise).
        # Le sous-module _macos est charge dynamiquement -> a forcer.
        "truststore",
        "truststore._macos",
        "ServiceManagement",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MistralSTT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # pas de fenetre terminal
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MistralSTT",
)

app = BUNDLE(
    coll,
    name="MistralSTT.app",
    icon=ICON,
    bundle_identifier="com.glantin.mistral-stt",
    version=VERSION,
    info_plist={
        "CFBundleName": "MistralSTT",
        "CFBundleDisplayName": "MistralSTT",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        # App en barre de menus : pas d'icone dans le Dock.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        # Texte du pop-up d'autorisation micro.
        "NSMicrophoneUsageDescription": (
            "MistralSTT enregistre votre voix pour la transcrire en texte."
        ),
    },
)
