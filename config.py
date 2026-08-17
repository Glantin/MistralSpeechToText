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
# Duree max d'une prise (garde-fou memoire pour l'ecoute continue mains-libres :
# sinon le buffer croit sans borne). Au-dela, on cesse d'accumuler ; le debut
# est conserve et transcrit normalement. 10 min ~ 19 Mo en 16 kHz mono int16.
MAX_RECORD_SECONDS = 600

# --- Transcription --------------------------------------------------------
MISTRAL_MODEL = "voxtral-mini-latest"
# On NE fixe PAS la langue : auto-detection du franglais (melange FR/EN).
MISTRAL_LANGUAGE = None

# --- Reseau / timeouts HTTP ----------------------------------------------
# Sans timeout explicite, un changement/perte de reseau laisse la requete
# SUSPENDUE indefiniment : le thread de transcription reste bloque et la
# pastille se fige. On borne donc l'appel.
#   - CONNECT court : un reseau mort echoue vite -> on bascule en reprise ;
#   - READ genereux : couvre l'upload + la transcription d'un audio long.
HTTP_CONNECT_TIMEOUT = 10.0   # secondes
HTTP_READ_TIMEOUT = 180.0     # secondes

# --- Reprise (file persistante) ------------------------------------------
# Quand une transcription echoue (reseau), le WAV n'est PAS jete : il est
# conserve et re-tente en tache de fond selon ce backoff (secondes ; la
# derniere valeur est repetee ensuite), jusqu'a reussite. Ainsi un vocal
# long n'est jamais perdu, meme apres un redemarrage de l'app.
RETRY_BACKOFF_SECONDS = [2, 5, 15, 30, 60, 120, 300]
# Plafond de tentatives TRANSITOIRES : au-dela, on abandonne meme sur du reseau
# (garde-fou anti-boucle, en plus de l'age max). Une erreur PERMANENTE (400/401
# /422...) est, elle, abandonnee des la 1re tentative (cf. transcribe_queue).
RETRY_MAX_ATTEMPTS = 12
# Au-dela de cet age, un job en attente est purge (garde-fou anti-accumulation).
PENDING_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 jours

# --- Feedback -------------------------------------------------------------
# Sons systeme macOS joues aux transitions (None pour desactiver).
SOUND_START = "/System/Library/Sounds/Tink.aiff"
SOUND_DONE = "/System/Library/Sounds/Pop.aiff"

# --- Indicateur visuel ----------------------------------------------------
# Petite pastille flottante (NSPanel) en bas-centre de l'ecran :
#   rouge = enregistrement, ambre = transcription en cours, masquee = repos.
INDICATOR_ENABLED = True
# Deux cadences pour la boucle main-thread qui pilote l'UI (et rend la main au
# Ctrl+C en CLI) :
#   - RAPIDE quand la pastille est VISIBLE (enregistrement/transcription) : elle
#     est reaffirmee au premier plan souvent, donc reste au-dessus meme si une
#     app passe en plein ecran ;
#   - REPOS (idle) : bien plus lent, car il n'y a alors quasiment rien a faire.
#     On evite ainsi 10 reveils/s inutiles a vie (cout CPU permanent, sensible
#     sur Mac Intel). L'apparition de la pastille reste immediate : le coeur
#     reveille le main thread au changement d'etat (core.on_ui_state_change).
INDICATOR_TICK_SECONDS = 0.1        # cadence rapide (pastille visible)
INDICATOR_TICK_IDLE_SECONDS = 0.75  # cadence repos

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

# --- Stockage utilisateur (hors dossier projet) --------------------------
# On range les donnees runtime a cote de la cle API, dans Application Support
# (la .app packagee n'a pas de dossier projet inscriptible).
import credentials  # noqa: E402  (evite un cycle : credentials n'importe pas config)

# File de reprise : WAV en attente de transcription (+ sidecars .json).
PENDING_DIR = os.path.join(credentials.APP_SUPPORT_DIR, "pending")
# Dictionnaire de vocabulaire specifique (une entree par ligne, '#' = commentaire).
# Passe tel quel a l'API via context_bias : aucune requete/credit en plus.
VOCAB_FILE = os.path.join(credentials.APP_SUPPORT_DIR, "vocabulary.txt")
