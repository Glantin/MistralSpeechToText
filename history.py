"""Historique des transcriptions MistralSpeechToText.

Chaque transcription reussie est journalisee dans history.jsonl (une ligne
JSON par entree). Utile quand le collage se perd (aucun champ texte focalise) :
on peut retrouver le texte et le recopier.

Usage CLI :
    uv run python history.py            # affiche les N dernieres entrees
    uv run python history.py -n 5       # les 5 dernieres
    uv run python history.py --copy     # recopie la derniere dans le presse-papier
    uv run python history.py --last     # alias de --copy
"""

import argparse
import json
from datetime import datetime

import config


def append(text: str) -> None:
    """Ajoute une entree a l'historique. Ne leve jamais (best-effort)."""
    if not text:
        return
    entry = {
        "ts": datetime.now().astimezone().isoformat(),
        "text": text,
        "chars": len(text),
    }
    try:
        with open(config.HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # L'historique ne doit jamais casser le flux de dictee.
        pass


def read(n: int) -> list[dict]:
    """Retourne les `n` dernieres entrees valides (les plus recentes en dernier)."""
    try:
        with open(config.HISTORY_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # ligne corrompue : on l'ignore
    return entries[-n:] if n > 0 else entries


def _fmt_ts(iso: str) -> str:
    """Rend un timestamp ISO lisible (sans planter si format inattendu)."""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso or "?"


def _print_entries(entries: list[dict]) -> None:
    if not entries:
        print("(historique vide)")
        return
    for e in entries:
        ts = _fmt_ts(e.get("ts", ""))
        text = e.get("text", "")
        print(f"[{ts}] {text}")


def _copy_last() -> None:
    entries = read(1)
    if not entries:
        print("(historique vide : rien a copier)")
        return
    text = entries[-1].get("text", "")
    if not text:
        print("(derniere entree vide : rien a copier)")
        return
    # Import tardif : evite de charger AppKit quand on ne fait qu'afficher.
    from inserter import set_clipboard

    set_clipboard(text)
    print(f"copie dans le presse-papier ({len(text)} caracteres) : {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Historique des transcriptions MistralSpeechToText.")
    parser.add_argument(
        "-n",
        type=int,
        default=config.HISTORY_DEFAULT_N,
        help=f"nombre d'entrees a afficher (defaut {config.HISTORY_DEFAULT_N})",
    )
    parser.add_argument(
        "--copy",
        "--last",
        dest="copy",
        action="store_true",
        help="recopie la derniere transcription dans le presse-papier",
    )
    args = parser.parse_args()

    if args.copy:
        _copy_last()
    else:
        _print_entries(read(args.n))


if __name__ == "__main__":
    main()
