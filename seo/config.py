"""Konfiguration laden und für den Live-Betrieb validieren."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(path: str | None = None, sandbox: bool = False) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    base = ROOT / ".sandbox" if sandbox else ROOT
    cfg["_root"] = ROOT
    cfg["_data"] = base / cfg["paths"]["data"]
    cfg["_site"] = base / cfg["paths"]["site"]
    cfg["_reports"] = base / cfg["paths"]["reports"]
    cfg["_sandbox"] = sandbox
    return cfg


def placeholder(value) -> bool:
    s = str(value or "").strip()
    return (not s) or "HIER" in s or "DEINE" in s


def live_problems(cfg: dict) -> list[str]:
    """Alles, was vor dem ersten echten Lauf stimmen muss."""
    problems: list[str] = []
    if placeholder(cfg["site"]["base_url"]):
        problems.append("site.base_url ist noch ein Platzhalter (config.yaml).")
    if placeholder(cfg["affiliate"]["url_template"]):
        problems.append("affiliate.url_template ist noch ein Platzhalter (config.yaml).")
    op = cfg["site"].get("operator", {})
    for key, label in (("name", "Name"), ("address", "Anschrift"), ("email", "E-Mail")):
        if not str(op.get(key, "")).strip():
            problems.append(f"site.operator.{key} ({label}) fehlt (Pflichtangabe für das Impressum).")
    if "gmbh" in str(op.get("name", "")).lower():
        for key, label in (("managing_directors", "Geschäftsführer"), ("register_court", "Registergericht"),
                           ("register_number", "Handelsregisternummer")):
            if not str(op.get(key, "")).strip():
                problems.append(f"site.operator.{key} ({label}) fehlt (Pflichtangabe im Impressum einer GmbH).")
    return problems
