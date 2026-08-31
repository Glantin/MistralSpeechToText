"""MistralSpeechToText transcription history.

Every successful transcription is logged to history.jsonl (one JSON line per
entry). Useful when a paste is lost (no text field focused): you can find the
text and copy it again.

CLI usage:
    uv run python history.py            # show the last N entries
    uv run python history.py -n 5       # the last 5
    uv run python history.py --copy     # copy the last one to the clipboard
    uv run python history.py --last     # alias of --copy
"""

import argparse
import json
from datetime import datetime

import config


def append(text: str) -> None:
    """Append an entry to the history. Never raises (best-effort)."""
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
        # History must never break the dictation flow.
        pass


def read(n: int) -> list[dict]:
    """Return the last `n` valid entries (most recent last)."""
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
            continue  # corrupted line: skip it
    return entries[-n:] if n > 0 else entries


def _fmt_ts(iso: str) -> str:
    """Render an ISO timestamp readably (without crashing on odd formats)."""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso or "?"


def _print_entries(entries: list[dict]) -> None:
    if not entries:
        print("(history empty)")
        return
    for e in entries:
        ts = _fmt_ts(e.get("ts", ""))
        text = e.get("text", "")
        print(f"[{ts}] {text}")


def _copy_last() -> None:
    entries = read(1)
    if not entries:
        print("(history empty: nothing to copy)")
        return
    text = entries[-1].get("text", "")
    if not text:
        print("(last entry empty: nothing to copy)")
        return
    # Late import: avoid loading AppKit when we only display.
    from inserter import set_clipboard

    set_clipboard(text)
    print(f"copied to the clipboard ({len(text)} characters): {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="MistralSpeechToText transcription history.")
    parser.add_argument(
        "-n",
        type=int,
        default=config.HISTORY_DEFAULT_N,
        help=f"number of entries to show (default {config.HISTORY_DEFAULT_N})",
    )
    parser.add_argument(
        "--copy",
        "--last",
        dest="copy",
        action="store_true",
        help="copy the last transcription to the clipboard",
    )
    args = parser.parse_args()

    if args.copy:
        _copy_last()
    else:
        _print_entries(read(args.n))


if __name__ == "__main__":
    main()
