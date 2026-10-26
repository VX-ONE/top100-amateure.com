"""Video-Unterseiten der Top-Models.

Ablauf (Befehl `python -m seo.cli videos`, Workflow mittwochs und sonntags):
1. scrape:   Für die ersten N aktiven Models (Reihenfolge in data/models.yaml = Top-Liste) die neuesten Videos
             bei Visit-X einlesen (Titel, Länge, Datum, Beschreibung, Stichworte). Braucht Playwright/Chromium,
             weil die Videoliste erst im Browser entsteht. Freigabe von VX Cash liegt vor (Georg Krause, 20.10.2026).
2. generate: Für neue Videos eigenen, nicht expliziten Text schreiben (KI), eigenes Cover erzeugen.
3. render:   /<model>/videos/ (Übersicht) und /<model>/videos/<id>-<slug>/ (Videoseite) mit Deeplink
             auf das Video bei Visit-X inkl. Partnerdaten und Bonus-Hinweis.
Daten: data/videos/<model-slug>.json, Texte: data/videos/content/<id>.json
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from . import images as imgmod
from . import models as modelmod
from .keywords import blocked_hit, load_blocked, normalize, slugify
from .quality import EXPLICIT as EXPL

VX = "https://www.visit-x.net"


# ---------------------------------------------------------------- Daten

def vdir(cfg: dict) -> Path:
    d = Path(cfg["_data"]) / "videos"
    (d / "content").mkdir(parents=True, exist_ok=True)
    return d


def load(cfg: dict, slug: str) -> dict:
    p = vdir(cfg) / f"{slug}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"videos": []}


def save(cfg: dict, slug: str, data: dict) -> None:
    (vdir(cfg) / f"{slug}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_content(cfg: dict, vid: str) -> dict | None:
    p = vdir(cfg) / "content" / f"{vid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_content(cfg: dict, vid: str, c: dict) -> None:
    (vdir(cfg) / "content" / f"{vid}.json").write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def settings(cfg: dict) -> dict:
    s = {"top_models": 30, "per_model": 30, "new_per_model_per_run": 10, "generate_per_run": 150, "params": "",
         "llm_model": None, "blocked_terms": [], "workers": 6}
    s.update(cfg.get("videos") or {})
    return s


def video_blocked(cfg: dict) -> list[str]:
    """Eigene Sperrliste für Videos (die Keyword-Sperrliste ist für Themenseiten gedacht und hier zu breit)."""
    return [normalize(t) for t in settings(cfg)["blocked_terms"] if normalize(t)]


def model_pages(cfg: dict, store) -> list[tuple[dict, str]]:
    """(Model, Seiten-Slug) für die ersten N aktiven Models, die bei uns schon eine Profilseite haben."""
    pages = store.pages()
    by_name = {p.get("model"): s for s, p in pages.items() if p.get("kind") == modelmod.PAGE_KIND}
    out = []
    for m in modelmod.load_models(cfg):
        if not m["active"]:
            continue
        if m["name"] in by_name:
            out.append((m, by_name[m["name"]]))
        if len(out) >= settings(cfg)["top_models"]:
            break
    return out


def platform(m: dict) -> str:
    return (m.get("platform_name") or m["name"]).lower()


def deeplink(cfg: dict, vx_path: str) -> str:
    p = settings(cfg)["params"]
    return f"{VX}{vx_path}" + (("?" + p.lstrip("?")) if p else "")


def mmss(s: str) -> int:
    m = re.match(r"(\d+):(\d+)(?::(\d+))?", s or "")
    if not m:
        return 0
    a, b, c = m.groups()
    return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)


# ---------------------------------------------------------------- 1. Einlesen

LIST_JS = r"""() => [...document.querySelectorAll('a[href*="/videos/"]')]
  .filter(a => /\/videos\/\d{5,}-/.test(a.getAttribute('href')||''))
  .map(a => ({href: a.getAttribute('href'), text: a.innerText}))"""

DETAIL_JS = r"""() => document.body.innerText"""


def parse_detail(text: str, model_line: str, title: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = {"description": "", "date": "", "tags": []}
    try:
        i = max(i for i, l in enumerate(lines) if l == title)
    except ValueError:
        return out
    rest = lines[i + 1:]
    desc = []
    for j, l in enumerate(rest):
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", l):
            out["date"] = "-".join(reversed(l.split(".")))
            rest = rest[j + 1:]
            break
        desc.append(l)
    out["description"] = " ".join(desc)[:1500]
    if "Stichworte" in rest:
        k = rest.index("Stichworte")
        tags = []
        for l in rest[k + 1:]:
            if l.startswith("Weitere Videos") or l == "Empfohlen":
                break
            tags.append(l)
        out["tags"] = tags[:20]
    return out


def scrape(cfg: dict, store, only: list[str] | None = None, log=print) -> dict:
    """Liest neue Videos ein. Details (Beschreibung, Datum, Stichworte) nur für noch unbekannte Videos."""
    from playwright.sync_api import sync_playwright

    s = settings(cfg)
    blocked = video_blocked(cfg)
    stats = {"models": 0, "neu": 0, "gesperrt": 0, "fehler": 0}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="de-DE", viewport={"width": 1280, "height": 2000})
        for m, slug in model_pages(cfg, store):
            if only and slug not in only:
                continue
            data = load(cfg, slug)
            known = {v["id"] for v in data["videos"]}
            found = []
            try:
                for n in (1, 2, 3):
                    page.goto(f"{VX}/de/amateur/{platform(m)}/videos/?page={n}", wait_until="domcontentloaded", timeout=45000)
                    try:  # Videokacheln werden per JavaScript nachgeladen: warten, bis echte Video-Links da sind
                        page.wait_for_function(
                            "() => document.querySelectorAll('a[href*=\"/videos/\"]').length && "
                            "[...document.querySelectorAll('a[href*=\"/videos/\"]')].filter(a => /\\/videos\\/\\d{5,}-/.test(a.getAttribute('href')||'')).length >= 8",
                            timeout=30000)
                    except Exception:
                        pass
                    page.mouse.wheel(0, 4000)
                    time.sleep(2)
                    for a in page.evaluate(LIST_JS):
                        mt = re.search(r"/videos/(\d+)-([^/]+)/?", a["href"])
                        if not mt or any(f["id"] == mt.group(1) for f in found):
                            continue
                        lines = [l.strip() for l in a["text"].splitlines() if l.strip() and l.strip() != "AB 18"]
                        dur = next((l for l in lines if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", l)), "")
                        title = next((l for l in lines if l not in (dur, m["name"]) and not l.isdigit()
                                      and l.lower() != platform(m)), "")
                        found.append({"id": mt.group(1), "vx_slug": mt.group(2), "title": title, "duration": dur,
                                      "vx_path": a["href"] if a["href"].startswith("/") else re.sub(r"^https?://[^/]+", "", a["href"])})
                    if len(found) >= s["per_model"]:
                        break
            except Exception as e:
                stats["fehler"] += 1
                log(f"  [{slug}] Liste nicht lesbar: {type(e).__name__}: {e}")
                continue
            stats["models"] += 1
            if len(found) < 3 and not stats.get("debug"):
                stats["debug"] = 1
                try:
                    txt = page.evaluate("() => document.body.innerText")
                    log(f"  DEBUG {page.url} | Titel: {page.title()} | Textlänge {len(txt)} | Links: "
                        f"{page.evaluate('() => document.querySelectorAll(\"a\").length')}")
                    log("  DEBUG Text: " + re.sub(r"\s+", " ", txt)[:1500])
                    log("  DEBUG gefunden: " + json.dumps(found[:3], ensure_ascii=False))
                    page.screenshot(path=str(Path(cfg["_reports"]) / "videos-debug.png"))
                except Exception as e:
                    log(f"  DEBUG fehlgeschlagen: {e}")
            added = 0
            for v in found[: s["per_model"]]:
                if v["id"] in known or not v["title"]:
                    continue
                if added >= s["new_per_model_per_run"]:
                    break
                added += 1
                hit = blocked_hit(normalize(v["title"]), blocked)
                if hit:
                    v.update(status="skipped", reason=f"gesperrt: {hit}")
                    stats["gesperrt"] += 1
                else:
                    try:
                        page.goto(VX + v["vx_path"], wait_until="domcontentloaded", timeout=45000)
                        try:
                            page.wait_for_function("() => document.body.innerText.includes('Stichworte')", timeout=20000)
                        except Exception:
                            page.wait_for_timeout(3000)
                        v.update(parse_detail(page.evaluate(DETAIL_JS), m["name"], v["title"]))
                    except Exception as e:
                        log(f"  [{slug}] Video {v['id']}: {type(e).__name__}")
                    hit = blocked_hit(normalize(v["title"] + " " + v.get("description", "") + " " + " ".join(v.get("tags", []))), blocked)
                    v.update(status="skipped", reason=f"gesperrt: {hit}") if hit else v.update(status="new")
                    stats["gesperrt" if hit else "neu"] += 1
                v["seen"] = date.today().isoformat()
                data["videos"].append(v)
            data.update(model=m["name"], platform=platform(m), updated=date.today().isoformat())
            save(cfg, slug, data)
            log(f"  [{slug}] {len(found)} Videos in der Liste, gespeichert: {len(data['videos'])}")
        browser.close()
    return stats


VXQL_QUERY = ("query VideoSearch($first:Int,$offset:Int,$order:SearchOrder,$filter:SearchFilter){search{videos("
              "search:\"\",first:$first,offset:$offset,order:$order,filter:$filter){total items{video{id title duration "
              "linkVX(internal:true) ageRating model{id name}}}}}}")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")


def _http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 40) -> str:
    import urllib.request
    h = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9", "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _secs(n) -> str:
    n = int(n or 0)
    h, rest = divmod(n, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def api_videos(name_lc: str, first: int = 30, model_id: int | None = None, offset: int = 0) -> tuple[int | None, list[dict]]:
    """Neueste Videos eines Models über die Datenschnittstelle der Visit-X-Seite (wie der Browser sie lädt).
    Gibt (Model-ID, Liste) zurück. Speichert nur ID, Titel, Länge und Link – keine Profildaten."""
    html = _http(f"{VX}/de/amateur/{name_lc}/videos/")
    at = re.search(r'"accessToken":"([^"]+)"', html)
    vt = re.search(r'"vxqlAccessToken":"([^"]+)"', html)
    if not vt:
        raise RuntimeError("kein Zugangstoken in der Seite")
    if not model_id:
        if not at:
            raise RuntimeError("keine Model-ID ermittelbar")
        actor = json.loads(_http(f"{VX}/rest/v1/actor/getActorDataByIdOrName/{name_lc}?language=de&access_token={at.group(1)}"))
        model_id = int((actor.get("actorData") or {}).get("id") or 0) or None
        if not model_id:
            raise RuntimeError("Model-ID nicht gefunden")
    body = json.dumps({"query": VXQL_QUERY, "variables": {"first": first, "offset": offset, "order": "newest",
                       "filter": {"includedModelIds": [model_id]}}}).encode()
    res = json.loads(_http(f"{VX}/vxql?language=de", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {vt.group(1)}",
        "Origin": VX, "Referer": f"{VX}/de/amateur/{name_lc}/videos/"}))
    items = (((res.get("data") or {}).get("search") or {}).get("videos") or {}).get("items") or []
    out = []
    for it in items:
        v = it.get("video") or {}
        link = v.get("linkVX") or ""
        mt = re.search(r"/videos/(\d+)-([^/?#]+)/?", link)
        if not mt or not v.get("title"):
            continue
        out.append({"id": str(v["id"]), "vx_slug": mt.group(2), "title": v["title"].strip(), "duration": _secs(v.get("duration")),
                    "vx_path": re.sub(r"^https?://[^/]+", "", link).split("?")[0]})
    return model_id, out


def scrape_api(cfg: dict, store, only: list[str] | None = None, log=print) -> dict:
    """Video-Operator ohne Browser: fragt je Top-Model die neuesten Videos ab und legt neue als 'new' an."""
    s = settings(cfg)
    blocked = video_blocked(cfg)
    stats = {"models": 0, "neu": 0, "gesperrt": 0, "fehler": 0, "quelle": "api"}
    for m, slug in model_pages(cfg, store):
        if only and slug not in only:
            continue
        data = load(cfg, slug)
        try:
            mid, found = api_videos(platform(m), first=s["per_model"], model_id=data.get("vx_model_id"))
        except Exception as e:
            stats["fehler"] += 1
            log(f"  [{slug}] Schnittstelle nicht lesbar: {type(e).__name__}: {e}")
            continue
        stats["models"] += 1
        data["vx_model_id"] = mid
        known = {v["id"] for v in data["videos"]}
        cap = s["per_model"] if not known else s["new_per_model_per_run"]   # neues Model: gleich alle 30 übernehmen
        added = 0
        for v in found:
            if v["id"] in known:
                continue
            if added >= cap:
                break
            added += 1
            hit = blocked_hit(normalize(v["title"]), blocked)
            v.update(status="skipped", blocked=hit) if hit else v.update(status="new")
            stats["gesperrt" if hit else "neu"] += 1
            v["seen"] = date.today().isoformat()
            data["videos"].append(v)
        data.update(model=m["name"], platform=platform(m), updated=date.today().isoformat())
        save(cfg, slug, data)
        log(f"  [{slug}] {len(found)} Videos abgefragt, neu: {added}")
        time.sleep(1.5)   # schonend: eine Anfrage pro Sekunde
    return stats


def backfill(cfg: dict, store, log=print) -> dict:
    """Ältere Videos der Top-Models schrittweise aufnehmen (chronologisch: von neu nach alt).
    1) Einmalig je Model bis zu backfill_max Videos als 'backlog' vormerken.
    2) Täglich backfill_per_day davon reihum (je Model das jeweils neueste noch offene) auf 'new' setzen.
    Automatische Bremse: sind ältere Backfill-Seiten schlecht indexiert, wird das Tagespensum gesenkt."""
    s = settings(cfg)
    n_models, cap, per_day = int(s.get("backfill_models", 0)), int(s.get("backfill_max", 0)), int(s.get("backfill_per_day", 0))
    stats = {"vorgemerkt": 0, "freigegeben": 0, "gesperrt": 0, "offen": 0, "pensum": per_day}
    if not (n_models and cap and per_day):
        return stats
    blocked = video_blocked(cfg)
    models = model_pages(cfg, store)[:n_models]
    datas = {}
    for m, slug in models:
        data = load(cfg, slug)
        datas[slug] = data
        if data.get("backlog_done"):
            continue
        known = {v["id"] for v in data["videos"]}
        mid, offset, added = data.get("vx_model_id"), 0, 0
        try:
            while offset < cap:
                mid, chunk = api_videos(platform(m), first=min(50, cap - offset), model_id=mid, offset=offset)
                if not chunk:
                    break
                for v in chunk:
                    if v["id"] not in known:
                        v.update(status="backlog", source="backfill", seen=date.today().isoformat())
                        data["videos"].append(v)
                        known.add(v["id"])
                        added += 1
                offset += len(chunk)
                time.sleep(1.2)
        except Exception as e:
            log(f"  [{slug}] Backlog nicht vollständig lesbar: {type(e).__name__}: {e}")
            save(cfg, slug, data)
            continue
        data["vx_model_id"], data["backlog_done"] = mid, date.today().isoformat()
        save(cfg, slug, data)
        stats["vorgemerkt"] += added
        log(f"  [{slug}] Backlog: {added} ältere Videos vorgemerkt")
    # Bremse: Indexierungsquote älterer Backfill-Seiten (mind. 10 Tage alt, von der Search Console geprüft)
    old = (date.today() - timedelta(days=10)).isoformat()
    checked = [v for d in datas.values() for v in d["videos"]
               if v.get("source") == "backfill" and v.get("status") == "published" and (v.get("published_at") or "9") <= old and "indexed" in v]
    if len(checked) >= 40:
        ratio = sum(1 for v in checked if v["indexed"]) / len(checked)
        if ratio < 0.4:
            per_day = max(25, per_day // 5)
            log(f"  Backfill gebremst: nur {ratio:.0%} der älteren Backfill-Seiten indexiert -> {per_day} pro Tag")
    stats["pensum"] = per_day
    # Reihum freigeben, je Model das neueste noch offene Video zuerst
    queues = []
    for m, slug in models:
        q = sorted((v for v in datas[slug]["videos"] if v.get("status") == "backlog"), key=lambda v: -int(v["id"]))
        if q:
            queues.append((slug, q))
    touched = set()
    while stats["freigegeben"] + stats["gesperrt"] < per_day and any(q for _, q in queues):
        for slug, q in queues:
            if not q or stats["freigegeben"] + stats["gesperrt"] >= per_day:
                continue
            v = q.pop(0)
            hit = blocked_hit(normalize(v["title"]), blocked)
            if hit:
                v.update(status="skipped", blocked=hit)
                stats["gesperrt"] += 1
            else:
                v["status"] = "new"
                stats["freigegeben"] += 1
            touched.add(slug)
    for slug in touched:
        save(cfg, slug, datas[slug])
    stats["offen"] = sum(len(q) for _, q in queues)
    return stats


# ---------------------------------------------------------------- 2. Texte

VIDEO_SYSTEM = """Du bist SEO-Redakteur einer deutschen Informationsseite über Amateur-Models bei Visit-X (nur für Erwachsene).
Du schreibst eine eigenständige Begleitseite zu EINEM Video eines Models. Die Seite soll für Suchen wie
"<Model> Video", "<Model> Videos", "<Model> <Videotitel>" ranken und einzigartig sein (kein Duplicate Content).

Harte Regeln:
1. Nicht explizit: keine Beschreibung sexueller Handlungen, keine Vulgärwörter, keine Körperbeschreibungen. Den Inhalt
   des Videos ordnest du nur allgemein ein (Stimmung, Setting, Format, Länge), so wie ein Programmhinweis.
2. Nichts erfinden: Nur Angaben aus den DATEN verwenden (Titel, Länge, Datum, Model-Fakten, Anbieter-Fakten).
   Keine Angaben zu Alter, Wohnort, Privatleben.
3. Die Originalbeschreibung NICHT übernehmen oder nah umschreiben, sondern komplett eigene Sätze.
4. Keine Links, kein HTML, Markdown nur für **fett** und Listen. Keine Überschriften im Fließtext.
5. Den Bühnennamen immer exakt so schreiben, wie in den DATEN angegeben.
6. Suchbegriffe nie fett setzen oder als Suchphrase zitieren; keine Sätze wie "Wer nach X sucht".
Antworte nur über das Werkzeug "antwort"."""

VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "meta_description": {"type": "string", "description": "120-155 Zeichen, nennt Model, ein Themen-Stichwort, Länge und Nutzen, nicht explizit"},
        "intro": {"type": "string", "description": "2-3 Sätze, erster Satz enthält '<Bühnenname> Video'"},
        "sections": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "object", "properties": {
            "h2": {"type": "string", "description": "enthält den Bühnennamen"}, "body": {"type": "string", "description": "60-120 Wörter"}},
            "required": ["h2", "body"]}},
        "faq": {"type": "array", "minItems": 3, "maxItems": 4, "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string"}}, "required": ["q", "a"]}},
        "cover_line": {"type": "string", "description": "Kurzer, nicht expliziter Cover-Text, max. 26 Zeichen"},
    },
    "required": ["meta_description", "intro", "sections", "faq", "cover_line"],
}


VULGAR = re.compile(r"(?<![\wäöüß])(cum\w*|creampie\w*|anal\w*|squirt\w*|dildo\w*|bbc|bwc|pussy|fuck\w*|sex\w*|porno\w*|"
                    r"gangbang\w*|orgie\w*|deepthroat|facial|footjob|blowjob|handjob|joi|cei|wix\w*|schlampe\w*|hure\w*|"
                    r"l[oö]ch\w*|nass\w*|feucht\w*|spritz\w*|besam\w*|lesbo\w*|strap\w*|dreier|vierer|milf\w*|notgeil\w*|"
                    r"versaut\w*|pervers\w*|zerst[oö]r\w*|gnadenlos|hart|heftig\w*|xxx*l|xl|omg|wtf|ao|dp|pov|uncut|ladung\w*|"
                    r"sahne|eier|kehlen\w*|rachen\w*|dirty\w*|talk|fist\w*|plug\w*|toy\w*|vibrator|nackt\w*|cock|dick|ass|"
                    r"bitch\w*|slut\w*|lick|suck|stöpsel|stopf\w*|reit\w*|ritt|benutz\w*|abgeschleppt|fremdfick\w*|fremdgeh\w*|"
                    r"fremdgefickt|prügel|zerfickt|weggefickt|durchgefickt)(?![\wäöüß])", re.I)
STOP = {"und", "oder", "mit", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einem", "einen", "einer", "mich", "mir",
        "mein", "meine", "meinen", "meinem", "dich", "dir", "dein", "deine", "ich", "du", "er", "sie", "es", "wir", "ihr", "uns",
        "ist", "sind", "war", "wird", "werden", "hat", "haben", "bis", "zum", "zur", "vom", "von", "auf", "aus", "für", "fuer",
        "nach", "vor", "bei", "im", "in", "am", "an", "als", "wie", "was", "wer", "wenn", "dann", "noch", "nur", "so", "statt",
        "nicht", "kein", "keine", "jetzt", "endlich", "einfach", "total", "mega", "super", "krass", "komplett", "richtig",
        "the", "and", "with", "my", "me", "you", "your", "for", "of", "to", "on", "in", "part", "teil", "classic", "titel",
        "ganz", "sein", "seine", "seinen", "seinem", "ihre", "ihren", "ihrem", "beim", "lass", "darf", "will", "geht", "gehts",
        "eskaliert", "eskalation", "minuten", "min", "stunden", "special", "best", "of", "neue", "neuer", "neues", "erste",
        "ersten", "erster", "erstes", "mal", "voll", "volle", "kann", "habe", "keinen", "aber", "sorry", "lässt", "sich", "hier",
        "völlig", "mehr", "gehen", "endet", "fürs", "inkl", "während", "heißer", "heisse", "heiße", "dicker", "saftige",
        "lecke", "jähriger", "durchgenommen", "flutet", "privater", "wieder", "immer", "alles", "alle", "jeden", "jeder",
        "dreist", "dreister", "dreiste", "spontan", "spontaner", "spontane", "macht", "machen", "bringt", "kommt", "gibt"}
ACRONYMS = {"BBC", "BWC", "XXL", "XXXL", "XL", "JOI", "AO", "POV", "DP", "MFF", "OMG", "WTF", "ONS", "OP", "US", "FKK", "SOS",
            "WG", "DVD", "ASMR", "MILF", "MILFS", "XMAS", "GFE", "CEI", "DT", "BTS"}
ROOTS = ("schwanz", "fick", "fotz", "titt", "sperma", "musch", "arsch", "geil", "wichs", "wixx", "pimmel", "schlamp", "porno",
         "cum", "sex", "orgas", "leck", "spritz", "pussy", "fuck", "squirt", "anal", "creampie", "blas", "notgeil", "hure")
TAIL_STOP = {"und", "oder", "mit", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einem", "einen", "einer", "in", "im",
             "am", "an", "auf", "aus", "für", "zum", "zur", "vom", "von", "bis", "&", "+", "-", "–", "sich", "lässt", "ist",
             "war", "wird", "mich", "mir", "ich", "du", "mein", "meine", "meinen", "dein", "deine", "sein", "seine", "ihr", "ihre",
             "uns", "unser", "geilem", "geilen", "noch", "nicht", "so", "wie", "beim", "bei", "nach", "vor", "als", "es", "er", "sie"}


def clean_title(t: str) -> str:
    """Videotitel lesbar machen: Emojis/Sonderzeichen raus, Satzzeichen bündeln, GROSSBUCHSTABEN normalisieren."""
    t = re.sub(r"[^\w\s.,:;!?&+#'’´\-–/()%€]", "", t, flags=re.U)
    t = re.sub(r"([!?.])[!?.]+", r"\1", t)
    t = re.sub(r"\s*/+\s*", " – ", t).strip(" /-–")
    words = []
    for w in t.split():
        core = re.sub(r"\W", "", w)
        if core.isupper() and core not in ACRONYMS and not re.search(r"\d", core) and len(core) > 1:
            w = w[0] + w[1:].lower() if (w[0].isalpha() and (len(core) > 3 or not words)) else w.lower()
        words.append(w)
    t = re.sub(r"\s+", " ", " ".join(words)).strip()
    t = re.sub(r"\s+([!?.,:;])", r"\1", t)
    return (t[0].upper() + t[1:]) if t else "Video"


def trim(t: str, limit: int) -> str:
    t = (t or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(".,:;–- ") + " …"


def title_core(t: str, limit: int) -> str:
    t = clean_title(t).rstrip(".,:;–- ")
    if len(t) <= limit:
        return t
    ws = t[:limit + 1].split(" ")
    ws = ws[:-1] if len(ws) > 1 else ws
    while len(ws) > 2 and ws[-1].lower().strip(".,:;!?") in TAIL_STOP:
        ws.pop()
    return " ".join(ws).rstrip(".,:;–-& ") or t[:limit]


def seo_title(m: dict, v: dict) -> str:
    suffix = f" – {m['name']} Video"
    t = title_core(v["title"], max(24, 68 - len(suffix))) + suffix
    return t + " bei Visit-X" if len(t) < 40 else t


def topic_hints(title: str, name: str) -> list[str]:
    own = {normalize(x) for x in re.split(r"[\s\-_]+", name)} | {normalize(name)}
    out = []
    for w in re.findall(r"[A-Za-zÄÖÜäöüß][\w\-’´']{2,}", clean_title(title)):
        lw = w.lower().strip("-’´'")
        if len(lw) < 4 or lw in STOP or normalize(lw) in own or VULGAR.fullmatch(lw) or EXPL.fullmatch(lw):
            continue
        if VULGAR.search(lw) or EXPL.search(lw) or any(r in lw for r in ROOTS):
            continue
        if lw not in [o.lower() for o in out]:
            out.append(w if not w.isupper() else w.capitalize())
    return out[:6]


def build_prompt(m: dict, v: dict, facts: str) -> str:
    hints = topic_hints(v["title"], m["name"])
    meta = f"Länge: {v.get('duration') or 'unbekannt'} Minuten | mit Ton"
    if v.get("date"):
        meta += f" | veröffentlicht am {v['date']}"
    return "\n".join([
        f"Bühnenname: {m['name']}",
        f"Videotitel (Original, nur als Bezug, nicht zitieren): {v['title']}",
        "Neutrale Themen-Stichworte aus dem Titel: " + (", ".join(hints) if hints else
            "keine – dann Format, Länge und Stil des Models einordnen"),
        meta,
        "Model-Fakten:\n- " + "\n- ".join(m["facts"][:8]),
        "Anbieter-Fakten:\n" + facts,
        "WICHTIG für Einzigartigkeit: Greife die Themen-Stichworte (Setting, Ort, Anlass, Rollenspiel, Outfit) in neutralen Worten "
        "auf. Intro und erster Abschnitt müssen sich klar auf genau dieses Video beziehen, nicht allgemein auf das Model. "
        "Die erste H2 enthält den Bühnennamen und ein Themen-Stichwort (falls vorhanden).",
        "Abschnitte: 1) Thema und Einordnung dieses Videos (Stichworte, Länge, Format), 2) 'So schaust du das <Name> Video bei Visit-X' "
        "(Euro werden in Tokens umgerechnet, Zahlungsarten, kostenlos anmelden: über unseren Link ist der Gutscheincode für den "
        "100 % Aufladebonus automatisch hinterlegt), 3) optional 'Mehr <Name> Videos und Livecam'.",
        "Stief-/Familienbegriffe im Titel sind Rollenspiele unter erwachsenen Darstellern: als 'Rollenspiel' oder 'Tabu-Fantasie' "
        "einordnen, keine echte Verwandtschaft behaupten.",
        "Nicht schreiben: dass ein Datum unbekannt ist, 'laut Seitenbetreiber', 'diese Seite erklärt'. Keine Floskeln.",
        "FAQ (3-4): z. B. Wie lang ist das <Name> Video zu <Stichwort>? Wo kann ich es ansehen? Was kostet es? Gibt es mehr Videos von <Name>?",
    ])


def mock_content(m: dict, v: dict) -> dict:
    n = m["name"]
    body = (f"Dieses {n} Video ist {v.get('duration') or 'einige Minuten'} lang. " * 12).strip()
    return {"meta_description": f"{n} Video ({v.get('duration')}) bei Visit-X: Infos, Länge und so schaust du es mit Bonus.",
            "intro": f"Dieses {n} Video stellen wir dir hier vor.", "cover_line": "Neues Video",
            "sections": [{"h2": f"Worum es im {n} Video geht", "body": body}, {"h2": f"So schaust du das {n} Video", "body": body}],
            "faq": [{"q": "Wie lang ist das Video?", "a": f"{v.get('duration')} Minuten."}] * 3}


def check(c: dict, m: dict, v: dict) -> list[str]:
    from . import quality
    issues = []
    text = " ".join([c.get("meta_description", ""), c.get("intro", "")] +
                    [s["body"] + " " + s["h2"] for s in c.get("sections", [])] + [f["q"] + " " + f["a"] for f in c.get("faq", [])])
    if quality.EXPLICIT.search(text):
        issues.append("explizite Wörter")
    words = len(text.split())
    if words < 220:
        issues.append(f"zu kurz ({words} Wörter)")
    if normalize(m["name"]).replace("-", " ") not in normalize(c.get("seo_title", "")).replace("-", " ") and \
            normalize(m["name"]).replace("-", "") not in normalize(c.get("seo_title", "")).replace("-", ""):
        issues.append("Name fehlt im Titel")
    desc = normalize(v.get("description", ""))
    for chunk in re.findall(r"(?:\S+\s+){7}\S+", desc)[:30]:   # 8-Wort-Folgen aus der Originalbeschreibung
        if chunk in normalize(text):
            issues.append("Originalbeschreibung übernommen")
            break
    if not 25 <= len(c.get("seo_title", "")) <= 70:
        issues.append("Titellänge")
    return issues


def generate(cfg: dict, store, mock: bool = False, log=print) -> dict:
    from . import generate as gen
    s = settings(cfg)
    facts = gen.load_facts(cfg["_root"])
    stats = {"erzeugt": 0, "verworfen": 0}
    # Reihum über die Models, damit alle früh Videos bekommen (neueste zuerst)
    queues = []
    for m, slug in model_pages(cfg, store):
        data = load(cfg, slug)
        q = [v for v in data["videos"] if v.get("status") == "new"]
        q.sort(key=lambda v: int(v["id"]), reverse=True)   # Video-IDs von Visit-X sind chronologisch
        queues.append((m, slug, data, q))
    budget = s["generate_per_run"]
    llm_cfg = dict(cfg, llm=dict(cfg["llm"], model=s["llm_model"] or cfg["llm"]["model"], max_tokens=8000))
    jobs = []                                  # reihum, neueste zuerst
    while len(jobs) < budget and any(q for *_, q in queues):
        for m, slug, data, q in queues:
            if q and len(jobs) < budget:
                jobs.append((m, slug, data, q.pop(0)))

    def work(job):
        m, slug, data, v = job
        msgs = []
        for attempt in range(2):
            try:
                c = mock_content(m, v) if mock else gen.ask_json(llm_cfg, VIDEO_SYSTEM, build_prompt(m, v, facts), VIDEO_SCHEMA)
                c = gen.clean_content(c) | {"seo_title": seo_title(m, v), "h1": f"{clean_title(v['title'])} – {m['name']} Video",
                                            "cover_line": gen.clean_text(c.get("cover_line", ""))[:26]}
            except Exception as e:
                msgs.append(f"  [{slug}/{v['id']}] Fehler: {type(e).__name__}: {e}")
                continue
            issues = [] if mock else check(c, m, v)
            if not issues:
                return c, msgs
            msgs.append(f"  [{slug}/{v['id']}] verworfen: {', '.join(issues)}")
        return None, msgs

    from concurrent.futures import ThreadPoolExecutor
    started = time.time()
    with ThreadPoolExecutor(max_workers=int(s.get("workers", 6))) as ex:
        for (m, slug, data, v), (content, msgs) in zip(jobs, ex.map(work, jobs)):
            for x in msgs:
                log(x)
            if content:
                save_content(cfg, v["id"], content)
                v["status"] = "published"
                v["published_at"] = date.today().isoformat()
                stats["erzeugt"] += 1
            else:
                v["fails"] = v.get("fails", 0) + 1
                if v["fails"] >= 3:
                    v["status"] = "failed"
                stats["verworfen"] += 1
            save(cfg, slug, data)
    log(f"  {len(jobs)} Videos in {int(time.time() - started)} s bearbeitet")
    return stats


# ---------------------------------------------------------------- 3. Seiten

def video_svg(name: str, line: str, duration: str) -> str:
    safe = lambda s, n: re.sub(r"[<>&\"']", "", s)[:n]
    nm, ln, du = safe(name, 24), safe(line, 26), safe(duration, 8)
    size = 64 if len(ln) <= 18 else 52
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630">'
            '<defs><linearGradient id="vg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1e1b4b"/>'
            '<stop offset="1" stop-color="#be123c"/></linearGradient></defs>'
            '<rect width="1200" height="630" fill="url(#vg)"/>'
            '<circle cx="1040" cy="110" r="230" fill="#fff" fill-opacity=".05"/>'
            '<circle cx="600" cy="250" r="110" fill="#fff" fill-opacity=".16" stroke="#fff" stroke-width="6"/>'
            '<path d="M565 190 L565 310 L670 250 Z" fill="#fff"/>'
            '<rect x="60" y="50" width="200" height="46" rx="23" fill="#fff"/>'
            '<text x="160" y="82" text-anchor="middle" font-size="24" font-weight="800" fill="#be123c" letter-spacing="3" font-family="system-ui,Arial,sans-serif">VIDEO</text>'
            f'<rect x="1000" y="50" width="140" height="46" rx="10" fill="#000" fill-opacity=".45"/>'
            f'<text x="1070" y="82" text-anchor="middle" font-size="26" font-weight="700" fill="#fff" font-family="system-ui,Arial,sans-serif">{du}</text>'
            f'<text x="600" y="455" text-anchor="middle" font-size="{size}" font-weight="800" fill="#fff" font-family="system-ui,Arial,sans-serif">{ln}</text>'
            f'<text x="600" y="530" text-anchor="middle" font-size="36" font-weight="600" fill="#fff" fill-opacity=".85" font-family="system-ui,Arial,sans-serif">{nm} · Top-Model bei Visit-X</text>'
            '</svg>')


def page_slug(v: dict) -> str:
    return f"{v['id']}-{slugify(v['vx_slug'].replace('--', '-'))[:70]}"


def collect(cfg: dict, store) -> dict[str, dict]:
    """Veröffentlichte Videos je Model-Seiten-Slug, für Templates und Model-Seiten."""
    out = {}
    for m, slug in model_pages(cfg, store):
        vids = []
        for v in load(cfg, slug)["videos"]:
            if v.get("status") != "published":
                continue
            c = load_content(cfg, v["id"])
            if not c:
                continue
            vids.append({**v, "title": clean_title(v["title"]), "c": c, "url": f"/{slug}/videos/{page_slug(v)}/", "img": f"/img/v/{v['id']}.svg",
                         "link": deeplink(cfg, v["vx_path"]), "secs": mmss(v.get("duration", ""))})
        vids.sort(key=lambda x: (x.get("date", ""), int(x["id"])), reverse=True)
        seen = {}
        for x in vids:   # Seitentitel immer frisch bilden; doppelte (z. B. Serien) -> vollen Titel verwenden
            seen.setdefault(seo_title(m, x), []).append(x)
        for t, xs in seen.items():
            for x in xs:
                x["title_tag"] = t if len(xs) == 1 else f"{clean_title(x['title'])} – {m['name']} Video"
        if vids:
            out[slug] = {"model": m, "videos": vids}
    return out


def render(cfg: dict, store, env, ctx: dict, write, site_dir: Path, base: str, collected: dict) -> list[tuple[str, str]]:
    urls = []
    vt, it = env.get_template("video.html"), env.get_template("videos_index.html")
    (site_dir / "img" / "v").mkdir(parents=True, exist_ok=True)
    for slug, d in collected.items():
        m, vids = d["model"], d["videos"]
        for i, v in enumerate(vids):
            (site_dir / "img" / "v" / f"{v['id']}.svg").write_text(
                video_svg(m["name"], v["c"].get("cover_line") or "Neues Video", v.get("duration", "")), encoding="utf-8")
            n = len(vids)   # rotierendes Fenster: jedes Video bekommt Links von seinen Nachbarn
            related = [vids[(i + k) % n] for k in range(1, min(7, n))]
            prev_v, next_v = (vids[i - 1] if i > 0 else None), (vids[i + 1] if i + 1 < n else None)
            write(v["url"].strip("/") + "/index.html", vt.render(
                **ctx, v=v, c=v["c"], m=m, model_slug=slug, related=related, prev_v=prev_v, next_v=next_v, canonical=base + v["url"],
                robots="index,follow", title=v["title_tag"], description=trim(v["c"]["meta_description"], 158),
                og_image="", og_type="article"))
            urls.append((base + v["url"], v.get("published_at") or date.today().isoformat()))
        per = int(settings(cfg).get("index_per_page", 30))
        pages_n = max(1, -(-len(vids) // per))
        for pno in range(1, pages_n + 1):
            chunk = vids[(pno - 1) * per: pno * per]
            path = f"{slug}/videos/" + (f"seite/{pno}/" if pno > 1 else "")
            prev_url = None if pno == 1 else (f"/{slug}/videos/" if pno == 2 else f"/{slug}/videos/seite/{pno - 1}/")
            next_url = f"/{slug}/videos/seite/{pno + 1}/" if pno < pages_n else None
            suffix = f" – Seite {pno}" if pno > 1 else ""
            write(path + "index.html", it.render(
                **ctx, m=m, model_slug=slug, vids=chunk, total=len(vids), page_no=pno, pages_n=pages_n, offset=(pno - 1) * per,
                prev_url=prev_url, next_url=next_url, model_link=deeplink(cfg, f"/de/amateur/{platform(m)}/videos/"),
                canonical=f"{base}/{path}", robots="index,follow",
                title=f"{m['name']} Videos: alle {len(vids)} Clips im Überblick{suffix}",
                description=(f"Alle {len(vids)} {m['name']} Videos bei Visit-X, neueste zuerst: Titel und Länge im Überblick{suffix}. "
                             f"Direkt ansehen mit 100 % Aufladebonus.")[:158],
                og_image=""))
            urls.append((f"{base}/{path}", max((v.get("published_at", "") for v in chunk), default="") or date.today().isoformat()))
    if collected:   # Gesamtübersicht /videos/
        allv = sorted((dict(v, model=d["model"]["name"]) for d in collected.values() for v in d["videos"]),
                      key=lambda v: int(v["id"]), reverse=True)
        models = [{"name": d["model"]["name"], "slug": sl, "count": len(d["videos"]), "img": f"/img/{sl}.svg"}
                  for sl, d in collected.items()]
        write("videos/index.html", env.get_template("videos_hub.html").render(
            **ctx, vids=allv[:48], models=models, total=len(allv), canonical=f"{base}/videos/", robots="index,follow",
            title="Amateur Videos der Top-Models bei Visit-X: neue Clips",
            description=f"{len(allv)} Amateur-Videos der Top-Models bei Visit-X: neueste Clips, Länge und direkter Link. "
                        "Mit 100 % Aufladebonus über unseren Link.", og_image=""))
        urls.append((f"{base}/videos/", date.today().isoformat()))
    return urls
