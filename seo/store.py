"""Einfacher JSON-Speicher. Textformat, damit Änderungen in Git nachvollziehbar bleiben."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _read(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        (self.dir / "content").mkdir(parents=True, exist_ok=True)

    # Cluster = eine Seite mit Hauptkeyword und verwandten Suchbegriffen
    def clusters(self) -> list[dict]:
        return _read(self.dir / "clusters.json", [])

    def save_clusters(self, value: list[dict]) -> None:
        _write(self.dir / "clusters.json", value)

    # Veröffentlichte Seiten inkl. Indexierungs- und Leistungsdaten
    def pages(self) -> dict[str, dict]:
        return _read(self.dir / "pages.json", {})

    def save_pages(self, value: dict[str, dict]) -> None:
        _write(self.dir / "pages.json", value)

    def state(self) -> dict:
        state = _read(self.dir / "state.json", {})
        state.setdefault("throttle", {"active": False})
        state.setdefault("paused_topics", {})
        return state

    def save_state(self, value: dict) -> None:
        _write(self.dir / "state.json", value)

    # Inhalt einer Seite
    def content(self, slug: str) -> dict | None:
        return _read(self.dir / "content" / f"{slug}.json", None)

    def save_content(self, slug: str, value: dict) -> None:
        _write(self.dir / "content" / f"{slug}.json", value)

    def all_content(self) -> dict[str, dict]:
        out = {}
        for p in sorted((self.dir / "content").glob("*.json")):
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        return out
