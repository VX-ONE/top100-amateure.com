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
              "description tags released linkVX(internal:true) ageRating model{id name}}}}}}")
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
                    "vx_path": re.sub(r"^https?://[^/]+", "", link).split("?")[0],
                    "description": re.sub(r"https?://\S+", "", (v.get("description") or "")).strip()[:3000],
                    "tags": [t.strip() for t in (v.get("tags") or []) if isinstance(t, str) and t.strip()][:25],
                    "date": (v.get("released") or "")[:10]})
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
            hit = blocked_hit(normalize(v["title"] + " " + v.get("description", "")), blocked)
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
    # Bremse: Indexierungsquote aller Videoseiten, die mind. 10 Tage alt und von der Search Console geprüft sind,
    # plus Seiten, die in den letzten 7 Tagen aus dem Index gefallen sind. Videoseiten bringen den Traffic -> lieber langsamer.
    old = (date.today() - timedelta(days=10)).isoformat()
    week = (date.today() - timedelta(days=7)).isoformat()
    allv = [v for f in vdir(cfg).glob("*.json") for v in json.loads(f.read_text(encoding="utf-8")).get("videos", [])]
    checked = [v for v in allv if v.get("status") == "published" and (v.get("published_at") or "9") <= old and "indexed" in v]
    drops = sum(1 for v in allv if (v.get("dropped_at") or "") >= week)
    if len(checked) >= 50:
        ratio = sum(1 for v in checked if v["indexed"]) / len(checked)
        stats["indexquote"] = round(ratio, 2)
        if ratio < 0.4:
            per_day = 0
            log(f"  Backfill PAUSIERT: nur {ratio:.0%} der Videoseiten (>=10 Tage) indexiert")
        elif ratio < 0.6:
            per_day = min(per_day, 50)
            log(f"  Backfill gebremst: {ratio:.0%} der Videoseiten (>=10 Tage) indexiert -> {per_day} pro Tag")
    if drops >= 10 and per_day > 50:
        per_day = 50
        log(f"  Backfill gebremst: {drops} Videoseiten in 7 Tagen aus dem Index gefallen -> {per_day} pro Tag")
    stats["pensum"] = per_day
    # Tagespensum gilt pro Kalendertag, auch wenn der Operator mehrfach läuft
    state_f = vdir(cfg) / "_backfill_state.json"
    try:
        st = json.loads(state_f.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    done_today = st.get("count", 0) if st.get("date") == date.today().isoformat() else 0
    per_day = max(0, per_day - done_today)
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
            hit = blocked_hit(normalize(v["title"] + " " + v.get("description", "")), blocked)
            if hit:
                v.update(status="skipped", blocked=hit)
                stats["gesperrt"] += 1
            else:
                v["status"] = "new"
                stats["freigegeben"] += 1
            touched.add(slug)
    for slug in touched:
        save(cfg, slug, datas[slug])
    state_f.write_text(json.dumps({"date": date.today().isoformat(),
                                   "count": done_today + stats["freigegeben"] + stats["gesperrt"]}), encoding="utf-8")
    stats["offen"] = sum(len(q) for _, q in queues)
    return stats


def enrich(cfg: dict, store, log=print, max_models: int = 60) -> dict:
    """Ergänzt Beschreibung, Stichworte und Datum für bereits erfasste Videos (für einzigartige Texte und Sortierung).
    Prüft dabei die Sperrliste erneut mit Titel + Beschreibung; betroffene Seiten werden zurückgezogen."""
    blocked = video_blocked(cfg)
    stats = {"models": 0, "ergaenzt": 0, "zurueckgezogen": 0}
    for m, slug in model_pages(cfg, store)[:max_models]:
        data = load(cfg, slug)
        missing = {v["id"] for v in data["videos"] if "description" not in v and v.get("status") in ("published", "new", "backlog")}
        if not missing:
            continue
        mid, offset, changed = data.get("vx_model_id"), 0, False
        try:
            while missing and offset < 1500:
                mid, chunk = api_videos(platform(m), first=50, model_id=mid, offset=offset)
                if not chunk:
                    break
                by = {v["id"]: v for v in chunk}
                for v in data["videos"]:
                    if v["id"] in missing and v["id"] in by:
                        src = by[v["id"]]
                        v["description"], v["tags"] = src["description"], src["tags"]
                        if src["date"]:
                            v["date"] = src["date"]
                        missing.discard(v["id"])
                        stats["ergaenzt"] += 1
                        changed = True
                        hit = blocked_hit(normalize(v["title"] + " " + v["description"]), blocked)
                        if hit and v.get("status") in ("published", "new", "backlog"):
                            v.update(status="withdrawn" if v["status"] == "published" else "skipped", blocked=hit)
                            stats["zurueckgezogen"] += 1
                offset += len(chunk)
                time.sleep(1.2)
        except Exception as e:
            log(f"  [{slug}] Anreichern unvollständig: {type(e).__name__}: {e}")
        for v in data["videos"]:          # nicht mehr auffindbare Videos nicht erneut anfragen
            if v["id"] in missing:
                v.setdefault("description", "")
        data["vx_model_id"] = mid
        if changed or missing:
            save(cfg, slug, data)
        stats["models"] += 1
    return stats


# ---------------------------------------------------------------- 2. Texte

VIDEO_SYSTEM = """Du bist Redakteur einer deutschen Erotik-Infoseite (nur für Erwachsene) und schreibst eine eigenständige
Begleitseite zu EINEM Video eines Amateur-Models bei Visit-X. Die Seite soll für "<Model> <Videotitel>" und "<Model> Video"
ranken und für Google einen eigenen, hilfreichen Wert haben (kein Thin Content, kein Duplicate Content).

Stil: prickelnd, verführerisch, mit Kopfkino und Augenzwinkern – wie ein guter Teaser. Konkret zu DIESEM Video:
Setting, Ausgangssituation, Rollen/Story, Stimmung, Outfit, Ort, Tempo, Format. Abwechslungsreiche Sätze, keine Floskeln.

Harte Regeln:
1. Andeuten statt beschreiben: keine expliziten Wörter, keine Schilderung sexueller Handlungen, keine Körperteile,
   keine Vulgärsprache. Erotik entsteht über Stimmung, Spannung und Situation.
2. Nichts erfinden: Inhalt nur aus Titel, BESCHREIBUNG und Stichworten; Angebotsfakten nur aus den DATEN.
   Keine Angaben zu Alter, Wohnort, Privatleben, echten Namen.
3. Die BESCHREIBUNG ist nur Kontext: vollständig in eigenen Worten, niemals Sätze oder Satzteile übernehmen.
4. Familienbegriffe sind Rollenspiele erwachsener Darsteller – so einordnen.
5. Vorgegebene Struktur (Abschnitte, Tipp, FAQ-Themen, Einstieg) genau einhalten; jede H2 enthält den Bühnennamen
   oder einen konkreten Bezug zum Video. Keine Standard-Sätze über Zahlungsarten/Anmeldung im Fließtext,
   das steht schon im Button-Bereich – außer der Tipp verlangt es.
6. Bühnennamen exakt wie angegeben. Suchbegriffe nie fett, keine Sätze wie "Wer nach X sucht".
7. Keine Links, kein HTML, Markdown nur für Listen.
Antworte nur über das Werkzeug "antwort"."""

VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "meta_description": {"type": "string", "description": "130-155 Zeichen, konkret zum Video (Setting/Story), nennt Model und Länge, neugierig machend, nicht explizit"},
        "intro": {"type": "string", "description": "2-3 Sätze im vorgegebenen Einstiegs-Stil; erster Satz enthält den Bühnennamen und einen konkreten Bezug zum Video"},
        "highlights": {"type": "array", "minItems": 3, "maxItems": 4, "items": {"type": "string"},
                       "description": "Kurz notiert: 3-4 knappe Stichpunkte (je max. 12 Wörter) zu Setting, Stimmung, Format dieses Videos"},
        "sections": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "object", "properties": {
            "h2": {"type": "string"}, "body": {"type": "string", "description": "70-130 Wörter, konkret zu diesem Video"}},
            "required": ["h2", "body"]}},
        "tip": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string", "description": "40-70 Wörter"}},
                "required": ["title", "body"]},
        "faq": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string", "description": "1-3 Sätze"}}, "required": ["q", "a"]}},
        "cover_line": {"type": "string", "description": "Kurzer, nicht expliziter Cover-Text zum Video, max. 26 Zeichen"},
    },
    "required": ["meta_description", "intro", "highlights", "sections", "tip", "faq", "cover_line"],
}
VIDEO_VERSION = 2

ARCHETYPES = [
    "Worum es in diesem {n} Video geht – Ausgangssituation und Story in eigenen Worten",
    "Stimmung und Szene – Ort, Licht, Outfit, Tempo, wie die Spannung aufgebaut wird",
    "Für wen sich das Video lohnt – welche Vorlieben und Fantasien es bedient (angedeutet)",
    "Was diesen Clip von {n} besonders macht – Idee, Rolle, Twist, Persönlichkeit",
    "Hinter dem Titel – was der Titel verspricht und wie das Video es einlöst",
    "Länge, Format und Rhythmus – kurzer Teaser oder ausgedehnter Film, Kamera, Perspektive",
    "Rollenspiel und Charaktere – wer spielt wen, welche Dynamik (nur wenn im Video vorhanden, sonst Story)",
    "{n} in diesem Video – wie sie/er sich hier zeigt, passend zu den Model-Fakten",
]
TIPS = [
    "Bonus: über unseren Link ist der Gutscheincode für 100 % Aufladebonus automatisch hinterlegt – einfach kostenlos anmelden",
    "Bezahlen: PayPal, Apple Pay, Kreditkarte, Sofortüberweisung oder SEPA; Euro werden in Tokens umgerechnet",
    "Tokens: wie Euro in Tokens umgerechnet werden und warum der Aufladebonus das Guthaben verdoppelt",
    "Mehr von {n}: bei Visit-X gibt es weitere Videos und die Livecam – Profil im Blick behalten",
    "Erst schauen, dann entscheiden: über unseren Link landest du direkt beim Video und meldest dich selbst kostenlos an",
    "Ähnliche Clips: auf unserer {n}-Übersicht findest du alle Videos chronologisch sortiert",
]
FAQ_INTENTS = [
    "Worum geht es im Video (Story/Setting, angedeutet)?", "Wie lang ist das Video?", "Wo kann ich das Video ansehen?",
    "Was kostet das Video / wie funktionieren Tokens?", "Gibt es einen Bonus beim Aufladen?", "Welche Zahlungsarten gibt es?",
    "Wie viele Videos hat {n}?", "Kann ich {n} auch live in der Livecam sehen?", "Für wen ist das Video geeignet?",
    "Brauche ich ein Konto, um das Video zu sehen?",
]
INTRO_STYLES = ["mit einer neugierig machenden Frage beginnen", "direkt mitten in der Szene beginnen (Ort/Situation)",
                "mit dem Model und seinem Stil beginnen", "mit einem überraschenden Detail aus Titel oder Beschreibung beginnen"]
BOILER = ["gängige zahlungsarten sind", "die anmeldung startest du selbst", "welche methode dir angeboten wird",
          "zeigt visit-x beim aufladen", "über unseren link landest du zuerst im videoangebot", "du findest es im videoangebot"]


def _pick(seq: list, k: int, seed: str) -> list:
    import random as _r
    rnd = _r.Random(seed)
    return rnd.sample(seq, k)


def clean_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags or []:
        lw = t.lower().strip()
        if not lw or VULGAR.search(lw) or any(r in lw for r in ROOTS) or EXPL.search(lw) or len(lw) > 30:
            continue
        if lw not in [o.lower() for o in out]:
            out.append(t.strip())
    return out[:8]


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


def build_prompt(m: dict, v: dict, facts: str, siblings: list[dict] | None = None) -> str:
    n = m["name"]
    seed = v["id"]
    arche = [x.format(n=n) for x in _pick(ARCHETYPES, 3, seed + "a")]
    tip = _pick(TIPS, 1, seed + "t")[0].format(n=n)
    faqs = [x.format(n=n) for x in _pick(FAQ_INTENTS, 3, seed + "f")]
    style = _pick(INTRO_STYLES, 1, seed + "i")[0]
    hints = topic_hints(v["title"], n)
    tags = clean_tags(v.get("tags") or [])
    meta = f"Länge: {v.get('duration') or 'unbekannt'} Minuten | mit Ton"
    if v.get("date"):
        meta += f" | veröffentlicht am {v['date'][8:10]}.{v['date'][5:7]}.{v['date'][0:4]}"
    sib = [s_["title"] for s_ in (siblings or []) if s_.get("id") != v["id"]][:6]
    desc = (v.get("description") or "").strip()
    parts = [
        f"Bühnenname: {n}",
        f"Videotitel: {v['title']}",
        "BESCHREIBUNG des Models (nur als Kontext, nicht übernehmen; explizite Details nur andeuten):\n" + (desc[:1800] or "(keine)"),
        "Stichworte: " + (", ".join(tags) if tags else "(keine)") + " | Themen aus dem Titel: " + (", ".join(hints) or "(keine)"),
        meta,
        "Model-Fakten:\n- " + "\n- ".join(m["facts"][:6]),
        "Anbieter-Fakten (nur für Tipp/FAQ):\n" + facts,
        f"Einstieg: {style}.",
        "Die drei Abschnitte (H2 frei formulieren, mit Bühnennamen oder konkretem Videobezug):\n1. " + "\n2. ".join(arche[:2]) + "\n3. " + arche[2],
        f"Tipp-Box (Titel + Text): {tip}",
        "FAQ – genau diese drei Themen, als natürliche Google-Fragen mit Bühnennamen: " + " | ".join(faqs),
    ]
    if sib:
        parts.append("Weitere Videos dieses Models (nur zur Abgrenzung, NICHT beschreiben): " + " | ".join(sib))
    parts.append("Diese Formulierungen NICHT verwenden: " + " | ".join(BOILER))
    return "\n".join(parts)


def mock_content(m: dict, v: dict) -> dict:
    n = m["name"]
    body = (f"Dieses {n} Video ist {v.get('duration') or 'einige Minuten'} lang und hat eine eigene Stimmung. " * 9).strip()
    return {"meta_description": f"{n} Video ({v.get('duration')}) bei Visit-X: Story, Stimmung und wie du es mit Bonus ansiehst.",
            "intro": f"Dieses {n} Video stellen wir dir hier vor.", "cover_line": "Neues Video", "highlights": ["Setting", "Stimmung", "Format"],
            "sections": [{"h2": f"Worum es im {n} Video geht", "body": body}, {"h2": f"Stimmung bei {n}", "body": body},
                         {"h2": f"Für wen das {n} Video passt", "body": body}],
            "tip": {"title": "Tipp", "body": body[:200]},
            "faq": [{"q": "Wie lang ist das Video?", "a": f"{v.get('duration')} Minuten."}] * 3}


def _shingles(t: str, k: int = 5) -> set:
    w = re.findall(r"\w+", t.lower())
    return {" ".join(w[i:i + k]) for i in range(len(w) - k + 1)}


def content_text(c: dict) -> str:
    return " ".join([c.get("meta_description", ""), c.get("intro", ""), " ".join(c.get("highlights") or [])] +
                    [s_["body"] + " " + s_["h2"] for s_ in c.get("sections", [])] +
                    [(c.get("tip") or {}).get("title", "") + " " + (c.get("tip") or {}).get("body", "")] +
                    [f["q"] + " " + f["a"] for f in c.get("faq", [])])


def check(c: dict, m: dict, v: dict, sibling_texts: list[str] | None = None) -> list[str]:
    issues = []
    text = content_text(c)
    if EXPL.search(text):
        issues.append(f"explizite Wörter ('{EXPL.search(text).group(0)}')")
    words = len(text.split())
    if words < 380:
        issues.append(f"zu kurz ({words} Wörter, mindestens 380)")
    low = normalize(text)
    desc = normalize(v.get("description", ""))
    for chunk in re.findall(r"(?:\S+\s+){5}\S+", desc)[:80]:   # 6-Wort-Folgen aus der Originalbeschreibung
        if chunk in low:
            issues.append("Formulierung aus der Originalbeschreibung übernommen – komplett eigene Worte")
            break
    for b in BOILER:
        if b in low:
            issues.append(f"Standardsatz verwendet ('{b}')")
            break
    sh = _shingles(text)
    for t in sibling_texts or []:
        o = _shingles(t)
        if sh and o and len(sh & o) / len(sh | o) > 0.12:
            issues.append("zu ähnlich zu einer anderen Videoseite dieses Models – andere Formulierungen")
            break
    if len(c.get("sections", [])) != 3 or not c.get("tip") or len(c.get("highlights") or []) < 3:
        issues.append("Struktur unvollständig (3 Abschnitte, Tipp, 3-4 Stichpunkte)")
    return issues


def generate(cfg: dict, store, mock: bool = False, log=print) -> dict:
    """Erzeugt Texte für neue Videos und schreibt bestehende Videoseiten schrittweise auf Textversion 2 um
    (Beschreibung als Kontext, wechselnde Struktur, Ähnlichkeitsprüfung gegen andere Videos desselben Models)."""
    from . import generate as gen
    s = settings(cfg)
    facts = gen.load_facts(cfg["_root"])
    stats = {"erzeugt": 0, "verworfen": 0, "ueberarbeitet": 0, "ueberarbeitung_fehlgeschlagen": 0}
    new_q, ref_q, sib = [], [], {}
    for m, slug in model_pages(cfg, store):
        data = load(cfg, slug)
        vids = sorted(data["videos"], key=lambda v: int(v["id"]), reverse=True)
        sib[slug] = [v for v in vids if v.get("status") == "published"]
        q = [v for v in vids if v.get("status") == "new"]
        r = []
        for v in vids:
            if v.get("status") != "published" or v.get("refresh_fails", 0) >= 2:
                continue
            c = load_content(cfg, v["id"]) or {}
            if c.get("version", 1) < VIDEO_VERSION:
                r.append(v)
        new_q.append((m, slug, data, q))
        ref_q.append((m, slug, data, r))

    def round_robin(queues, budget):
        jobs = []
        while len(jobs) < budget and any(q for *_, q in queues):
            for m, slug, data, q in queues:
                if q and len(jobs) < budget:
                    jobs.append((m, slug, data, q.pop(0)))
        return jobs

    jobs = [(j, False) for j in round_robin(new_q, s["generate_per_run"])]
    jobs += [(j, True) for j in round_robin(ref_q, int(s.get("refresh_per_run", 0)))]
    llm_cfg = dict(cfg, llm=dict(cfg["llm"], model=s["llm_model"] or cfg["llm"]["model"], max_tokens=8000))

    def sibling_texts(slug, v, k=6):
        lst = sib.get(slug, [])
        ids = [x["id"] for x in lst]
        i = ids.index(v["id"]) if v["id"] in ids else 0
        near = [x for x in lst[max(0, i - k): i + k + 1] if x["id"] != v["id"]]
        out = []
        for x in near:
            c = load_content(cfg, x["id"]) or {}
            if c.get("version", 1) >= VIDEO_VERSION:
                out.append(content_text(c))
        return near, out

    def work(job):
        (m, slug, data, v), _ = job
        near, texts = sibling_texts(slug, v)
        msgs, issues = [], None
        for attempt in range(2):
            try:
                prompt = build_prompt(m, v, facts, near)
                if issues:
                    prompt += "\nDer letzte Entwurf wurde abgelehnt. Behebe: " + "; ".join(issues)
                c = mock_content(m, v) if mock else gen.ask_json(llm_cfg, VIDEO_SYSTEM, prompt, VIDEO_SCHEMA)
                c = gen.clean_content(c) | {"seo_title": seo_title(m, v), "h1": f"{clean_title(v['title'])} – {m['name']} Video",
                                            "cover_line": gen.clean_text(c.get("cover_line", ""))[:26], "version": VIDEO_VERSION}
                c["highlights"] = [gen.clean_text(x) for x in (c.get("highlights") or []) if isinstance(x, str)][:4]
                if isinstance(c.get("tip"), dict):
                    c["tip"] = {"title": gen.clean_text(c["tip"].get("title", "")), "body": gen.clean_text(c["tip"].get("body", ""))}
            except Exception as e:
                msgs.append(f"  [{slug}/{v['id']}] Fehler: {type(e).__name__}: {e}")
                continue
            issues = [] if mock else check(c, m, v, texts)
            if not issues:
                return c, msgs
            msgs.append(f"  [{slug}/{v['id']}] verworfen: {', '.join(issues)}")
        return None, msgs

    from concurrent.futures import ThreadPoolExecutor
    started = time.time()
    with ThreadPoolExecutor(max_workers=int(s.get("workers", 6))) as ex:
        for ((m, slug, data, v), is_refresh), (content, msgs) in zip(jobs, ex.map(work, jobs)):
            for x in msgs:
                log(x)
            if content:
                save_content(cfg, v["id"], content)
                if is_refresh:
                    v["refreshed_at"] = date.today().isoformat()
                    stats["ueberarbeitet"] += 1
                else:
                    v["status"] = "published"
                    v["published_at"] = date.today().isoformat()
                    stats["erzeugt"] += 1
            elif is_refresh:
                v["refresh_fails"] = v.get("refresh_fails", 0) + 1
                stats["ueberarbeitung_fehlgeschlagen"] += 1
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
            vids.append({**v, "title": clean_title(v["title"]), "tags_clean": clean_tags(v.get("tags") or []), "c": c, "url": f"/{slug}/videos/{page_slug(v)}/", "img": f"/img/v/{v['id']}.svg",
                         "link": deeplink(cfg, v["vx_path"]), "secs": mmss(v.get("duration", ""))})
        vids.sort(key=lambda x: int(x["id"]), reverse=True)   # Visit-X-IDs sind chronologisch (neueste zuerst)
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
            discover=[v for v in allv[48:] if v.get("indexed") is False][:48],
            title="Amateur Videos der Top-Models bei Visit-X: neue Clips",
            description=f"{len(allv)} Amateur-Videos der Top-Models bei Visit-X: neueste Clips, Länge und direkter Link. "
                        "Mit 100 % Aufladebonus über unseren Link.", og_image=""))
        urls.append((f"{base}/videos/", date.today().isoformat()))
    return urls
