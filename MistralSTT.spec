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

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[],
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
