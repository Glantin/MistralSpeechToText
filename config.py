"""Constantes partagees de MistralSpeechToText.

La touche de declenchement est isolee ici pour pouvoir la rebasculer
facilement (Option droite par defaut ; replis possibles plus bas).
"""

import os

# --- Touche de declenchement ---------------------------------------------
# Keycodes macOS (virtual key codes) des modificateurs gauche/droite.
#   58 = Option gauche   61 = Option droite
#   59 = Control gauche  62 = Control droit
#   56 = Shift gauche    60 = Shift droit
# On declenche sur l'Option DROITE : l'Option gauche reste libre pour les
# accents (e, e, c...).
TRIGGER_KEYCODE = 61  # Option droite

# Masque de flag associe a la famille Option (kCGEventFlagMaskAlternate).
# Sert a distinguer un appui (down) d'un relachement (up) sur flagsChanged.
TRIGGER_FLAG_MASK = 0x00080000  # NSEventModifierFlagOption

# Si tu veux rebasculer sur Control droit :
#   TRIGGER_KEYCODE = 62
#   TRIGGER_FLAG_MASK = 0x00040000  # kCGEventFlagMaskControl

# Touche espace (pour basculer en ecoute continue pendant le maintien).
SPACE_KEYCODE = 49

# Touche Echap : annule l'enregistrement en cours (jette la prise, pas de
# transcription ni de collage).
ESCAPE_KEYCODE = 53

# --- Audio ----------------------------------------------------------------
SAMPLE_RATE = 16000  # 16 kHz, suffisant et leger pour la STT
CHANNELS = 1

# --- Transcription --------------------------------------------------------
MISTRAL_MODEL = "voxtral-mini-latest"
# On NE fixe PAS la langue : auto-detection du franglais (melange FR/EN).
MISTRAL_LANGUAGE = None

# --- Feedback -------------------------------------------------------------
# Sons systeme macOS joues aux transitions (None pour desactiver).
SOUND_START = "/System/Library/Sounds/Tink.aiff"
SOUND_DONE = "/System/Library/Sounds/Pop.aiff"

# --- Indicateur visuel ----------------------------------------------------
# Petite pastille flottante (NSPanel) en bas-centre de l'ecran :
#   rouge = enregistrement, ambre = transcription en cours, masquee = repos.
INDICATOR_ENABLED = True
# Periode de la boucle main-thread qui pilote l'UI ET rend la main au Ctrl+C.
# Plus bas = pastille plus reactive, un peu plus de reveils CPU.
INDICATOR_TICK_SECONDS = 0.1

# --- Presse-papier --------------------------------------------------------
# Filet de securite : si True (defaut), la derniere transcription RESTE dans le
# presse-papier (l'ancien contenu n'est PAS restaure). Ainsi chaque dictee est
# captee par un gestionnaire d'historique de presse-papier (ex: Raycast) et
# reste recollable a la main (Cmd+V) si le collage automatique s'est perdu.
# Mets False pour revenir a l'ancien comportement (restauration du presse-papier).
KEEP_LAST_IN_CLIPBOARD = True

# --- Historique -----------------------------------------------------------
# Chaque transcription reussie est journalisee ici (une ligne JSON).
HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "history.jsonl"
)
# Nombre d'entrees affichees par defaut par `history.py`.
HISTORY_DEFAULT_N = 20
