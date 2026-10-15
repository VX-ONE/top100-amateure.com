"""Optimierer: wertet täglich Rankings, Klickrate und Suchanfragen je Seite aus, verbessert Seiten mit Potenzial
und macht Verschlechterungen rückgängig.

Regeln (alle Schwellen in config.yaml unter optimize):
- title_ctr:   Seite rankt vorn (Position <= 10), wird aber kaum geklickt -> Titel, Meta-Beschreibung, H1 schärfer formulieren.
- striking:    Seite steht auf Position 8 bis 30 -> Abschnitte und FAQ zu den Suchanfragen ergänzen, die bisher nicht abgedeckt sind.
- no_traction: Seite ist indexiert, aber nach langer Zeit ohne Impressionen -> einmalig mit anderem Blickwinkel neu schreiben.
- not_indexed: nur im Bericht (die Seite ist noch nicht im Index, dagegen hilft Warten und Qualität, nicht Umschreiben).

Schutz: Mindestalter, Abkühlzeit je Seite, Höchstzahl pro Lauf, jede Änderung geht durch dasselbe Qualitäts-Gate wie neue
Seiten (bei Model-Seiten zusätzlich Sperrbegriffe und facts), die alte Fassung wird in data/history/ gesichert, und nach
effect_days wird verglichen: wurde die Position deutlich schlechter, wird die alte Fassung wiederhergestellt.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from . import generate, quality
from . import models as modelmod
from .keywords import Vocab, classify, load_blocked, normalize

DEFAULTS = {
    "enabled": True, "min_age_days": 21, "cooldown_days": 28, "max_per_run": 3, "min_impressions": 30,
    "striking_min": 8, "striking_max": 30, "ctr_floor": 0.015, "no_traction_days": 60, "effect_days": 21,
    "rollback_drop": 4,
}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("optimize") or {})}


def _age(meta: dict, today: date) -> int:
    return (today - date.fromisoformat(meta["published_at"])).days


def analyze(pages: dict[str, dict], cfg: dict, today: date | None = None) -> tuple[list[dict], list[dict]]:
    """Reine Auswertung ohne Netz. Gibt (Aktionen, nur_Bericht) zurück."""
    s = settings(cfg)
    today = today or date.today()
    actions, notes = [], []
    for slug, p in pages.items():
        age = _age(p, today)
        if age < s["min_age_days"]:
            continue
        opt = p.get("opt", {})
        last = opt.get("last")
        if last and (today - date.fromisoformat(last)).days < s["cooldown_days"]:
            continue
        impr, clicks, pos = p.get("impressions", 0), p.get("clicks", 0), p.get("position")
        if p.get("indexed") is False:
            notes.append({"slug": slug, "rule": "not_indexed", "reason": f"nicht indexiert ({p.get('coverage') or 'unbekannt'}), {age} Tage alt"})
            continue
        ctr = clicks / impr if impr else 0.0
        if impr >= s["min_impressions"] and pos is not None and pos <= 10 and ctr < s["ctr_floor"]:
            actions.append({"slug": slug, "rule": "title_ctr", "score": impr,
                            "reason": f"Position {pos}, aber nur {ctr:.1%} Klickrate bei {impr} Impressionen"})
        elif impr >= s["min_impressions"] and pos is not None and s["striking_min"] <= pos <= s["striking_max"]:
            actions.append({"slug": slug, "rule": "striking", "score": impr,
                            "reason": f"Position {pos} bei {impr} Impressionen: Potenzial für die erste Seite"})
        elif (age >= s["no_traction_days"] and p.get("indexed") is True and impr < 5
              and p.get("kind") != modelmod.PAGE_KIND and opt.get("rewrites", 0) < 1):
            actions.append({"slug": slug, "rule": "no_traction", "score": 0,
                            "reason": f"indexiert, aber nach {age} Tagen nur {impr} Impressionen"})
    actions.sort(key=lambda a: -a["score"])
    return actions[:s["max_per_run"]], notes


# ---------- Änderungen erzeugen ----------

def _page_queries(cfg: dict, svc, p: dict, days: int = 28) -> list[dict]:
    from . import gsc
    return gsc.page_queries(cfg, svc, p["url"], days=days)


def _usable_queries(rows: list[dict], p: dict, cfg: dict, model: dict | None) -> list[dict]:
    """Nur Suchanfragen, die auch als neue Keywords erlaubt wären (gleiche Filter wie im Kreislauf)."""
    vocab, blocked = Vocab(cfg), load_blocked(cfg)
    out = []
    for r in rows:
        k = normalize(r["query"])
        ok = modelmod.check_keyword(k, model, cfg)[0] if model else classify(k, cfg, vocab, blocked)[0]
        if ok:
            out.append({**r, "query": k})
    return out[:10]


def _slim(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "image"}


_STR = {"type": "string"}
SCHEMAS = {
    "title_ctr": {"type": "object", "properties": {"title": _STR, "meta_description": _STR, "h1": _STR},
                  "required": ["title", "meta_description", "h1"]},
    "striking": {"type": "object", "properties": {
        "sections": {"type": "array", "items": {"type": "object", "properties": {"h2": _STR, "body": _STR},
                                                 "required": ["h2", "body"]}},
        "faq": {"type": "array", "items": {"type": "object", "properties": {"q": _STR, "a": _STR},
                                            "required": ["q", "a"]}}}, "required": ["sections", "faq"]},
}


def _ask(cfg: dict, system: str, user: str, rule: str) -> dict:
    return generate.ask_json(cfg, system, user, SCHEMAS[rule])


RULE_TEXT = {
    "title_ctr": ("Die Seite rankt weit vorn, wird aber zu selten geklickt. Schreibe NUR title (20 bis 65 Zeichen), "
                  "meta_description (80 bis 165 Zeichen) und h1 neu: konkreter Nutzen, Hauptkeyword vorn, "
                  "ehrlich, kein Clickbait, keine Versprechen, die der Text nicht hält. "
                  'Antwort: {"title": "...", "meta_description": "...", "h1": "..."}'),
    "striking": ("Die Seite steht auf Position 8 bis 30. Ergänze höchstens 2 neue Abschnitte und höchstens 2 neue "
                 "FAQ-Einträge, die die unten genannten Suchanfragen beantworten, soweit der bestehende Text sie nicht "
                 "abdeckt. Bestehende Abschnitte bleiben unverändert. Jeder neue Abschnitt hat mindestens 120 Wörter. "
                 'Antwort: {"sections": [{"h2": "...", "body": "..."}], "faq": [{"q": "...", "a": "..."}]}'),
}


def _mock_change(rule: str, c: dict, p: dict) -> dict:
    cap = p["primary"][:1].upper() + p["primary"][1:]
    if rule == "title_ctr":
        return {"title": f"{cap}: Antworten und Überblick", "meta_description": c["meta_description"], "h1": c["h1"]}
    if rule == "striking":
        pool = generate._POOL
        body = " ".join(pool[:8])
        return {"sections": [{"h2": f"{cap}: häufige Nachfragen", "body": body}], "faq": []}
    return generate.mock_generate({"slug": p["slug"] + "-v2", "primary": p["primary"]}, {})


def propose(action: dict, p: dict, c: dict, cfg: dict, facts: str, queries: list[dict], model: dict | None,
            mock: bool) -> dict | None:
    """Gibt die neue Fassung des Inhalts zurück (noch ungeprüft)."""
    rule = action["rule"]
    new = json.loads(json.dumps(c))
    if rule == "no_traction":
        cluster = {"slug": p["slug"], "primary": p["primary"], "keywords": p["keywords"], "topic": p["topic"]}
        draft = (generate.clean_content(_mock_change(rule, c, p)) if mock else generate.generate(
            cluster, cfg, facts, [], ["Die bisherige Fassung bekommt keine Impressionen. Wähle einen deutlich anderen "
                                      "Blickwinkel, andere Gliederung und beantworte die Fragen der Suchenden konkreter."]))
        new.update({k: draft[k] for k in ("title", "meta_description", "h1", "intro", "key_takeaways", "sections", "faq",
                                          "cta_text")})
        return new
    if mock:
        change = _mock_change(rule, c, p)
    else:
        system = generate.MODEL_SYSTEM if model else generate.SYSTEM
        user = (f"Aktuelle Seite (JSON):\n{json.dumps(_slim(c), ensure_ascii=False)}\n\n"
                f"Hauptkeyword: {p['primary']}\n"
                f"Suchanfragen mit Impressionen (Search Console):\n"
                + "\n".join(f"- {q['query']} ({q['impressions']} Impr., Position {q['position']:.0f})" for q in queries)
                + ("\n\nFAKTEN über das Model (einzige zulässige Quelle):\n- " + "\n- ".join(model["facts"]) if model else
                   "\n\nFAKTEN:\n" + (facts or "(keine)"))
                + f"\n\nAufgabe: {RULE_TEXT[rule]}")
        change = _ask(cfg, system, user, rule)
    if rule == "title_ctr":
        new.update({k: generate.clean_text(str(change[k])) for k in ("title", "meta_description", "h1")})
    else:
        have = {normalize(s["h2"]) for s in new["sections"]}
        add_s = [{"h2": generate.clean_text(s["h2"]), "body": generate.clean_text(s["body"])}
                 for s in change.get("sections", []) if normalize(s["h2"]) not in have][:2]
        have_q = {normalize(f["q"]) for f in new["faq"]}
        add_f = [{"q": generate.clean_text(f["q"]), "a": generate.clean_text(f["a"])}
                 for f in change.get("faq", []) if normalize(f["q"]) not in have_q][:2]
        if not add_s and not add_f:
            return None
        new["sections"] += add_s
        new["faq"] += add_f
    return new


def check(new: dict, p: dict, cfg: dict, facts: str, texts: dict[str, str], model: dict | None, mock: bool) -> list[str]:
    cluster = {"primary": p["primary"]}
    others = {s: t for s, t in texts.items() if s != p["slug"]}
    if model:
        lim = cfg["limits"]
        issues = quality.check(new, cluster, cfg, "\n".join(model["facts"]) + "\n" + facts, others, relaxed=mock,
                               blocked=[], min_words=lim["min_words"], max_sim_limit=lim["max_similarity"])
        return issues + modelmod.text_problems(quality.article_text(new), model, cfg)
    return quality.check(new, cluster, cfg, facts, others, relaxed=mock)


# ---------- Wirkung messen, zurückrollen ----------

def _history_dir(store, slug: str) -> Path:
    return Path(store.dir) / "history" / slug


def evaluate_effects(pages: dict[str, dict], store, cfg: dict, today: date | None = None) -> list[str]:
    s = settings(cfg)
    today = today or date.today()
    lines = []
    for slug, p in pages.items():
        for e in p.get("opt", {}).get("log", []):
            if e.get("status") != "applied" or (today - date.fromisoformat(e["date"])).days < s["effect_days"]:
                continue
            base, pos = e.get("base", {}).get("position"), p.get("position")
            if base is None or pos is None:
                e["status"] = "kept"
                e["result"] = "keine Positionsdaten zum Vergleich"
                continue
            if pos - base >= s["rollback_drop"] and p.get("impressions", 0) <= e["base"].get("impressions", 0):
                f = _history_dir(store, slug) / e["backup"]
                if f.exists():
                    old = json.loads(f.read_text(encoding="utf-8"))
                    cur = store.content(slug) or {}
                    old["image"] = cur.get("image", old.get("image"))
                    store.save_content(slug, old)
                    p["modified_at"] = today.isoformat()
                    e["status"] = "reverted"
                    e["result"] = f"Position {base} -> {pos}, alte Fassung wiederhergestellt"
                    lines.append(f"- {slug}: {e['rule']} zurückgerollt (Position {base} -> {pos})")
                    continue
            e["status"] = "kept"
            e["result"] = f"Position {base} -> {pos}, Impressionen {e['base'].get('impressions', 0)} -> {p.get('impressions', 0)}"
            lines.append(f"- {slug}: {e['rule']} behalten ({e['result']})")
    return lines


# ---------- Ablauf ----------

def run(cfg: dict, store, svc=None, mock: bool = False, today: date | None = None) -> dict:
    s = settings(cfg)
    today = today or date.today()
    result = {"applied": [], "rejected": [], "notes": [], "effects": []}
    if not s["enabled"]:
        return result
    pages = store.pages()
    result["effects"] = evaluate_effects(pages, store, cfg, today)
    actions, notes = analyze(pages, cfg, today)
    result["notes"] = notes
    facts = generate.load_facts(cfg["_root"])
    contents = store.all_content()
    texts = {sl: quality.article_text(c) for sl, c in contents.items()}
    models = {m["name"]: m for m in modelmod.load_models(cfg)}

    for a in actions:
        slug = a["slug"]
        p, c = pages[slug], contents.get(slug)
        if c is None:
            continue
        model = models.get(p.get("model")) if p.get("kind") == modelmod.PAGE_KIND else None
        if p.get("kind") == modelmod.PAGE_KIND and (model is None or not model["active"]):
            continue
        queries: list[dict] = []
        if a["rule"] == "striking" and not mock:
            if svc is None:
                continue
            try:
                queries = _usable_queries(_page_queries(cfg, svc, p), p, cfg, model)
            except Exception as e:
                print(f"  [{slug}] Suchanfragen nicht abrufbar: {type(e).__name__}: {e}")
                continue
            if not queries:
                continue
        try:
            new = propose(a, p, c, cfg, facts, queries, model, mock)
        except Exception as e:
            result["rejected"].append(f"{slug} ({a['rule']}): Erzeugung fehlgeschlagen: {type(e).__name__}")
            continue
        if new is None:
            continue
        issues = check(new, p, cfg, facts, texts, model, mock)
        if issues:
            result["rejected"].append(f"{slug} ({a['rule']}): {issues[0]}")
            continue
        hist = _history_dir(store, slug)
        hist.mkdir(parents=True, exist_ok=True)
        backup = f"{today.isoformat()}-{a['rule']}.json"
        (hist / backup).write_text(json.dumps(c, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        store.save_content(slug, new)
        texts[slug] = quality.article_text(new)
        opt = p.setdefault("opt", {"log": [], "rewrites": 0})
        opt["last"] = today.isoformat()
        if a["rule"] == "no_traction":
            opt["rewrites"] = opt.get("rewrites", 0) + 1
        opt["log"].append({"date": today.isoformat(), "rule": a["rule"], "reason": a["reason"], "status": "applied",
                           "backup": backup, "base": {"position": p.get("position"), "impressions": p.get("impressions", 0),
                                                      "clicks": p.get("clicks", 0)}})
        p["title"] = new["title"]
        p["modified_at"] = today.isoformat()
        result["applied"].append(f"{slug}: {a['rule']} ({a['reason']})")
    store.save_pages(pages)
    return result


def report_section(result: dict) -> str:
    lines = ["", "## Optimierung", ""]
    lines += [f"- Geändert: {x}" for x in result["applied"]] or ["- Heute keine Änderungen."]
    lines += [f"- Verworfen (Qualitäts-Gate): {x}" for x in result["rejected"]]
    lines += [f"- Nur beobachtet: {n['slug']}: {n['reason']}" for n in result["notes"][:20]]
    if result["effects"]:
        lines += ["", "### Wirkung früherer Änderungen", ""] + result["effects"]
    return "\n".join(lines) + "\n"
