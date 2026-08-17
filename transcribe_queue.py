"""File de transcription PERSISTANTE avec reprise automatique.

Pourquoi ce module ? Sur reseau faible/changeant, une transcription pouvait
echouer et l'audio etait perdu (WAV temporaire supprime, aucune reprise). Ici :

  - l'enregistrement (mic) et la transcription (reseau) vivent sur DEUX threads
    distincts : un appel reseau lent/bloque ne gele plus l'enregistrement ;
  - chaque prise est ecrite sur DISQUE (PENDING_DIR) avant transcription, donc
    conservee tant qu'aucun transcript n'a ete obtenu — meme apres un redemarrage
    de l'app (recover_pending) ;
  - en cas d'echec, on RE-TENTE en tache de fond selon un backoff plafonne,
    jusqu'a reussite.

Livraison (callbacks injectes par mistral_stt.py) :
  - succes du 1er coup (jamais differe)  -> deliver_immediate (colle au curseur) ;
  - succes apres un echec (differe)       -> deliver_deferred (presse-papier +
                                              pastille verte + notification).

CONTRAINTE THREADING : un seul worker consomme la file ; l'etat partage est
protege par un Condition. Les callbacks sont invoques HORS verrou.
"""

import json
import os
import shutil
import threading
import time
import uuid

import config
import history
import transcribe as _transcribe

# --- Etat partage ---------------------------------------------------------
_cond = threading.Condition(threading.RLock())
# jobid -> {"wav_path", "created_ts", "attempts", "next_try_ts", "ever_deferred"}
_jobs: dict[str, dict] = {}
_active_jobid: str | None = None  # job en cours de tentative (None au repos)
_started = False

# --- Callbacks (branches par mistral_stt.py) ------------------------------
# Invoques HORS verrou. Chacun peut rester None (ex: contexte de test).
on_state_change = None   # () -> None : recalcul de la pastille
deliver_immediate = None  # (text: str) -> None : collage au curseur
deliver_deferred = None   # (text: str) -> None : presse-papier + flash + notif
on_error = None           # (message: str) -> None : 1re defaillance TRANSITOIRE (option)
on_permanent_error = None  # (message: str) -> None : job ABANDONNE (erreur permanente)


def _notify_state() -> None:
    cb = on_state_change
    if cb is not None:
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass


# --- Compteurs lus par la dERIVATION de la pastille (main thread) ---------
def active_count() -> int:
    """1 si une tentative de transcription est en cours, sinon 0 (-> ambre)."""
    with _cond:
        return 1 if _active_jobid is not None else 0


def pending_count() -> int:
    """Nombre de jobs en attente d'une (nouvelle) tentative (-> bleu 'retrying').

    Exclut le job actuellement en cours de tentative (compte, lui, en 'ambre').
    """
    with _cond:
        n = len(_jobs)
        if _active_jobid is not None:
            n -= 1
        return n


# --- Persistance (sidecar JSON a cote du WAV) -----------------------------
def _sidecar_path(jobid: str) -> str:
    return os.path.join(config.PENDING_DIR, f"{jobid}.json")


def _wav_path(jobid: str) -> str:
    return os.path.join(config.PENDING_DIR, f"{jobid}.wav")


def _write_sidecar(jobid: str, meta: dict) -> None:
    try:
        data = {
            "created_ts": meta["created_ts"],
            "attempts": meta["attempts"],
            "next_try_ts": meta["next_try_ts"],
            "ever_deferred": meta["ever_deferred"],
        }
        with open(_sidecar_path(jobid), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _remove_job(jobid: str) -> None:
    """Retire le job de la file ET du disque (WAV + sidecar)."""
    meta = _jobs.pop(jobid, None)
    wav = meta["wav_path"] if meta else _wav_path(jobid)
    for p in (wav, _sidecar_path(jobid)):
        try:
            os.remove(p)
        except OSError:
            pass


def _backoff_for(attempts: int) -> float:
    """Delai (s) avant la prochaine tentative ; la derniere valeur est repetee."""
    schedule = config.RETRY_BACKOFF_SECONDS
    if not schedule:
        return 30.0
    idx = min(max(attempts - 1, 0), len(schedule) - 1)
    return float(schedule[idx])


# --- API publique ---------------------------------------------------------
def enqueue(wav_path: str) -> None:
    """Inscrit une nouvelle prise : deplace le WAV dans PENDING_DIR et reveille
    le worker (transcription due immediatement)."""
    if not wav_path or not os.path.exists(wav_path):
        return
    os.makedirs(config.PENDING_DIR, exist_ok=True)
    jobid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    dest = _wav_path(jobid)
    try:
        shutil.move(wav_path, dest)
    except OSError:
        # Le move a echoue (ex: volumes differents) : on copie en dernier recours.
        try:
            shutil.copyfile(wav_path, dest)
            os.remove(wav_path)
        except OSError:
            return
    now = time.time()
    meta = {
        "wav_path": dest,
        "created_ts": now,
        "attempts": 0,
        "next_try_ts": now,  # due immediatement
        "ever_deferred": False,
    }
    with _cond:
        _jobs[jobid] = meta
        _write_sidecar(jobid, meta)
        _cond.notify_all()
    _notify_state()


def recover_pending() -> int:
    """ Re-inscrit les WAV presents dans PENDING_DIR (reprise apres redemarrage).

    Un job recupere est considere 'differe' (ever_deferred) : sa livraison passera
    par le presse-papier, jamais par un collage au curseur (le contexte a change).
    Purge au passage les prises trop vieilles. Renvoie le nombre repris.
    """
    d = config.PENDING_DIR
    if not os.path.isdir(d):
        return 0
    now = time.time()
    recovered = 0
    for name in os.listdir(d):
        if not name.endswith(".wav"):
            continue
        jobid = name[:-4]
        wav = os.path.join(d, name)
        if jobid in _jobs:
            continue
        # Charge le sidecar s'il existe, sinon valeurs par defaut.
        meta = {
            "wav_path": wav,
            "created_ts": now,
            "attempts": 0,
            "next_try_ts": now,
            "ever_deferred": True,
        }
        try:
            with open(_sidecar_path(jobid), encoding="utf-8") as f:
                saved = json.load(f)
            meta["created_ts"] = saved.get("created_ts", now)
            meta["attempts"] = saved.get("attempts", 0)
            meta["ever_deferred"] = True  # survivance = differe
            meta["next_try_ts"] = now  # re-tente tout de suite au demarrage
        except (OSError, ValueError):
            pass
        # Purge des prises trop vieilles.
        if now - meta["created_ts"] > config.PENDING_MAX_AGE_SECONDS:
            for p in (wav, _sidecar_path(jobid)):
                try:
                    os.remove(p)
                except OSError:
                    pass
            continue
        with _cond:
            _jobs[jobid] = meta
        recovered += 1
    if recovered:
        with _cond:
            _cond.notify_all()
        _notify_state()
    return recovered


def start() -> threading.Thread | None:
    """Demarre le worker de transcription (idempotent)."""
    global _started
    with _cond:
        if _started:
            return None
        _started = True
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# --- Worker ---------------------------------------------------------------
def _pick_due_job(now: float) -> tuple[str | None, float | None]:
    """Renvoie (jobid du a transcrire, ou None) et (prochaine echeance, ou None).

    Appele SOUS verrou. Purge d'abord les jobs trop vieux.
    """
    expired = [
        jid
        for jid, m in _jobs.items()
        if now - m["created_ts"] > config.PENDING_MAX_AGE_SECONDS
    ]
    for jid in expired:
        _remove_job(jid)

    due_jid = None
    due_ts = None
    next_ts = None
    for jid, m in _jobs.items():
        t = m["next_try_ts"]
        if t <= now and (due_ts is None or t < due_ts):
            due_jid, due_ts = jid, t
        if next_ts is None or t < next_ts:
            next_ts = t
    return due_jid, next_ts


def _run() -> None:
    global _active_jobid
    while True:
        with _cond:
            now = time.time()
            jobid, next_ts = _pick_due_job(now)
            if jobid is None:
                # Rien de du : on dort jusqu'a la prochaine echeance (ou indefiniment
                # s'il n'y a aucun job), reveillable par enqueue()/recover_pending().
                wait = None if next_ts is None else max(0.05, next_ts - now)
                _cond.wait(timeout=wait)
                continue
            meta = _jobs[jobid]
            wav = meta["wav_path"]
            _active_jobid = jobid
        _notify_state()  # -> pastille ambre (transcription en cours)

        # Appel reseau HORS verrou (bloquant, borne par le timeout HTTP).
        text = None
        ok = False
        err_msg = None
        retriable = True
        try:
            text = _transcribe.transcribe(wav)
            ok = True
        except Exception as exc:  # noqa: BLE001
            err_msg = _transcribe.classify_error(exc)[1]
            retriable = _transcribe.is_retriable(exc)

        first_failure = False
        gave_up = False
        with _cond:
            _active_jobid = None
            if ok:
                ever_deferred = _jobs.get(jobid, {}).get("ever_deferred", False)
                _remove_job(jobid)
            else:
                m = _jobs.get(jobid)
                if m is not None:
                    m["attempts"] += 1
                    first_failure = m["attempts"] == 1
                    m["ever_deferred"] = True
                    # Abandon si l'erreur est PERMANENTE (400/401/422... : re-tenter
                    # ne peut pas aider et laisserait la pastille bleue a vie) ou si
                    # le plafond de tentatives transitoires est atteint. Sinon on
                    # re-planifie avec backoff.
                    if not retriable or m["attempts"] >= config.RETRY_MAX_ATTEMPTS:
                        gave_up = True
                        _remove_job(jobid)
                    else:
                        m["next_try_ts"] = time.time() + _backoff_for(m["attempts"])
                        _write_sidecar(jobid, m)
        _notify_state()  # -> idle / bleu (selon jobs restants)

        if ok:
            if text:
                # Journalise AVANT livraison : la trace existe quoi qu'il arrive.
                try:
                    history.append(text)
                except Exception:  # noqa: BLE001
                    pass
                cb = deliver_deferred if ever_deferred else deliver_immediate
                if cb is not None:
                    try:
                        cb(text)
                    except Exception:  # noqa: BLE001
                        pass
            # texte vide : rien a livrer (job deja retire).
        elif gave_up:
            # Echec DEFINITIF (job retire) : on le signale a chaque fois (c'est un
            # abandon, pas une simple attente) via on_permanent_error -> flash
            # d'erreur + notification. Repli sur on_error si non branche.
            cb = on_permanent_error or on_error
            if err_msg and cb is not None:
                try:
                    cb(err_msg)
                except Exception:  # noqa: BLE001
                    pass
        elif first_failure and err_msg and on_error is not None:
            # 1re defaillance TRANSITOIRE seulement (pas chaque reprise, sinon spam).
            # La reprise continue en fond ; la pastille reste bleue.
            try:
                on_error(err_msg)
            except Exception:  # noqa: BLE001
                pass
