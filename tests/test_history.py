"""Unit tests for the JSONL transcription history (history.py)."""

import config
import history


def test_append_then_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_PATH", str(tmp_path / "history.jsonl"))
    history.append("hello world")
    entries = history.read(10)
    assert len(entries) == 1
    assert entries[0]["text"] == "hello world"
    assert entries[0]["chars"] == len("hello world")
    assert "ts" in entries[0]


def test_append_ignores_empty_text(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_PATH", str(tmp_path / "history.jsonl"))
    history.append("")
    assert history.read(10) == []


def test_read_returns_last_n_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_PATH", str(tmp_path / "history.jsonl"))
    for i in range(5):
        history.append(f"entry {i}")
    last_two = history.read(2)
    assert [e["text"] for e in last_two] == ["entry 3", "entry 4"]


def test_read_skips_corrupted_lines(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_text('{"text": "ok", "chars": 2, "ts": "t"}\nnot-json\n', encoding="utf-8")
    monkeypatch.setattr(config, "HISTORY_PATH", str(path))
    entries = history.read(10)
    assert len(entries) == 1
    assert entries[0]["text"] == "ok"


def test_read_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_PATH", str(tmp_path / "nope.jsonl"))
    assert history.read(10) == []
