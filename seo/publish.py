"""Veröffentlichung per Git. Cloudflare Pages (oder Netlify) deployt automatisch bei jedem Push."""
from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def publish(cfg: dict, new_pages: int) -> str:
    root: Path = cfg["_root"]
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return "Kein Git-Repository: Seite wurde nur lokal gebaut (Ordner 'site')."
    paths = [cfg["paths"]["site"], cfg["paths"]["data"], cfg["paths"]["reports"]]
    _git(root, "add", "-A", *paths)
    if _git(root, "diff", "--cached", "--quiet").returncode == 0:
        return "Keine Änderungen zu veröffentlichen."
    msg = f"Tageslauf {date.today().isoformat()}: {new_pages} neue Seite(n)"
    r = _git(root, "commit", "-m", msg)
    if r.returncode != 0:
        return f"Commit fehlgeschlagen: {r.stderr.strip()}"
    # Parallel gepushte Änderungen (z. B. Code-Updates) übernehmen und erneut versuchen,
    # damit die Arbeit eines Laufs nie verloren geht. Bei Konflikten in erzeugten Dateien gewinnt dieser Lauf.
    for attempt in range(4):
        r = _git(root, "push")
        if r.returncode == 0:
            return f"Veröffentlicht: {msg}" + (f" (nach {attempt} Wiederholung(en))" if attempt else "")
        pr = _git(root, "pull", "--rebase", "-X", "theirs")
        if pr.returncode != 0:
            _git(root, "rebase", "--abort")
            pr = _git(root, "pull", "--no-rebase", "-X", "ours", "--no-edit")
            if pr.returncode != 0:
                return f"Push fehlgeschlagen, Abgleich nicht möglich: {pr.stderr.strip()[:300]}"
    return f"Push fehlgeschlagen: {r.stderr.strip()}"
