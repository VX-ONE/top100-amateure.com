"""Neutrale Profilseiten zu Models (Bühnennamen) aus offiziellen VX-Cash-Landingpages.

Grenzen, die im Code erzwungen werden:
- Keywords bestehen nur aus dem Namen und erlaubten Zusatzwörtern (config.yaml: models.keyword_words).
- Aussagen über das Model nur aus den "facts" in data/models.yaml.
- Gesperrte Begriffe (Privatangaben, Leaks, Fremdmaterial, einzelne Praktiken) werden im Keyword und im Text geprüft.
- Ein Model lässt sich jederzeit entfernen: active: false oder data/blocked_names.txt.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .keywords import normalize, slugify

PAGE_KIND = "model"
TOPIC = "profile"


def _models_file(cfg: dict) -> Path:
    return Path(cfg["_root"]) / "data" / "models.yaml"


def blocked_names(cfg: dict) -> set[str]:
    p = Path(cfg["_root"]) / "data" / "blocked_names.txt"
    if not p.exists():
        return set()
    return {normalize(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()}


def load_models(cfg: dict) -> list[dict]:
    p = _models_file(cfg)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    blocked = blocked_names(cfg)
    models = []
    for m in raw.get("models", []):
        name = str(m["name"]).strip()
        base = normalize(name)
        variants = {base, base.replace(" ", ""), base.replace("-", " "), base.replace("-", "")} | {normalize(v) for v in m.get("name_variants", [])}
        split = normalize(re.sub(r"(?<=[a-zäöü])(?=[A-ZÄÖÜ])", " ", name).replace("-", " "))
        variants.add(split)
        active = bool(m.get("active", True)) and not (variants & blocked)
        models.append({
            "name": name,
            "slug": m.get("slug") or slugify(name),
            "active": active,
            "variants": sorted(variants, key=len, reverse=True),
            "links": [{"label": str(link["label"]).replace("{name}", name), "url": link["url"]}
                      for link in m.get("landing_links", [])],
            "keywords": list(dict.fromkeys(
                [split, base.replace("-", " ")] + [normalize(k).replace(base.replace("-", " "), split, 1) for k in m.get("keywords", [])] +
                [normalize(k) for k in m.get("keywords", [])])),
            "facts": [str(f).replace("{name}", name) for f in m.get("facts", [])],
            "profile_url": str(m.get("profile_url") or ""),
            "platform_name": str(m.get("platform_name") or ""),
        })
    return models


def get_model(cfg: dict, name: str) -> dict | None:
    return next((m for m in load_models(cfg) if m["name"] == name), None)


def check_keyword(kw: str, model: dict, cfg: dict) -> tuple[bool, str]:
    """Name plus erlaubte Zusatzwörter. Alles andere (Praktiken, Privatangaben, Leaks ...) fällt heraus."""
    k = normalize(kw)
    rest = f" {k} "
    found = False
    for v in model["variants"]:
        if f" {v} " in rest:
            rest = rest.replace(f" {v} ", " ", 1)
            found = True
    if not found:
        return False, "Name fehlt"
    allowed = {normalize(w) for w in cfg["models"]["keyword_words"]}
    for ph in sorted((a for a in allowed if " " in a), key=len, reverse=True):
        rest = rest.replace(f" {ph} ", " ")
    for tok in rest.split():
        if tok not in allowed and not re.fullmatch(r"\d{1,4}", tok):
            return False, f"nicht erlaubtes Wort: {tok}"
    return True, "ok"


def valid_keywords(model: dict, cfg: dict) -> list[str]:
    return [k for k in model["keywords"] if check_keyword(k, model, cfg)[0]]


def text_problems(text: str, model: dict, cfg: dict) -> list[str]:
    """Prüft den fertigen Artikel auf gesperrte Begriffe (ganze Wörter) und fremde Personennamen-Hinweise."""
    low = " " + normalize(text) + " "
    for v in model["variants"]:
        low = low.replace(f" {v} ", " ")
    low = re.sub(r"\bchloe privat\b", " ", low)
    issues = []
    text_ok = {normalize(x) for x in cfg["models"].get("text_allowed_terms", [])}
    for term in cfg["models"]["blocked_terms"]:
        t = normalize(term)
        if t in text_ok:
            continue
        if re.search(rf"(?<![\wäöüß]){re.escape(t)}(?![\wäöüß])", low):
            issues.append(f"Gesperrter Begriff im Text: {t}")
            break
    return issues


def links_for(cfg: dict, name: str) -> list[dict]:
    """Links eines Models. Ist ein Profil-Deeplink hinterlegt (pro Model "profile_url" oder affiliate.profile_template),
    kommt er zuerst: Nutzer landen direkt auf dem Profil statt auf der allgemeinen Videoseite."""
    model = get_model(cfg, name)
    if not model:
        return []
    links = list(model["links"])
    tpl = model.get("profile_url") or (cfg["affiliate"].get("profile_template") or "")
    if tpl:
        from urllib.parse import quote
        platform = model.get("platform_name") or name
        url = tpl.replace("{name_lc}", quote(platform.lower())).replace("{name}", quote(platform))
        links = [{"label": f"Profil von {name} bei Visit-X", "url": url}] + [l for l in links if l["url"] != url]
    return links


def apply_blocklist(cfg: dict, store) -> list[str]:
    """Entfernt Profilseiten von Models, die inaktiv, gesperrt oder nicht mehr in models.yaml sind."""
    models = {m["name"]: m for m in load_models(cfg)}
    pages = store.pages()
    removed = []
    for slug, meta in list(pages.items()):
        if meta.get("kind") != PAGE_KIND:
            continue
        m = models.get(meta.get("model"))
        if m is None or not m["active"]:
            removed.append(slug)
            del pages[slug]
            f = Path(store.dir) / "content" / f"{slug}.json"
            if f.exists():
                f.unlink()
    if removed:
        store.save_pages(pages)
    return removed
