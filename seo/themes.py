"""Themenseiten je Model: /<model>/videos/<thema>/ (z. B. /lia-engel/videos/anal/).

Grundlage: lucycat.com-Search-Console-Export (Okt. 2026): 12 % der Suchanfragen mit Modelnamen sind
„<Name> <Genre>“ (Anal, Creampie, Footjob, Latex, JOI/Wichsanleitung, Public, Domina …). Freigabe Chris 27.10.2026.

- Ein Thema gilt für ein Video, wenn der Begriff im Titel oder in den Visit-X-Stichworten steht (keine Vermutungen).
- Eine Themenseite entsteht erst ab `themes.min_videos` passenden veröffentlichten Videos und wird schrittweise
  freigeschaltet (`themes.per_day`), damit die Indexierung nicht leidet.
- Jede Seite bekommt einen eigenen, nicht expliziten Einleitungstext (Claude Haiku) und listet die passenden Videos.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .quality import EXPLICIT

# Reihenfolge = Priorität bei gleicher Videoanzahl (nach Suchnachfrage im lucycat-Export)
THEMES = [
    {"slug": "anal", "label": "Anal", "re": r"\banal", "hint": "Anal-Szenen"},
    {"slug": "creampie", "label": "Creampie", "re": r"creampie", "hint": "Creampie-Clips"},
    {"slug": "footjob", "label": "Footjob", "re": r"footjob|füße|fuesse|\bfeet\b|fußfetisch|fussfetisch", "hint": "Footjob- und Fuß-Clips"},
    {"slug": "latex", "label": "Latex", "re": r"latex|\black\b", "hint": "Latex- und Lack-Outfits"},
    {"slug": "joi", "label": "JOI", "re": r"\bjoi\b|wichsanleitung|wichsanweisung", "hint": "JOI-Clips (Anleitung zum Mitmachen)"},
    {"slug": "public", "label": "Public", "re": r"public|öffentlich|outdoor|freibad|draußen|parkplatz|im wald", "hint": "Public- und Outdoor-Clips"},
    {"slug": "natursekt", "label": "Natursekt", "re": r"natursekt|\bpiss|\bpee\b", "hint": "Natursekt-Clips"},
    {"slug": "dreier", "label": "Dreier", "re": r"dreier|threesome|\b3er\b|\bmmf\b|\bffm\b", "hint": "Dreier-Clips"},
    {"slug": "domina", "label": "Domina", "re": r"domina|femdom|herrin", "hint": "Domina- und Femdom-Clips"},
    {"slug": "cumshot", "label": "Cumshot", "re": r"cumshot|compilation", "hint": "Cumshot-Clips und Compilations"},
    {"slug": "blowjob", "label": "Blowjob", "re": r"blowjob|deepthroat", "hint": "Blowjob-Clips"},
    {"slug": "squirt", "label": "Squirt", "re": r"squirt", "hint": "Squirt-Clips"},
    {"slug": "solo", "label": "Solo", "re": r"\bsolo", "hint": "Solo-Clips"},
    {"slug": "pov", "label": "POV", "re": r"\bpov\b", "hint": "POV-Clips (aus deiner Sicht gefilmt)"},
    {"slug": "lesbian", "label": "Lesbian", "re": r"lesbian|\blesben|\blesbisch", "hint": "Lesbian-Clips mit zwei Frauen"},
    {"slug": "gangbang", "label": "Gangbang", "re": r"gangbang", "hint": "Gangbang-Clips"},
    {"slug": "dirty-talk", "label": "Dirty Talk", "re": r"dirty ?talk", "hint": "Dirty-Talk-Clips"},
    {"slug": "dessous", "label": "Dessous", "re": r"dessous|strapse|nylon|strumpf", "hint": "Clips in Dessous und Nylons"},
    {"slug": "massage", "label": "Massage", "re": r"massage", "hint": "Massage-Clips"},
]
BY_SLUG = {t["slug"]: t for t in THEMES}
_RX = {t["slug"]: re.compile(t["re"], re.I) for t in THEMES}
VERSION = 1

SYSTEM = """Du schreibst für top100amateure.com, einen unabhängigen deutschsprachigen Ratgeber zu Amateur-Models bei Visit-X.
Aufgabe: kurzer, einzigartiger Einleitungstext für eine Themenseite, die alle Videos eines Models zu einem Thema auflistet.

Regeln:
1. Ton: locker, neugierig machend, augenzwinkernd. Andeuten statt beschreiben. NIE explizit: keine Körperteile, keine Sexwörter
   (z. B. ficken, Schwanz, Muschi, geil, Orgasmus, Sperma). Das Thema selbst nur als Kategorienamen nennen.
2. Nur Fakten aus den Angaben verwenden (Name, Anzahl Videos, Längen, neueste Titel als Stimmung). Nichts erfinden
   (kein Alter, kein Wohnort, keine Biografie, keine Bewertungen).
3. Den genauen Suchbegriff „{kw}“ genau einmal im Einleitungstext und einmal in der Zwischenüberschrift natürlich verwenden.
   Kein Keyword-Stuffing, keine Fettungen.
4. Keine Standardsätze über Zahlungsarten, Registrierung oder Tokens (das steht schon auf der Seite).
5. Deutsch, Du-Ansprache, keine Emojis, keine Anführungszeichen um Videotitel.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "meta_description": {"type": "string", "description": "130-155 Zeichen, mit Suchbegriff und Anzahl Videos"},
        "intro": {"type": "string", "description": "100-140 Wörter, enthält den Suchbegriff genau einmal"},
        "h2": {"type": "string", "description": "Zwischenüberschrift mit dem Suchbegriff, max. 60 Zeichen"},
        "body": {"type": "string", "description": "140-190 Wörter in 2 Absätzen: was die Videos dieses Themas bei diesem Model ausmacht (Stimmung, Bandbreite, Längen, für wen sie passen), andeutend"},
        "faq": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string", "description": "2-3 Sätze"}}, "required": ["q", "a"]}},
    },
    "required": ["meta_description", "intro", "h2", "body", "faq"],
}


def settings(cfg: dict) -> dict:
    s = {"enabled": True, "min_videos": 5, "per_day": 40, "generate_per_run": 60}
    s.update(((cfg.get("videos") or {}).get("themes")) or {})
    return s


def detect(v: dict) -> list[str]:
    text = (v.get("title", "") + " " + " ".join(v.get("tags") or [])).lower()
    return [t for t, rx in _RX.items() if rx.search(text)]


def _root(cfg: dict) -> Path:
    d = Path(cfg["_data"]) / "videos"
    (d / "themes").mkdir(parents=True, exist_ok=True)
    return d


def load_state(cfg: dict) -> dict:
    p = _root(cfg) / "_themes.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"released": {}}


def save_state(cfg: dict, st: dict) -> None:
    (_root(cfg) / "_themes.json").write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def load_content(cfg: dict, key: str) -> dict | None:
    p = _root(cfg) / "themes" / (key.replace("/", "__") + ".json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_content(cfg: dict, key: str, c: dict) -> None:
    p = _root(cfg) / "themes" / (key.replace("/", "__") + ".json")
    p.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def candidates(cfg: dict, collected: dict) -> dict[str, dict]:
    """{"<model>/<thema>": {"slug", "theme", "videos"}} für alle Kombinationen mit genug veröffentlichten Videos."""
    s = settings(cfg)
    out = {}
    for slug, d in collected.items():
        by = {}
        for v in d["videos"]:
            for t in detect(v):
                by.setdefault(t, []).append(v)
        for t, vs in by.items():
            if len(vs) >= int(s["min_videos"]):
                out[f"{slug}/{t}"] = {"slug": slug, "theme": t, "videos": vs}
    return out


def release(cfg: dict, collected: dict, log=print) -> dict:
    """Schaltet pro Tag höchstens `per_day` neue Themenseiten frei (größte zuerst, reihum über die Models)."""
    s = settings(cfg)
    if not s["enabled"]:
        return {"freigeschaltet": 0, "aktiv": 0}
    st = load_state(cfg)
    today = date.today().isoformat()
    done_today = sum(1 for d in st["released"].values() if d == today)
    budget = max(0, int(s["per_day"]) - done_today)
    cand = candidates(cfg, collected)
    order = {t["slug"]: i for i, t in enumerate(THEMES)}
    queues = {}
    # zuerst die laut lucycat-Daten meistgesuchten Themen, innerhalb eines Themas die größten Seiten
    for k, c in sorted(cand.items(), key=lambda x: (order[x[1]["theme"]], -len(x[1]["videos"]))):
        if k not in st["released"]:
            queues.setdefault(c["slug"], []).append(k)
    new = []
    while budget > 0 and any(queues.values()):
        for slug in list(queues):
            if queues[slug] and budget > 0:
                k = queues[slug].pop(0)
                st["released"][k] = today
                new.append(k)
                budget -= 1
    if new:
        save_state(cfg, st)
    log(f"  Themenseiten: {len(new)} neu freigeschaltet, {len(st['released'])} insgesamt, {len(cand)} möglich")
    return {"freigeschaltet": len(new), "aktiv": len(st["released"]), "moeglich": len(cand), "neu": new}


def prompt_for(m: dict, t: dict, vids: list[dict]) -> str:
    kw = f"{m['name']} {t['label']}"
    mins = [v.get("duration", "") for v in vids if v.get("duration")]
    lines = [f"Model (Bühnenname): {m['name']}" + (f" (auch geschrieben: {m['name_alt']})" if m.get("name_alt") else ""),
             f"Thema: {t['label']} ({t['hint']})", f"Suchbegriff: {kw}", f"Anzahl passender Videos: {len(vids)}",
             f"Längen (min): {', '.join(mins[:12])}",
             "Neueste Titel (nur als Stimmung, nicht zitieren, keine expliziten Wörter übernehmen): " +
             " | ".join(re.sub(r"\s+", " ", v["title"])[:70] for v in vids[:8])]
    if m.get("facts"):
        lines.append("Fakten zum Model: " + " ".join(m["facts"][:4]))
    return "\n".join(lines) + "\n\nSchreibe die Texte im vorgegebenen Format."


def _text(c: dict) -> str:
    return " ".join([c.get("intro", ""), c.get("h2", ""), c.get("body", "")] +
                    [f.get("q", "") + " " + f.get("a", "") for f in c.get("faq", [])])


def _sh(t: str) -> set:
    w = re.findall(r"[a-zäöüß0-9]+", t.lower())
    return {" ".join(w[i:i + 5]) for i in range(max(0, len(w) - 4))}


def check(c: dict, m: dict, t: dict, others: list[str]) -> list[str]:
    issues = []
    text = _text(c)
    if EXPLICIT.search(text):
        issues.append(f"explizite Wörter ('{EXPLICIT.search(text).group(0)}')")
    if len(text.split()) < 260:
        issues.append(f"zu kurz ({len(text.split())} Wörter, mindestens 260)")
    kw = f"{m['name']} {t['label']}".lower()
    if kw not in re.sub(r"\s+", " ", (c.get("intro", "") + " " + c.get("h2", "")).lower()):
        issues.append(f"Suchbegriff „{m['name']} {t['label']}“ fehlt in Einleitung/Überschrift")
    if len(c.get("faq") or []) < 2:
        issues.append("2 Fragen mit q und a nötig")
    sh = _sh(text)
    for o in others:
        so = _sh(o)
        if sh and so and len(sh & so) / len(sh | so) > 0.15:
            issues.append("zu ähnlich zu einer anderen Themenseite – andere Formulierungen")
            break
    return issues


def mock(m: dict, t: dict, vids: list[dict]) -> dict:
    kw = f"{m['name']} {t['label']}"
    return {"meta_description": f"{kw}: {len(vids)} Videos bei Visit-X im Überblick.",
            "intro": f"{kw} – hier findest du {len(vids)} Clips. " + "Platzhaltertext für den Testlauf. " * 12,
            "h2": f"Was {kw} Videos ausmacht", "body": "Platzhaltertext für den Testlauf ohne Sprachmodell. " * 12,
            "faq": [{"q": f"Wie viele {kw} Videos gibt es?", "a": f"{len(vids)} Videos."},
                    {"q": "Wo kann ich sie ansehen?", "a": "Bei Visit-X über den Button."}], "version": VERSION}


def generate(cfg: dict, collected: dict, mock_mode: bool = False, log=print) -> dict:
    """Texte für freigeschaltete Themenseiten ohne Inhalt (parallel, mit Prüfung und einem zweiten Versuch)."""
    from concurrent.futures import ThreadPoolExecutor
    from . import generate as gen
    s = settings(cfg)
    st = load_state(cfg)
    cand = candidates(cfg, collected)
    jobs = [k for k in sorted(st["released"], key=lambda k: st["released"][k]) if k in cand and not load_content(cfg, k)]
    jobs = jobs[: int(s["generate_per_run"])]
    by_theme = {}
    for k in st["released"]:
        c = load_content(cfg, k)
        if c:
            by_theme.setdefault(k.split("/")[1], []).append(_text(c))
    llm_cfg = dict(cfg, llm=dict(cfg["llm"], model=(cfg.get("videos") or {}).get("llm_model") or cfg["llm"]["model"], max_tokens=8000))
    stats = {"erzeugt": 0, "verworfen": 0}

    def work(k):
        info = cand[k]
        m, t, vids = collected[info["slug"]]["model"], BY_SLUG[info["theme"]], info["videos"]
        issues, msgs = None, []
        for _ in range(2):
            try:
                p = prompt_for(m, t, vids) + (("\nLetzter Entwurf abgelehnt. Behebe: " + "; ".join(issues)) if issues else "")
                c = mock(m, t, vids) if mock_mode else gen.ask_json(llm_cfg, SYSTEM.replace("{kw}", f"{m['name']} {t['label']}"), p, SCHEMA)
                c = {"meta_description": gen.clean_text(c.get("meta_description", "")), "intro": gen.clean_text(c.get("intro", "")),
                     "h2": gen.clean_text(c.get("h2", "")), "body": gen.clean_text(c.get("body", "")),
                     "faq": [{"q": gen.clean_text(x.get("q", "")), "a": gen.clean_text(x.get("a", ""))}
                             for x in (c.get("faq") or []) if isinstance(x, dict) and x.get("q") and x.get("a")][:2],
                     "version": VERSION}
                issues = [] if mock_mode else check(c, m, t, by_theme.get(t["slug"], []))
            except Exception as e:
                msgs.append(f"  [Thema {k}] Fehler: {type(e).__name__}: {e}")
                issues = [str(e)[:80]]
                continue
            if not issues:
                return k, c, msgs
            msgs.append(f"  [Thema {k}] verworfen: {', '.join(issues)}")
        return k, None, msgs

    with ThreadPoolExecutor(max_workers=6) as ex:
        for k, c, msgs in ex.map(work, jobs):
            for x in msgs:
                log(x)
            if c:
                save_content(cfg, k, c)
                stats["erzeugt"] += 1
            else:
                stats["verworfen"] += 1
    return stats


def attach(cfg: dict, collected: dict) -> dict[str, dict]:
    """Aktive Themenseiten (freigeschaltet + Text vorhanden) an collected hängen: je Model eine Themenliste,
    je Video die passenden Themen. Gibt {key: info} der aktiven Seiten zurück."""
    st = load_state(cfg)
    cand = candidates(cfg, collected)
    live = {}
    for k in st["released"]:
        if k in cand:
            c = load_content(cfg, k)
            if c:
                info = dict(cand[k], content=c, url=f"/{k.split('/')[0]}/videos/{k.split('/')[1]}/")
                live[k] = info
    for slug, d in collected.items():
        lst = [{"slug": k.split("/")[1], "label": BY_SLUG[k.split("/")[1]]["label"], "count": len(i["videos"]), "url": i["url"]}
               for k, i in live.items() if i["slug"] == slug]
        d["themes"] = sorted(lst, key=lambda x: -x["count"])
        tmap = {x["slug"]: x for x in lst}
        for v in d["videos"]:
            v["themes"] = [tmap[t] for t in detect(v) if t in tmap]
    return live


def render(cfg: dict, env, ctx: dict, write, base: str, collected: dict, live: dict) -> list[tuple[str, str]]:
    from .videos import deeplink, platform
    tpl = env.get_template("theme.html")
    urls = []
    for k, info in live.items():
        slug, t = info["slug"], BY_SLUG[info["theme"]]
        m = collected[slug]["model"]
        vids, c = info["videos"], info["content"]
        n = len(vids)
        title = next(x for x in (f"{m['name']} {t['label']}: {n} Pornos & Videos bei Visit-X", f"{m['name']} {t['label']}: {n} Pornos & Videos",
                                 f"{m['name']} {t['label']} Pornos") if len(x) <= 65 or x.endswith(" Pornos"))
        others = [x for x in collected[slug].get("themes", []) if x["slug"] != t["slug"]]
        write(info["url"].strip("/") + "/index.html", tpl.render(
            **ctx, m=m, model_slug=slug, theme=t, vids=vids, c=c, others=others,
            model_link=deeplink(cfg, f"/de/amateur/{platform(m)}/videos/"), canonical=base + info["url"], robots="index,follow",
            title=title, description=(c.get("meta_description") or f"{m['name']} {t['label']}: {n} Videos bei Visit-X.")[:158],
            og_image=f"{base}/logo.png"))
        urls.append((base + info["url"], max((v.get("published_at", "") for v in vids), default="") or date.today().isoformat()))
    return urls
