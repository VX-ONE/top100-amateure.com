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
from datetime import date
from pathlib import Path

from . import images as imgmod
from . import models as modelmod
from .keywords import blocked_hit, load_blocked, normalize, slugify

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
         "llm_model": None, "blocked_terms": []}
    s.update(cfg.get("videos") or {})
    return s


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
    blocked = load_blocked(cfg) + [normalize(t) for t in s["blocked_terms"]]
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
                    try:
                        page.wait_for_selector('a[href*="/videos/"]', timeout=15000)
                    except Exception:
                        pass
                    time.sleep(1.5)
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
                        page.wait_for_timeout(2500)
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
Antworte nur über das Werkzeug "antwort"."""

VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "seo_title": {"type": "string", "description": "35-60 Zeichen, enthält Bühnenname + 'Video' + Kern des Titels (entschärft)"},
        "meta_description": {"type": "string", "description": "120-155 Zeichen, nennt Model, Länge und Nutzen, nicht explizit"},
        "intro": {"type": "string", "description": "2-3 Sätze, erster Satz enthält '<Bühnenname> Video'"},
        "sections": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "object", "properties": {
            "h2": {"type": "string", "description": "enthält den Bühnennamen"}, "body": {"type": "string", "description": "60-120 Wörter"}},
            "required": ["h2", "body"]}},
        "faq": {"type": "array", "minItems": 3, "maxItems": 4, "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string"}}, "required": ["q", "a"]}},
        "cover_line": {"type": "string", "description": "Kurzer, nicht expliziter Cover-Text, max. 26 Zeichen"},
    },
    "required": ["seo_title", "meta_description", "intro", "sections", "faq", "cover_line"],
}


def build_prompt(m: dict, v: dict, facts: str) -> str:
    return "\n".join([
        f"Bühnenname: {m['name']}",
        f"Videotitel (Original, nur als Suchbegriff verwenden): {v['title']}",
        f"Länge: {v.get('duration') or 'unbekannt'} | Veröffentlicht: {v.get('date') or 'unbekannt'} | mit Ton",
        "Model-Fakten:\n- " + "\n- ".join(m["facts"][:8]),
        "Anbieter-Fakten:\n" + facts,
        "Abschnitte (Vorschlag): 'Worum es in diesem <Name> Video geht' (allgemein), 'So schaust du das <Name> Video bei Visit-X' "
        "(Tokens, Zahlungsarten, kostenlos anmelden, Bonus), 'Mehr <Name> Videos und Livecam'.",
        "FAQ: z. B. Wie lang ist das Video? Wo kann ich es ansehen? Gibt es mehr Videos von <Name>? Was kostet es?",
    ])


def mock_content(m: dict, v: dict) -> dict:
    n = m["name"]
    body = (f"Dieses {n} Video ist {v.get('duration') or 'einige Minuten'} lang. " * 12).strip()
    return {"seo_title": f"{n} Video: {v['title'][:30]}", "meta_description": f"{n} Video ({v.get('duration')}) bei Visit-X: Infos, Länge und so schaust du es mit Bonus.",
            "intro": f"Dieses {n} Video stellen wir dir hier vor.", "cover_line": "Neues Video",
            "sections": [{"h2": f"Worum es im {n} Video geht", "body": body}, {"h2": f"So schaust du das {n} Video", "body": body}],
            "faq": [{"q": "Wie lang ist das Video?", "a": f"{v.get('duration')} Minuten."}] * 3}


def check(c: dict, m: dict, v: dict) -> list[str]:
    from . import quality
    issues = []
    text = " ".join([c.get("seo_title", ""), c.get("meta_description", ""), c.get("intro", "")] +
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
        q.sort(key=lambda v: v.get("date", ""), reverse=True)
        queues.append((m, slug, data, q))
    budget = s["generate_per_run"]
    llm_cfg = dict(cfg, llm=dict(cfg["llm"], model=s["llm_model"] or cfg["llm"]["model"], max_tokens=3000))
    while budget > 0 and any(q for *_, q in queues):
        for m, slug, data, q in queues:
            if not q or budget <= 0:
                continue
            v = q.pop(0)
            content = None
            for attempt in range(2):
                try:
                    c = mock_content(m, v) if mock else gen.ask_json(llm_cfg, VIDEO_SYSTEM, build_prompt(m, v, facts), VIDEO_SCHEMA)
                    c = gen.clean_content(c) | {"seo_title": gen.clean_text(c.get("seo_title", "")),
                                                "cover_line": gen.clean_text(c.get("cover_line", ""))[:26]}
                except Exception as e:
                    log(f"  [{slug}/{v['id']}] Fehler: {type(e).__name__}: {e}")
                    continue
                issues = [] if mock else check(c, m, v)
                if not issues:
                    content = c
                    break
                log(f"  [{slug}/{v['id']}] verworfen: {', '.join(issues)}")
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
            budget -= 1
            save(cfg, slug, data)
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
            vids.append({**v, "c": c, "url": f"/{slug}/videos/{page_slug(v)}/", "img": f"/img/v/{v['id']}.svg",
                         "link": deeplink(cfg, v["vx_path"]), "secs": mmss(v.get("duration", ""))})
        vids.sort(key=lambda x: (x.get("date", ""), x["id"]), reverse=True)
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
            related = [x for x in vids if x["id"] != v["id"]][:6]
            write(v["url"].strip("/") + "/index.html", vt.render(
                **ctx, v=v, c=v["c"], m=m, model_slug=slug, related=related, canonical=base + v["url"],
                robots="index,follow", title=v["c"]["seo_title"], description=v["c"]["meta_description"],
                og_image="", og_type="article"))
            urls.append((base + v["url"], v.get("published_at") or date.today().isoformat()))
        write(f"{slug}/videos/index.html", it.render(
            **ctx, m=m, model_slug=slug, vids=vids, canonical=f"{base}/{slug}/videos/", robots="index,follow",
            title=f"{m['name']} Videos: die neuesten Clips im Überblick",
            description=f"Alle neuen {m['name']} Videos bei Visit-X: Titel, Länge und Datum im Überblick. Direkt ansehen mit 100 % Aufladebonus.",
            og_image=""))
        urls.append((f"{base}/{slug}/videos/", max(v.get("published_at", "") for v in vids) or date.today().isoformat()))
    return urls
