"""Zweiwöchentlicher Voll-Audit nach dem Masterprompt „Ultimate Website Audit & Optimization“.

Prüft die gebaute Website (site/), die ausgelieferte Live-Seite, Search-Console-Daten und die Pipeline und schreibt
reports/<datum>-audit.md (Management-Report) sowie reports/audit/<datum>.json (Messwerte für den Vorher-Nachher-
Vergleich beim nächsten Lauf). Der Workflow „Audit“ stellt den Bericht als GitHub-Issue (Label „audit“) zu.

Grundsätze aus dem Masterprompt:
- Nur echte Messungen. Was nicht messbar ist, wird als „nicht verifiziert“ ausgewiesen statt geschätzt.
- Der Audit ändert nichts an der Website. Funde werden mit Priorität P0–P3 gemeldet; sichere Korrekturen setzt
  Claude in der nächsten Sitzung um, Löschungen/Noindex/Weiterleitungen/Affiliate-Änderungen nur mit Freigabe.
"""
from __future__ import annotations

import json
import os
import random
import re
import statistics as st
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from . import quality

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 "
      "Top100-Audit/1.0")
AFF_PARAMS = {"w": "40150", "ws": "2610001", "pc": "2XITGA"}

# Gewichtung der Gesamtnote (Summe 100): Inhalte, Indexierung und Umsatz zählen am meisten,
# weil sie Traffic und Provision direkt bestimmen; Code-Themen wirken nur indirekt.
WEIGHTS = {
    "Technical SEO": 8, "Crawling & Indexability": 10, "On-Page SEO": 7, "Content Quality": 10,
    "Search Intent Alignment": 5, "Keyword Strategy": 5, "Internal Linking": 6, "Information Architecture": 4,
    "Structured Data": 4, "Core Web Vitals": 5, "Mobile Experience": 5, "UX/UI": 3, "Accessibility": 3,
    "Conversion Optimization": 6, "Affiliate Monetization": 8, "Trust & Transparency": 4, "Security": 3,
    "Code Quality": 2, "Scalability": 1, "Maintainability": 1,
}


# ---------------------------------------------------------------- HTML einlesen

class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}
    # Bedienelemente (Buttons, Zahlungs-/Bonus-Hinweise, Menüs, Kartenraster) zählen nicht als redaktioneller Text
    UI = {"vcta", "paynote", "howto", "small", "mtabs", "crumbs", "pager", "guides-box", "grid", "vtags",
          "stickycta", "block-head", "vmeta", "bonus-big", "adnote", "cards", "card"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.canonical = None
        self.lang = None
        self.headings: list[tuple[int, str]] = []
        self.links: list[dict] = []
        self.imgs: list[dict] = []
        self.ld: list[str] = []
        self.scripts: list[str] = []
        self.text_main: list[str] = []
        self.text_all: list[str] = []
        self.buttons = 0
        self._stack: list[str] = []
        self._ui: list[bool] = []
        self._in_main = 0
        self._in_chrome = 0  # header/footer/nav
        self._cap = None  # (tag, buffer)
        self._a = None
        self._ld = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta":
            k = a.get("name") or a.get("property")
            if k:
                self.meta.setdefault(k.lower(), a.get("content", ""))
        elif tag == "link" and "canonical" in a.get("rel", "").split():
            self.canonical = a.get("href")
        elif tag == "script":
            if a.get("type") == "application/ld+json":
                self._ld = []
            if a.get("src"):
                self.scripts.append(a["src"])
        elif tag == "img":
            if self._a is not None:
                self._a["img"] = True
            self.imgs.append({"src": a.get("src", ""), "alt": a.get("alt"), "w": a.get("width"), "h": a.get("height"),
                              "loading": a.get("loading", ""), "main": bool(self._in_main)})
        elif tag == "a":
            self._a = {"href": a.get("href", ""), "rel": a.get("rel", ""), "target": a.get("target", ""),
                       "ui": any(self._ui),
                       "cls": a.get("class", ""), "text": [], "main": bool(self._in_main) and not self._in_chrome}
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._cap = (tag, [])
        if tag in ("main", "article"):
            self._in_main += 1
        if tag in ("header", "footer", "nav"):
            self._in_chrome += 1
        if tag not in ("meta", "link", "img", "br", "hr", "input", "source"):
            self._stack.append(tag)
            self._ui.append(bool(self.UI & set(a.get("class", "").split())) or tag in ("a", "button", "summary"))

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.ld.append("".join(self._ld))
            self._ld = None
        if tag == "a" and self._a is not None:
            self._a["text"] = re.sub(r"\s+", " ", "".join(self._a["text"])).strip()
            self.links.append(self._a)
            if "btn" in self._a["cls"].split():
                self.buttons += 1
            self._a = None
        if self._cap and tag == self._cap[0]:
            self.headings.append((int(tag[1]), re.sub(r"\s+", " ", "".join(self._cap[1])).strip()))
            self._cap = None
        if tag in ("main", "article"):
            self._in_main = max(0, self._in_main - 1)
        if tag in ("header", "footer", "nav"):
            self._in_chrome = max(0, self._in_chrome - 1)
        while self._stack:
            self._ui.pop()
            if self._stack.pop() == tag:
                break

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
            return
        if any(t in self.SKIP for t in self._stack):
            return
        if self._stack and self._stack[-1] == "title":
            self.title += data
            return
        if self._cap:
            self._cap[1].append(data)
        if self._a is not None:
            self._a["text"].append(data)
        self.text_all.append(data)
        if self._in_main and not self._in_chrome and not any(self._ui):
            self.text_main.append(data)


def _kind(path: str, model_slugs: set[str]) -> str:
    if re.match(r"^/[^/]+/videos/\d", path):
        return "Videoseite"
    if re.match(r"^/[^/]+/videos/(seite/\d+/)?$", path) and path != "/videos/":
        return "Video-Übersicht"
    if path == "/":
        return "Startseite"
    seg = path.strip("/").split("/")[0]
    if seg in model_slugs:
        return "Model-Profil"
    if seg in ("impressum", "datenschutz"):
        return "Rechtliches"
    if path in ("/videos/", "/models/", "/live-cams-videos/", "/ratgeber/") or (path.count("/") == 3 and seg == "themen"):
        return "Hub"
    return "Ratgeber/Themen"


def crawl_local(cfg: dict, store, root: Path) -> dict:
    site = root / "site"
    base = cfg["site"]["base_url"].rstrip("/")
    model_slugs = {s for s, p in store.pages().items() if p.get("kind") == "model"}
    pages = {}
    for f in sorted(site.rglob("index.html")):
        rel = "/" + str(f.parent.relative_to(site)).replace("\\", "/") + "/"
        rel = "/" if rel == "/./" else rel
        p = _Page()
        p.feed(f.read_text(encoding="utf-8", errors="replace"))
        main = re.sub(r"\s+", " ", " ".join(p.text_main)).strip()
        pages[rel] = {
            "path": rel, "url": base + rel, "kind": _kind(rel, model_slugs), "title": p.title.strip(),
            "desc": p.meta.get("description", ""), "robots": p.meta.get("robots", ""), "canonical": p.canonical,
            "lang": p.lang, "meta": p.meta, "headings": p.headings, "links": p.links, "imgs": p.imgs, "ld": p.ld,
            "scripts": p.scripts, "main": main, "full": re.sub(r"\s+", " ", " ".join(p.text_all)), "words": len(main.split()), "buttons": p.buttons,
            "bytes": f.stat().st_size,
        }
    # Sitemaps
    sm_urls = []
    for f in site.glob("sitemap*.xml"):
        x = f.read_text(encoding="utf-8")
        if "<sitemapindex" in x:
            continue
        sm_urls += re.findall(r"<loc>([^<]+)</loc>", x)
    files = {"/" + str(f.relative_to(site)).replace("\\", "/") for f in site.rglob("*") if f.is_file()}
    redirects = []
    rf = site / "_redirects"
    if rf.exists():
        for line in rf.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.startswith("#"):
                redirects.append((parts[0], parts[1]))
    robots = (site / "robots.txt").read_text(encoding="utf-8") if (site / "robots.txt").exists() else ""
    headers = (site / "_headers").read_text(encoding="utf-8") if (site / "_headers").exists() else ""
    return {"pages": pages, "sitemap": sm_urls, "files": files, "redirects": redirects, "robots": robots,
            "headers": headers, "base": base, "site": site}


# ---------------------------------------------------------------- Prüfungen (offline)

class Findings:
    def __init__(self):
        self.items: list[dict] = []
        self.checks: dict[str, list[tuple[str, float | None, str]]] = defaultdict(list)

    def add(self, prio: str, area: str, text: str, count: int = 0, examples=(), fix: str = "", auto: bool = True):
        self.items.append({"prio": prio, "area": area, "text": text, "count": count,
                           "examples": [str(e) for e in list(examples)[:5]], "fix": fix, "auto": auto})

    def check(self, cat: str, name: str, ratio: float | None, detail: str = ""):
        """ratio = Anteil OK (0..1) oder None = nicht messbar."""
        self.checks[cat].append((name, None if ratio is None else max(0.0, min(1.0, ratio)), detail))


def _ok(bad: int, total: int) -> float:
    return 1.0 if total == 0 else 1 - bad / total


def _norm_path(href: str, base: str) -> str | None:
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    u = urlparse(urljoin(base + "/", href))
    if u.netloc and u.netloc not in (urlparse(base).netloc,):
        return None
    return u.path or "/"


def offline_checks(c: dict, F: Findings, cfg: dict, metrics: dict) -> None:
    pages, base = c["pages"], c["base"]
    idx = {p: d for p, d in pages.items() if "noindex" not in d["robots"]}
    metrics["urls_total"] = len(pages)
    metrics["urls_indexable"] = len(idx)
    metrics["by_kind"] = dict(Counter(d["kind"] for d in pages.values()))

    # --- Sitemap / Canonical / Robots
    sm = set(c["sitemap"])
    sm_paths = {urlparse(u).path for u in sm}
    not_in_sm = [d["url"] for p, d in idx.items() if d["url"] not in sm and d["kind"] != "Rechtliches"]
    sm_missing = [u for u in sm if urlparse(u).path not in pages]
    sm_noindex = [u for u in sm if urlparse(u).path in pages and "noindex" in pages[urlparse(u).path]["robots"]]
    can_bad = [d["url"] for d in pages.values() if d["canonical"] != d["url"]]
    metrics.update(sitemap_urls=len(sm), sitemap_missing=len(sm_missing), not_in_sitemap=len(not_in_sm),
                   canonical_bad=len(can_bad))
    F.check("Crawling & Indexability", "Indexierbare Seiten in der Sitemap", _ok(len(not_in_sm), len(idx)))
    F.check("Crawling & Indexability", "Sitemap-URLs existieren", _ok(len(sm_missing), len(sm)))
    F.check("Crawling & Indexability", "Keine noindex-URLs in der Sitemap", _ok(len(sm_noindex), len(sm)))
    F.check("Technical SEO", "Canonical = eigene URL (https, www, Slash)", _ok(len(can_bad), len(pages)))
    robots_ok = "Disallow: /\n" not in c["robots"] and "Sitemap:" in c["robots"]
    F.check("Crawling & Indexability", "robots.txt erlaubt Crawling + nennt Sitemap", 1.0 if robots_ok else 0.0)
    if sm_missing:
        F.add("P0", "Indexierung", "Sitemap enthält URLs ohne Seite (404 für Google)", len(sm_missing), sm_missing,
              "Sitemap-Erzeugung an veröffentlichte Seiten koppeln")
    if not_in_sm:
        F.add("P1", "Indexierung", "Indexierbare Seiten fehlen in der Sitemap", len(not_in_sm), not_in_sm)
    if sm_noindex:
        F.add("P1", "Indexierung", "noindex-Seiten stehen in der Sitemap", len(sm_noindex), sm_noindex)
    if can_bad:
        F.add("P0", "Technik", "Canonical weicht von der eigenen URL ab", len(can_bad), can_bad)
    if not robots_ok:
        F.add("P0", "Indexierung", "robots.txt sperrt Crawling oder nennt keine Sitemap")

    # --- interne Links, Klicktiefe, verwaiste Seiten
    statics = c["files"]
    redir_src = {s.rstrip("/") + "/" for s, _ in c["redirects"]} | {s for s, _ in c["redirects"]}
    inbound = Counter()
    out_edges = defaultdict(set)
    broken = defaultdict(set)
    noslash = set()
    anchors = defaultdict(Counter)
    for p, d in pages.items():
        for l in d["links"]:
            t = _norm_path(l["href"], base)
            l["_t"] = t
            if t is None:
                continue
            if t in pages:
                if t != p:
                    inbound[t] += 1
                    out_edges[p].add(t)
                    if l["text"] and l["main"] and not l["ui"]:
                        anchors[t][l["text"].lower()] += 1
            elif t.rstrip("/") + "/" in pages and not t.endswith("/") and "." not in t.rsplit("/", 1)[-1]:
                noslash.add(f"{p} → {t}")
                out_edges[p].add(t + "/")
            elif t in statics or t in redir_src or t.split("?")[0] in statics:
                pass
            else:
                broken[t].add(p)
    depth = {"/": 0}
    q = deque(["/"])
    while q:
        u = q.popleft()
        for v in out_edges[u]:
            if v not in depth:
                depth[v] = depth[u] + 1
                q.append(v)
    unreachable = [p for p in idx if p not in depth]
    orphans = [p for p in idx if inbound[p] == 0 and p != "/"]
    deep = [p for p in idx if depth.get(p, 99) > 3 and p not in unreachable]
    nb = sum(len(v) for v in broken.values())
    total_links = sum(len(d["links"]) for d in pages.values())
    metrics.update(broken_links=nb, broken_targets=len(broken), orphans=len(orphans), unreachable=len(unreachable),
                   depth_max=max(depth.values()) if depth else 0,
                   depth_dist=dict(Counter(min(v, 6) for p, v in depth.items() if p in idx)),
                   links_internal_avg=round(st.mean(len(out_edges[p]) for p in pages), 1) if pages else 0)
    F.check("Crawling & Indexability", "Keine kaputten internen Links", _ok(len(broken), len(pages)))
    F.check("Crawling & Indexability", "Alle Seiten von der Startseite erreichbar", _ok(len(unreachable), len(idx)))
    F.check("Internal Linking", "Keine verwaisten Seiten", _ok(len(orphans), len(idx)))
    F.check("Information Architecture", "Klicktiefe ≤ 3", _ok(len(deep), len(idx)))
    F.check("Technical SEO", "Interne Links mit Abschluss-Slash (keine Weiterleitung)", _ok(len(noslash), max(1, total_links)))
    if broken:
        ex = [f"{t} (verlinkt auf {len(s)} Seiten, z. B. {sorted(s)[0]})" for t, s in sorted(broken.items(), key=lambda x: -len(x[1]))]
        F.add("P0", "Interne Links", "Kaputte interne Links (Ziel existiert nicht)", nb, ex,
              "Links auf zurückgezogene/fehlende Seiten entfernen oder Ziel wiederherstellen")
    if unreachable:
        F.add("P1", "Architektur", "Seiten sind über Links von der Startseite nicht erreichbar", len(unreachable), unreachable)
    if orphans:
        F.add("P1", "Interne Links", "Verwaiste Seiten (kein interner Link zeigt darauf)", len(orphans), orphans)
    if deep:
        F.add("P2", "Architektur", "Seiten tiefer als 3 Klicks", len(deep), deep)
    if noslash:
        F.add("P3", "Technik", "Interne Links ohne Abschluss-Slash (führen über Weiterleitung)", len(noslash), sorted(noslash))
    # Ankertexte: dieselbe exakte Formulierung sehr oft auf dasselbe Ziel (Überoptimierung)
    over = [f"{t}: „{a}“ {n}×" for t, cnt in anchors.items() for a, n in cnt.items()
            if n > 400 and len(a.split()) >= 3 and pages.get(t, {}).get("kind") not in ("Startseite", "Hub", "Rechtliches")]
    F.check("Internal Linking", "Keine überoptimierten Ankertexte", _ok(len(over), max(1, len(anchors))))
    if over:
        F.add("P3", "Interne Links", "Sehr häufig wiederholte identische Ankertexte", len(over), over)
    weak = [p for p in idx if inbound[p] <= 1 and pages[p]["kind"] in ("Ratgeber/Themen", "Model-Profil")]
    F.check("Internal Linking", "Ratgeber/Profile mit ≥ 2 internen Links", _ok(len(weak), sum(1 for p in idx if pages[p]["kind"] in ("Ratgeber/Themen", "Model-Profil"))))
    if weak:
        F.add("P2", "Interne Links", "Ratgeber/Profile mit höchstens einem eingehenden Link", len(weak), weak)
    kinds_inb = defaultdict(list)
    for p in idx:
        kinds_inb[pages[p]["kind"]].append(inbound[p])
    metrics["inbound_median_by_kind"] = {k: st.median(v) for k, v in kinds_inb.items()}

    # --- Metadaten
    titles = Counter(d["title"] for d in idx.values())
    descs = Counter(d["desc"] for d in idx.values() if d["desc"])
    dup_t = [f"{t} ({n}×)" for t, n in titles.items() if n > 1]
    dup_d = [f"{t[:70]}… ({n}×)" for t, n in descs.items() if n > 1]
    no_d = [d["url"] for d in idx.values() if not d["desc"]]
    t_len = [f"{d['title']} ({len(d['title'])})" for d in idx.values() if not 25 <= len(d["title"]) <= 65]
    d_len = [f"{d['url']} ({len(d['desc'])})" for d in idx.values() if d["desc"] and not 70 <= len(d["desc"]) <= 160]
    h1_bad = [f"{d['url']} ({sum(1 for h in d['headings'] if h[0] == 1)} H1)" for d in idx.values()
              if sum(1 for h in d["headings"] if h[0] == 1) != 1]
    skips = []
    for d in idx.values():
        prev = 0
        for lvl, _ in d["headings"]:
            if prev and lvl > prev + 1:
                skips.append(d["url"])
                break
            prev = lvl
    og_miss = [d["url"] for d in idx.values() if not all(k in d["meta"] for k in ("og:title", "og:url", "og:type"))]
    # Videoseiten haben SVG-Cover; soziale Netzwerke zeigen SVG nicht an – dort bewusst kein og:image
    og_img = [d["url"] for d in idx.values() if "og:image" not in d["meta"] and d["kind"] != "Videoseite"]
    tw = [d["url"] for d in idx.values() if "twitter:card" not in d["meta"] and d["kind"] != "Videoseite"]
    lang_bad = [d["url"] for d in pages.values() if d["lang"] != "de"]
    n = len(idx)
    metrics.update(dup_titles=len(dup_t), dup_desc=len(dup_d), missing_desc=len(no_d), title_len_bad=len(t_len),
                   desc_len_bad=len(d_len), h1_bad=len(h1_bad), heading_skips=len(skips), og_image_missing=len(og_img))
    for name, bad in (("Eindeutige Titel", sum(titles[t] for t in titles if titles[t] > 1)),
                      ("Meta-Description vorhanden", len(no_d)),
                      ("Eindeutige Descriptions", sum(descs[t] for t in descs if descs[t] > 1)),
                      ("Titel 25–65 Zeichen", len(t_len)), ("Description 70–160 Zeichen", len(d_len)),
                      ("Genau eine H1", len(h1_bad)), ("Überschriften ohne Sprung", len(skips))):
        F.check("On-Page SEO", name, _ok(bad, n))
    F.check("Technical SEO", "Open-Graph-Grunddaten", _ok(len(og_miss), n))
    F.check("Technical SEO", "Open-Graph-Bild / X-Card", _ok(len(set(og_img) | set(tw)), n))
    F.check("Technical SEO", "Sprachkennzeichnung lang=de", _ok(len(lang_bad), len(pages)))
    if dup_t:
        F.add("P1", "Metadaten", "Doppelte Title-Tags", len(dup_t), dup_t)
    if dup_d:
        F.add("P2", "Metadaten", "Doppelte Meta-Descriptions", len(dup_d), dup_d)
    if no_d:
        F.add("P1", "Metadaten", "Fehlende Meta-Description", len(no_d), no_d)
    if t_len:
        F.add("P3", "Metadaten", "Title kürzer als 25 oder länger als 65 Zeichen", len(t_len), t_len)
    if d_len:
        F.add("P3", "Metadaten", "Description kürzer als 70 oder länger als 160 Zeichen", len(d_len), d_len)
    if h1_bad:
        F.add("P1", "Überschriften", "Seiten ohne oder mit mehreren H1", len(h1_bad), h1_bad)
    if skips:
        F.add("P3", "Überschriften", "Überschriften-Ebene übersprungen (z. B. H2 → H4)", len(skips), skips)
    if og_img:
        F.add("P3", "Metadaten", "Kein og:image / keine X-Card (schlechtere Vorschau beim Teilen)", len(og_img), og_img)

    # --- Bilder
    imgs = [(d["url"], i) for d in pages.values() for i in d["imgs"]]
    no_alt = [f"{u} – {i['src'][:60]}" for u, i in imgs if not (i["alt"] or "").strip()]
    no_dim = [f"{u} – {i['src'][:60]}" for u, i in imgs if not (i["w"] and i["h"])]
    metrics.update(images=len(imgs), img_no_alt=len(no_alt), img_no_dims=len(no_dim))
    F.check("Accessibility", "Bilder mit Alt-Text", _ok(len(no_alt), len(imgs)))
    F.check("Core Web Vitals", "Bilder mit Breite/Höhe (kein Layout-Shift)", _ok(len(no_dim), len(imgs)))
    exts = Counter(Path(urlparse(i["src"]).path).suffix.lower() for _, i in imgs)
    modern = sum(v for k, v in exts.items() if k in (".webp", ".avif", ".svg"))
    metrics["img_formats"] = dict(exts)
    F.check("Core Web Vitals", "Moderne Bildformate (WebP/AVIF)", modern / len(imgs) if imgs else None)
    if no_alt:
        F.add("P2", "Bilder", "Bilder ohne Alt-Text", len(no_alt), no_alt)
    if no_dim:
        F.add("P2", "Bilder", "Bilder ohne width/height", len(no_dim), no_dim)
    if imgs and modern / len(imgs) < 0.8:
        F.add("P2", "Performance", f"Nur {modern / len(imgs):.0%} der Bilder als WebP/AVIF/SVG", len(imgs) - modern,
              [f"{k or 'ohne Endung'}: {v}" for k, v in exts.most_common()])

    # --- Strukturierte Daten
    ld_err, types = [], Counter()
    faq_invisible, crumbs_bad = [], []
    for d in pages.values():
        for raw in d["ld"]:
            try:
                obj = json.loads(raw)
            except Exception as e:
                ld_err.append(f"{d['url']}: {e}")
                continue
            graph = obj.get("@graph", [obj]) if isinstance(obj, dict) else obj
            for g in graph:
                t = g.get("@type")
                types[t if isinstance(t, str) else ",".join(t)] += 1
                if t == "FAQPage":
                    qs = [x.get("name", "") for x in g.get("mainEntity", [])]
                    if not qs or any(q and q[:40] not in " ".join(h[1] for h in d["headings"]) + d["main"] for q in qs):
                        faq_invisible.append(d["url"])
                if t == "BreadcrumbList":
                    for it in g.get("itemListElement", []):
                        p = urlparse(it.get("item", "")).path
                        if p and p not in pages:
                            crumbs_bad.append(f"{d['url']} → {it.get('item')}")
    metrics.update(ld_errors=len(ld_err), ld_types=dict(types))
    F.check("Structured Data", "JSON-LD syntaktisch gültig", _ok(len(ld_err), sum(len(d["ld"]) for d in pages.values())))
    F.check("Structured Data", "FAQ-Markup entspricht sichtbarem Inhalt", _ok(len(faq_invisible), max(1, types.get("FAQPage", 0))))
    F.check("Structured Data", "Breadcrumb-Ziele existieren", _ok(len(crumbs_bad), max(1, types.get("BreadcrumbList", 0))))
    F.check("Structured Data", "Strukturierte Daten auf indexierbaren Seiten",
            _ok(sum(1 for d in idx.values() if not d["ld"]), n))
    if ld_err:
        F.add("P1", "Strukturierte Daten", "Ungültiges JSON-LD", len(ld_err), ld_err)
    if faq_invisible:
        F.add("P1", "Strukturierte Daten", "FAQ-Markup ohne passende sichtbare Fragen", len(faq_invisible), faq_invisible)
    if crumbs_bad:
        F.add("P1", "Strukturierte Daten", "Breadcrumb verweist auf nicht existierende Seite", len(crumbs_bad), crumbs_bad)

    # --- Inhalte: Wortzahl, Bausteine, Ähnlichkeit, explizite Wörter
    words_by = defaultdict(list)
    for d in idx.values():
        words_by[d["kind"]].append(d["words"])
    metrics["words_median_by_kind"] = {k: st.median(v) for k, v in words_by.items()}
    thin = [f"{d['url']} ({d['words']} Wörter)" for d in idx.values()
            if d["kind"] in ("Videoseite", "Model-Profil", "Ratgeber/Themen") and d["words"] < 300]
    F.check("Content Quality", "Kein Thin Content (≥ 300 Wörter Hauptinhalt)",
            _ok(len(thin), sum(1 for d in idx.values() if d["kind"] in ("Videoseite", "Model-Profil", "Ratgeber/Themen"))))
    if thin:
        F.add("P1", "Inhalte", "Seiten mit weniger als 300 Wörtern Hauptinhalt", len(thin), thin)
    # Sätze, die auf vielen Seiten identisch vorkommen (Textbausteine)
    sent_pages = defaultdict(set)
    sents_of = {}
    for p, d in idx.items():
        ss = {s.strip() for s in re.split(r"(?<=[.!?])\s+", d["main"]) if len(s.split()) >= 8}
        sents_of[p] = ss
        for s in ss:
            sent_pages[s].add(p)
    boiler = {s for s, ps in sent_pages.items() if len(ps) >= 20}
    share_by = defaultdict(list)
    for p, ss in sents_of.items():
        if ss:
            share_by[idx[p]["kind"]].append(len(ss & boiler) / len(ss))
    metrics["boilerplate_share_by_kind"] = {k: round(st.mean(v), 3) for k, v in share_by.items()}
    allshare = [x for v in share_by.values() for x in v]
    F.check("Content Quality", "Geringer Anteil wiederholter Textbausteine", 1 - st.mean(allshare) if allshare else None)
    top_b = sorted(boiler, key=lambda s: -len(sent_pages[s]))[:5]
    if boiler:
        F.add("P1" if st.mean(allshare) > 0.15 else "P2", "Inhalte",
              f"Wiederholte Textbausteine: {len(boiler)} identische Sätze auf je ≥ 20 Seiten "
              f"(Ø {st.mean(allshare):.0%} des Hauptinhalts)", len(boiler),
              [f"„{s[:90]}…“ – {len(sent_pages[s])} Seiten" for s in top_b])
    # Ähnlichkeit: Videoseiten je Model mit chronologischen Nachbarn, alle anderen paarweise
    def sh(t):
        w = re.findall(r"[a-zäöüß0-9]+", t.lower())
        return {" ".join(w[i:i + 5]) for i in range(max(0, len(w) - 4))}
    shing = {p: sh(d["main"]) for p, d in idx.items() if d["kind"] in ("Videoseite", "Model-Profil", "Ratgeber/Themen")}
    sims = []
    near = []
    byg = defaultdict(list)
    for p in shing:
        k = idx[p]["kind"]
        byg[(k, p.split("/")[1]) if k == "Videoseite" else (k, "")].append(p)
    for (k, _), ps in byg.items():
        ps.sort()
        for i, a in enumerate(ps):
            rng = ps[i + 1:i + 7] if k == "Videoseite" else ps[i + 1:]
            for b in rng:
                A, B = shing[a], shing[b]
                if not A or not B:
                    continue
                j = len(A & B) / len(A | B)
                sims.append(j)
                if j > 0.35:
                    near.append(f"{a} ~ {b} ({j:.2f})")
    metrics["similarity_median"] = round(st.median(sims), 3) if sims else None
    metrics["near_duplicates"] = len(near)
    F.check("Content Quality", "Keine Beinahe-Duplikate (Jaccard > 0,35)", _ok(len(near), max(1, len(sims))))
    if near:
        F.add("P1", "Inhalte", "Beinahe-doppelte Seiten (5-Wort-Folgen > 35 % gleich)", len(near), near,
              "Zusammenführen (Freigabe) oder neu schreiben")
    expl = []
    for d in idx.values():
        m = quality.EXPLICIT.search(d["main"])
        if m and d["kind"] != "Videoseite":
            expl.append(f"{d['url']}: „{m.group(0)}“")
        elif m:
            # Auf Videoseiten steht der Originaltitel von Visit-X (H1) – nur der eigene Text zählt
            own = d["main"]
            for _, h in d["headings"][:1]:
                own = own.replace(h, "")
            m2 = quality.EXPLICIT.search(own)
            if m2:
                expl.append(f"{d['url']}: „{m2.group(0)}“")
    metrics["explicit_pages"] = len(expl)
    F.check("Trust & Transparency", "Eigene Texte ohne explizite Wörter", _ok(len(expl), n))
    if expl:
        F.add("P2", "Inhalte", "Explizite Wörter im eigenen Text (Suchmaschinen-/Werbe-Richtlinien)", len(expl), expl,
              auto=False)

    # --- Affiliate & Conversion
    aff_pages, param_bad, rel_bad, no_cta = 0, [], [], []
    hosts = Counter()
    for d in pages.values():
        affl = [l for l in d["links"] if "visit-x" in urlparse(l["href"]).netloc]
        if affl:
            aff_pages += 1
        elif d["kind"] not in ("Rechtliches",):
            no_cta.append(d["url"])
        for l in affl:
            qs = parse_qs(urlparse(l["href"]).query)
            if any(qs.get(k, [None])[0] != v for k, v in AFF_PARAMS.items()):
                param_bad.append(f"{d['url']} → {l['href'][:110]}")
            if "sponsored" not in l["rel"]:
                rel_bad.append(f"{d['url']} → {l['href'][:80]}")
        for l in d["links"]:
            h = urlparse(l["href"]).netloc
            if h and h not in urlparse(base).netloc:
                hosts[h] += 1
    tot_aff = sum(1 for d in pages.values() for l in d["links"] if "visit-x" in urlparse(l["href"]).netloc)
    metrics.update(affiliate_links=tot_aff, affiliate_param_bad=len(param_bad), affiliate_rel_bad=len(rel_bad),
                   pages_without_affiliate=len(no_cta), external_hosts=dict(hosts.most_common(10)))
    F.check("Affiliate Monetization", "Partner-Parameter w/ws/pc auf allen Visit-X-Links", _ok(len(param_bad), tot_aff))
    F.check("Affiliate Monetization", "Affiliate-Links als rel=sponsored gekennzeichnet", _ok(len(rel_bad), tot_aff))
    F.check("Conversion Optimization", "Jede Inhaltsseite hat einen Visit-X-Link", _ok(len(no_cta), len(pages)))
    if param_bad:
        F.add("P0", "Affiliate", "Visit-X-Links ohne vollständige Partner-Parameter (Provision geht verloren)",
              len(param_bad), param_bad, "Nur nach Freigabe ändern (Affiliate-Links)", auto=False)
    if rel_bad:
        F.add("P1", "Affiliate", "Affiliate-Links ohne rel=sponsored", len(rel_bad), rel_bad)
    if no_cta:
        hub_only = all(pages[urlparse(u).path]["kind"] == "Hub" for u in no_cta)
        F.add("P3" if hub_only else "P2", "Conversion", "Seiten ohne Visit-X-Link" + (" (nur Übersichtsseiten)" if hub_only else ""),
              len(no_cta), no_cta)
    # Bonus-Kommunikation einheitlich?
    bonus_vars = Counter()
    for d in pages.values():
        for m in re.finditer(r"(\d{2,3})\s?(%|Prozent)[^.]{0,40}(Bonus|Aufladebonus|Guthaben)", d["main"]):
            bonus_vars[m.group(1)] += 1
    metrics["bonus_mentions"] = dict(bonus_vars)
    F.check("Affiliate Monetization", "Bonus-Angabe einheitlich", 1.0 if len(bonus_vars) <= 1 else 0.5)
    if len(bonus_vars) > 1:
        F.add("P1", "Affiliate", "Unterschiedliche Bonus-Prozentangaben auf der Website", sum(bonus_vars.values()),
              [f"{k} %: {v}×" for k, v in bonus_vars.items()], auto=False)

    # --- Trust, Recht, Jugendschutz
    legal = ("/impressum/", "/datenschutz/")
    no_legal = [d["url"] for d in pages.values()
                if not all(any(_norm_path(l["href"], base) == lp for l in d["links"]) for lp in legal)]
    no_age = [d["url"] for d in pages.values() if not any("agegate" in s for s in d["scripts"])]
    no_rating = [d["url"] for d in pages.values() if d["meta"].get("rating") != "adult"]
    no_ad = [d["url"] for d in pages.values() if "Werbung" not in d["full"] and "Werbehinweis" not in d["full"]
             and d["kind"] != "Rechtliches"]
    gtag_direct = [d["url"] for d in pages.values() if any("googletagmanager" in s for s in d["scripts"])]
    F.check("Trust & Transparency", "Impressum + Datenschutz auf jeder Seite verlinkt", _ok(len(no_legal), len(pages)))
    F.check("Trust & Transparency", "Altersabfrage auf jeder Seite", _ok(len(no_age), len(pages)))
    F.check("Trust & Transparency", "rating=adult (SafeSearch) auf jeder Seite", _ok(len(no_rating), len(pages)))
    F.check("Trust & Transparency", "Werbekennzeichnung sichtbar", _ok(len(no_ad), len(pages)))
    F.check("Security", "Google Analytics erst nach Einwilligung", _ok(len(gtag_direct), len(pages)))
    for prio, lst, txt in (("P0", no_legal, "Impressum/Datenschutz nicht verlinkt"),
                           ("P0", no_age, "Altersabfrage fehlt"), ("P1", no_rating, "rating=adult fehlt"),
                           ("P1", no_ad, "Keine Werbekennzeichnung im Inhalt erkannt"),
                           ("P0", gtag_direct, "Google-Tag wird ohne Einwilligung geladen")):
        if lst:
            F.add(prio, "Recht/Trust", txt, len(lst), lst, auto=prio != "P0")

    # --- Sicherheitsheader laut _headers (live wird zusätzlich geprüft)
    h = c["headers"].lower()
    for name in ("x-content-type-options", "referrer-policy", "x-frame-options", "strict-transport-security",
                 "content-security-policy", "permissions-policy"):
        F.check("Security", f"Header {name} konfiguriert", 1.0 if name in h else 0.0)
    missing_h = [x for x in ("strict-transport-security", "content-security-policy", "permissions-policy") if x not in h]
    if missing_h:
        F.add("P2", "Sicherheit", "Sicherheitsheader fehlen in _headers: " + ", ".join(missing_h), len(missing_h))
    ext_scripts = sorted({s for d in pages.values() for s in d["scripts"] if s.startswith("http")})
    metrics["external_scripts"] = ext_scripts

    # --- Mobile/UX-Grundlagen aus dem HTML
    no_vp = [d["url"] for d in pages.values() if "viewport" not in d["meta"]]
    F.check("Mobile Experience", "Viewport-Meta", _ok(len(no_vp), len(pages)))
    empty_links = [f"{d['url']} → {l['href'][:60]}" for d in pages.values() for l in d["links"]
                   if not l["text"] and not l.get("img")]
    F.check("Accessibility", "Links mit Text oder Bild-Alt", _ok(len(empty_links), total_links) if total_links else None)
    if empty_links:
        F.add("P3", "Barrierefreiheit", "Links ohne Text", len(empty_links), empty_links)
    adnote = [d["url"] for d in pages.values() if d["kind"] in ("Videoseite", "Model-Profil", "Video-Übersicht", "Startseite")
              and "Dieser Ratgeber ist redaktionell" in d["full"]]
    if adnote:
        F.add("P3", "Recht/Trust", "Werbehinweis nennt Video-/Profilseiten „Ratgeber“", len(adnote), adnote)
    metrics["pages_bytes_median"] = st.median(d["bytes"] for d in pages.values()) if pages else 0


def model_keyword_checks(c: dict, F: Findings, cfg: dict, store, metrics: dict) -> None:
    """Pflicht-Keywords je Model (Vorgabe Chris): „<Name> Porno/Pornos“ und der Name zusammen- und auseinandergeschrieben
    auf Profil, Video-Übersicht und jeder Videoseite."""
    from . import models as modelmod
    alts = {m["name"]: m.get("name_alt", "") for m in modelmod.load_models(cfg)}
    model_of = {s: p.get("model") for s, p in store.pages().items() if p.get("kind") == "model"}
    miss = defaultdict(list)
    tot = Counter()

    def norm(t):
        return re.sub(r"[\s\-_()]+", " ", t.lower())

    for p, d in c["pages"].items():
        seg = p.strip("/").split("/")[0]
        name = model_of.get(seg)
        if not name or d["kind"] not in ("Model-Profil", "Videoseite", "Video-Übersicht"):
            continue
        alt = alts.get(name, "")
        k = d["kind"]
        tot[k] += 1
        head = norm(d["title"] + " " + " ".join(h for l, h in d["headings"] if l <= 2))
        body = norm(d["full"])
        forms = [name] + ([alt] if alt else [])
        porn = re.compile(r"(?:" + "|".join(re.escape(norm(f).strip()) for f in forms) + r")\s+porn", re.I)
        if k == "Model-Profil":
            ok_p = bool(porn.search(norm(d["title"]))) and bool(porn.search(norm(" ".join(h for l, h in d["headings"] if l == 1))))
        elif k == "Video-Übersicht":
            ok_p = bool(porn.search(norm(d["title"]))) and bool(porn.search(head))
        else:
            ok_p = bool(porn.search(head))
        if not ok_p:
            miss[f"{k}: „{name} Porno“ fehlt in Title/H1/H2"].append(p)
        for f in forms:
            if norm(f).strip() not in (norm(d["title"]) if k == "Model-Profil" else body):
                miss[f"{k}: Schreibweise „{f}“ fehlt" + (" im Title" if k == "Model-Profil" else "")].append(p)
    n = sum(tot.values())
    bad = len({p for v in miss.values() for p in v})
    metrics["model_keyword_pages"] = n
    metrics["model_keyword_missing"] = bad
    F.check("Keyword Strategy", "Model-Keywords (Name + Porno, beide Schreibweisen) auf Profil/Übersicht/Videos", _ok(bad, n))
    for txt, ps in miss.items():
        F.add("P1", "Keywords", txt, len(ps), ps)


def repo_checks(F: Findings, metrics: dict, root: Path) -> None:
    """Code-Qualität, Geheimnisse im Repository, Pipeline-Zuverlässigkeit der letzten 14 Tage."""
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=60).stdout.split()
    except Exception:
        tracked = []
    pat = re.compile(r"(sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY|"
                     r"\"private_key\"\s*:\s*\"-----)")
    secrets = []
    for f in tracked:
        if f.startswith(("site/", "data/")) or not f.endswith((".py", ".yml", ".yaml", ".json", ".js", ".md", ".txt", ".html", ".env")):
            continue
        try:
            t = (root / f).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if pat.search(t):
            secrets.append(f)
    metrics["secrets_found"] = len(secrets)
    F.check("Security", "Keine Zugangsdaten im Repository", 1.0 if not secrets else 0.0)
    if secrets:
        F.add("P0", "Sicherheit", "Mögliche Zugangsdaten im Repository", len(secrets), secrets, auto=False)
    # Python kompiliert fehlerfrei
    r = subprocess.run(["python", "-m", "compileall", "-q", "seo", "scripts"], cwd=root, capture_output=True, text=True)
    F.check("Code Quality", "Python kompiliert fehlerfrei", 1.0 if r.returncode == 0 else 0.0)
    if r.returncode:
        F.add("P0", "Code", "Python-Code kompiliert nicht", 1, [r.stdout[-300:] + r.stderr[-300:]])
    # Pipeline-Protokolle der letzten 14 Tage
    since = date.today() - timedelta(days=14)
    errs, runs = [], 0
    for f in sorted((root / "reports").glob("*.log")):
        try:
            d = date.fromisoformat(f.name[:10])
        except ValueError:
            continue
        if d < since:
            continue
        runs += 1
        t = f.read_text(encoding="utf-8", errors="ignore")
        for line in t.splitlines():
            if "FEHLER" in line or "Traceback" in line:
                errs.append(f"{f.name}: {line.strip()[:120]}")
    metrics["pipeline_logs_14d"] = runs
    metrics["pipeline_errors_14d"] = len(errs)
    F.check("Maintainability", "Pipeline-Läufe ohne Fehler (14 Tage)", _ok(min(len(errs), runs), runs) if runs else None)
    if errs:
        F.add("P1", "Pipeline", "Fehler in Pipeline-Protokollen (14 Tage)", len(errs), errs[-5:])
    # Größte Python-Dateien (Wartbarkeit)
    big = sorted(((sum(1 for _ in open(p, encoding="utf-8")), p.name) for p in (root / "seo").glob("*.py")), reverse=True)
    metrics["largest_modules"] = [f"{n} ({l} Zeilen)" for l, n in big[:3]]
    F.check("Maintainability", "Module unter 1.200 Zeilen", _ok(sum(1 for l, _ in big if l > 1200), len(big)))
    tests = list(root.glob("tests/test_*.py"))
    F.check("Code Quality", "Automatische Tests vorhanden", 1.0 if tests else 0.0)
    if not tests:
        F.add("P3", "Code", "Keine automatischen Tests im Repository", 0)
    else:
        r = subprocess.run(["python", "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=root, capture_output=True,
                           text=True, timeout=600)
        m = re.search(r"Ran (\d+) test", r.stderr)
        metrics["tests"] = f"{m.group(1) if m else '?'} Tests, {'bestanden' if r.returncode == 0 else 'FEHLGESCHLAGEN'}"
        F.check("Code Quality", "Automatische Tests bestanden", 1.0 if r.returncode == 0 else 0.0)
        if r.returncode:
            F.add("P0", "Code", "Automatische Tests schlagen fehl", 1, [r.stderr[-400:]])


def workflow_checks(F: Findings, metrics: dict) -> None:
    """Erfolgsquote der GitHub-Workflows der letzten 14 Tage (nur im Runner mit GH_TOKEN)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        F.check("Scalability", "Workflow-Erfolgsquote 14 Tage", None, "nur im GitHub-Runner messbar")
        return
    since = (date.today() - timedelta(days=14)).isoformat()
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/runs?per_page=100&created=>={since}",
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    try:
        runs = json.loads(urllib.request.urlopen(req, timeout=30).read())["workflow_runs"]
    except Exception as e:
        F.check("Scalability", "Workflow-Erfolgsquote 14 Tage", None, f"API nicht erreichbar: {e}")
        return
    done = [r for r in runs if r["status"] == "completed" and r["conclusion"] != "cancelled"]
    ok = sum(1 for r in done if r["conclusion"] == "success")
    by = defaultdict(lambda: [0, 0])
    for r in done:
        by[r["name"]][0] += r["conclusion"] == "success"
        by[r["name"]][1] += 1
    metrics["workflows_14d"] = {k: f"{a}/{b}" for k, (a, b) in by.items()}
    F.check("Scalability", "Workflow-Erfolgsquote 14 Tage", ok / len(done) if done else None)
    failed = [f"{r['name']} {r['created_at'][:10]}" for r in done if r["conclusion"] != "success"]
    if failed:
        F.add("P1", "Pipeline", "Fehlgeschlagene Workflow-Läufe (14 Tage)", len(failed), failed)


# ---------------------------------------------------------------- Live-Prüfungen (nur im Runner sinnvoll)

def _fetch(url: str, method: str = "GET", follow: bool = True, timeout: int = 10):
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedir)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        r = opener.open(req, timeout=timeout)
        body = r.read(200000) if method == "GET" else b""
        return r.status, dict(r.headers), time.time() - t0, r.geturl(), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), time.time() - t0, url, b""
    except Exception as e:
        return None, {"error": str(e)}, time.time() - t0, url, b""


def live_checks(c: dict, F: Findings, metrics: dict, cfg: dict, withdrawn: list[str], sample_videos: int = 80) -> bool:
    base = c["base"]
    st0, *_ = _fetch(base + "/")
    if st0 is None:
        for cat in ("Technical SEO", "Crawling & Indexability", "Security"):
            F.check(cat, "Live-Prüfung", None, "Live-Seite von hier nicht erreichbar")
        metrics["live"] = "nicht erreichbar"
        return False
    pages = c["pages"]
    vids = [p for p, d in pages.items() if d["kind"] == "Videoseite"]
    random.seed(date.today().toordinal())
    sample = [p for p, d in pages.items() if d["kind"] != "Videoseite"] + random.sample(vids, min(sample_videos, len(vids)))
    bad, ttfb = [], []

    def one(p):
        s, h, dt, final, _ = _fetch(base + p, method="HEAD")
        if s in (405, 403, None):
            s, h, dt, final, _ = _fetch(base + p)
        return p, s, dt, final
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        for p, s, dt, final in ex.map(one, sample):
            ttfb.append(dt)
            if s != 200 or urlparse(final).path != p:
                bad.append(f"{p} → {s} {final if urlparse(final).path != p else ''}".strip())
    metrics.update(live_checked=len(sample), live_bad=len(bad), ttfb_median_ms=round(st.median(ttfb) * 1000),
                   ttfb_p90_ms=round(sorted(ttfb)[int(len(ttfb) * 0.9) - 1] * 1000))
    F.check("Crawling & Indexability", "Live: Seiten liefern 200 ohne Weiterleitung", _ok(len(bad), len(sample)))
    F.check("Core Web Vitals", "Live: Serverantwort (Median < 600 ms)", 1.0 if st.median(ttfb) < 0.6 else 0.6 / st.median(ttfb))
    if bad:
        F.add("P0", "Live", "Seiten antworten nicht mit 200", len(bad), bad)
    # Fehlerseiten, Weiterleitungen, HTTPS
    s404, *_ = _fetch(base + "/diese-seite-gibt-es-nicht-" + str(int(time.time())) + "/")
    F.check("Technical SEO", "Unbekannte URL liefert 404 (kein Soft-404)", 1.0 if s404 == 404 else 0.0)
    if s404 != 404:
        F.add("P1", "Technik", f"Unbekannte URL liefert {s404} statt 404 (Soft-404)", 1)
    host = urlparse(base).netloc
    checks = {
        "http → https": ("http://" + host + "/", base + "/"),
        "ohne www → www": ("https://" + host.replace("www.", "", 1) + "/", base + "/"),
        "ohne Slash → mit Slash": (base + "/models", base + "/models/"),
    }
    redir = {}
    for name, (src, want) in checks.items():
        s, h, _, final, _ = _fetch(src, follow=False)
        loc = h.get("Location") or h.get("location") or ""
        ok = s in (301, 308) and urljoin(src, loc).rstrip("/") == want.rstrip("/")
        redir[name] = f"{s} → {loc or '-'}"
        F.check("Technical SEO", f"Weiterleitung {name} (301)", 1.0 if ok else 0.0)
        if not ok:
            F.add("P1" if "www" in name else "P2", "Technik", f"Weiterleitung {name} fehlt oder falsch ({s} {loc})", 1,
                  auto="www" not in name)
    metrics["redirects"] = redir
    # Zurückgezogene Videoseiten: 404 (gewollt) – Info, ob Google sie noch kennt, liefert die Search Console
    wd = [w for w in withdrawn[:20]]
    wd_st = {w: _fetch(base + w)[0] for w in wd}
    wd_bad = [f"{w} → {s}" for w, s in wd_st.items() if s == 200]   # nur echte Auslieferung zählt (keine Drosselung/Fehler)
    metrics["withdrawn_status"] = dict(Counter(str(s) for s in wd_st.values()))
    if wd_bad:
        F.add("P2", "Indexierung", "Zurückgezogene Videoseiten sind noch erreichbar", len(wd_bad), wd_bad)
    # Sicherheitsheader live
    _, hdr, *_ = _fetch(base + "/")
    hl = {k.lower(): v for k, v in hdr.items()}
    metrics["live_headers"] = {k: hl.get(k, "–") for k in ("strict-transport-security", "content-security-policy",
                                                              "x-content-type-options", "x-frame-options",
                                                              "referrer-policy", "permissions-policy", "cache-control")}
    F.check("Security", "Live: HSTS aktiv", 1.0 if "strict-transport-security" in hl else 0.0)
    rb = _fetch(base + "/robots.txt")
    F.check("Crawling & Indexability", "Live: robots.txt erreichbar", 1.0 if rb[0] == 200 else 0.0)
    return True



JS_METRICS = """() => new Promise(done => {
      let lcp = 0, cls = 0;
      new PerformanceObserver(l => { for (const e of l.getEntries()) lcp = e.startTime; }).observe({type: 'largest-contentful-paint', buffered: true});
      new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) cls += e.value; }).observe({type: 'layout-shift', buffered: true});
      setTimeout(() => {
        const nav = performance.getEntriesByType('navigation')[0] || {};
        const fcp = (performance.getEntriesByName('first-contentful-paint')[0] || {}).startTime || 0;
        const res = performance.getEntriesByType('resource');
        const bytes = res.reduce((a, r) => a + (r.transferSize || 0), 0) + (nav.transferSize || 0);
        const vw = document.documentElement.clientWidth;
        const overflow = document.documentElement.scrollWidth > vw + 1;
        const small = [...document.querySelectorAll('a,button')].filter(e => {
          const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (r.height < 24 || r.width < 24) && getComputedStyle(e).display !== 'inline';
        }).length;
        const tiny = [...document.querySelectorAll('p,li,a,span')].filter(e => parseFloat(getComputedStyle(e).fontSize) < 12 && e.textContent.trim().length > 3).length;
        const cta = [...document.querySelectorAll('a[rel~=sponsored].btn')].map(e => e.getBoundingClientRect().top + scrollY)[0];
        done({lcp, cls, fcp, ttfb: nav.responseStart || 0, bytes, requests: res.length + 1, overflow, small, tiny,
             cta_top: cta === undefined ? null : cta, vh: innerHeight});
      }, 2500);
    })"""


def lab_measure(url: str, mode: str) -> dict:
    """Eine Labormessung (wird als eigener Prozess gestartet: python -m seo.audit lab <url> <mobil|desktop>)."""
    from playwright.sync_api import sync_playwright
    vp = {"width": 390, "height": 844} if mode == "mobil" else {"width": 1366, "height": 900}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport=vp, user_agent=UA, is_mobile=mode == "mobil", device_scale_factor=2 if mode == "mobil" else 1)
        ctx.add_init_script("try{localStorage.setItem('ga_consent','denied')}catch(e){}")
        ctx.set_default_timeout(30000)
        pg = ctx.new_page()
        if mode == "mobil":
            try:
                cdp = ctx.new_cdp_session(pg)
                cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 150,
                         "downloadThroughput": 1.6e6 / 8, "uploadThroughput": 750e3 / 8})
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
            except Exception:
                pass
        pg.goto(url, wait_until="load", timeout=45000)
        res = pg.evaluate(JS_METRICS)
        b.close()
    return res


def browser_checks(c: dict, F: Findings, metrics: dict) -> None:
    """Labormessung mit Chromium (Playwright): LCP, CLS, FCP, TTFB, Gewicht, Überlauf, Tippziele – mobil + Desktop."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        F.check("Core Web Vitals", "Labormessung", None, "Playwright nicht installiert")
        return
    pages = c["pages"]
    pick = {}
    for kind in ("Startseite", "Model-Profil", "Videoseite", "Video-Übersicht", "Ratgeber/Themen", "Hub"):
        cand = sorted(p for p, d in pages.items() if d["kind"] == kind)
        if cand:
            pick[kind] = cand[len(cand) // 2]
    out = {}
    import sys
    for mode in ("mobil", "desktop"):
        for kind, p in pick.items():
            # Jede Messung in einem eigenen Prozess mit hartem Zeitlimit (Playwright kann sonst hängen bleiben)
            try:
                r = subprocess.run([sys.executable, "-m", "seo.audit", "lab", c["base"] + p, mode], capture_output=True,
                                   text=True, timeout=90)
                line = (r.stdout.strip().splitlines() or ["{}"])[-1]
                out[f"{kind} ({mode})"] = json.loads(line) if line.startswith("{") else {"error": (r.stderr or "keine Ausgabe")[-120:]}
            except subprocess.TimeoutExpired:
                out[f"{kind} ({mode})"] = {"error": "Zeitlimit 90 s"}
            except Exception as e:
                out[f"{kind} ({mode})"] = {"error": str(e)[:120]}
    metrics["lab"] = out
    ok = [v for v in out.values() if "error" not in v]
    if not ok:
        F.check("Core Web Vitals", "Labormessung", None, "Messung fehlgeschlagen")
        return
    mob = {k: v for k, v in out.items() if "mobil" in k and "error" not in v}
    lcp_ok = sum(1 for v in mob.values() if v["lcp"] <= 2500)
    cls_ok = sum(1 for v in ok if v["cls"] <= 0.1)
    F.check("Core Web Vitals", "Labor mobil: LCP ≤ 2,5 s (4× CPU, langsames 4G)", lcp_ok / len(mob) if mob else None)
    F.check("Core Web Vitals", "Labor: CLS ≤ 0,1", cls_ok / len(ok))
    F.check("Mobile Experience", "Kein horizontales Scrollen (390 px)", sum(1 for v in mob.values() if not v["overflow"]) / len(mob) if mob else None)
    F.check("Mobile Experience", "Tippziele ≥ 24 px", sum(1 for v in mob.values() if v["small"] <= 3) / len(mob) if mob else None)
    F.check("UX/UI", "Lesbare Schriftgrößen (≥ 12 px)", sum(1 for v in ok if v["tiny"] <= 3) / len(ok))
    above = [k for k, v in mob.items() if v.get("cta_top") is not None and v["cta_top"] < v["vh"] * 1.5]
    with_cta = [k for k, v in mob.items() if v.get("cta_top") is not None]
    F.check("Conversion Optimization", "Mobil: Haupt-CTA in den ersten 1,5 Bildschirmhöhen",
            len(above) / len(with_cta) if with_cta else None)
    low_cta = [f"{k}: erster Button bei {v['cta_top']:.0f} px (Bildschirm {v['vh']} px)" for k, v in mob.items()
               if v.get("cta_top") is not None and v["cta_top"] >= v["vh"] * 1.5]
    if low_cta:
        F.add("P2", "Conversion", "Mobil erscheint der erste Visit-X-Button erst nach mehr als 1,5 Bildschirmhöhen", len(low_cta),
              low_cta, "Button weiter oben platzieren (Conversion-Funnel → Freigabe)", auto=False)
    for k, v in mob.items():
        if v["lcp"] > 2500:
            F.add("P1", "Performance", f"LCP {v['lcp'] / 1000:.1f} s auf {k}", 1)
        if v["overflow"]:
            F.add("P1", "Mobil", f"Horizontales Scrollen auf {k}", 1)
        if v["small"] > 3:
            F.add("P3", "Mobil", f"{v['small']} kleine Tippziele (< 24 px) auf {k}", v["small"])
    for k, v in out.items():
        if "error" not in v and v["cls"] > 0.1:
            F.add("P1", "Performance", f"CLS {v['cls']:.2f} auf {k}", 1)


# ---------------------------------------------------------------- Search Console

def gsc_checks(cfg: dict, F: Findings, metrics: dict, c: dict) -> None:
    try:
        from . import gsc
        from .status import _query, _tot
        svc = gsc.service(cfg)
    except Exception as e:
        svc = None
    if not svc:
        for cat in ("Search Intent Alignment", "Keyword Strategy"):
            F.check(cat, "Search-Console-Daten", None, "keine Zugangsdaten in dieser Umgebung")
        metrics["gsc"] = "nicht verfügbar"
        return
    end = date.today() - timedelta(days=1)
    rows = _query(cfg, svc, end - timedelta(days=13), end, ["date"])
    if rows:
        end = date.fromisoformat(max(r["keys"][0] for r in rows))
    cur = _tot(_query(cfg, svc, end - timedelta(days=13), end, ["date"]))
    prev = _tot(_query(cfg, svc, end - timedelta(days=27), end - timedelta(days=14), ["date"]))
    metrics["gsc_14d"] = cur
    metrics["gsc_prev_14d"] = prev
    metrics["gsc_end"] = end.isoformat()
    qp = _query(cfg, svc, end - timedelta(days=27), end, ["query", "page"], limit=25000)
    byq = defaultdict(list)
    for r in qp:
        byq[r["keys"][0]].append(r)
    # Kannibalisierung: ≥ 2 URLs mit je ≥ 20 % der Impressionen einer Suchanfrage (min. 30 Impressionen)
    cann = []
    for qq, rs in byq.items():
        tot = sum(r["impressions"] for r in rs)
        if tot < 30:
            continue
        strong = [r for r in rs if r["impressions"] / tot >= 0.2]
        if len(strong) >= 2 and min(r["position"] for r in strong) > 3:   # Top-3 mit mehreren URLs = Mehrfach-Listing
            cann.append((tot, qq, [urlparse(r["keys"][1]).path for r in strong]))
    cann.sort(reverse=True)
    relevant = sum(1 for rs in byq.values() if sum(r["impressions"] for r in rs) >= 30)
    metrics["cannibalized_queries"] = len(cann)
    F.check("Keyword Strategy", "Keine Kannibalisierung (≥ 30 Impr.)", _ok(len(cann), relevant) if relevant else None)
    if cann:
        F.add("P1", "Keywords", "Mehrere eigene URLs konkurrieren um dieselbe Suchanfrage", len(cann),
              [f"„{q}“ ({t} Impr.): {', '.join(ps[:3])}" for t, q, ps in cann], "Interne Links + Titel auf eine URL ausrichten")
    # Suchintention: passt die Top-Suchanfrage einer Seite zu ihrem Titel?
    bypage = defaultdict(list)
    for r in qp:
        bypage[urlparse(r["keys"][1]).path].append(r)
    match = tot_p = 0
    mism = []
    for p, rs in bypage.items():
        if p not in c["pages"]:
            continue
        top = max(rs, key=lambda r: r["impressions"])
        if top["impressions"] < 20:
            continue
        tot_p += 1
        qt = set(re.findall(r"[a-zäöüß0-9]{3,}", top["keys"][0].lower()))
        tt = set(re.findall(r"[a-zäöüß0-9]{3,}", (c["pages"][p]["title"] + " " + p.replace("-", " ")).lower()))
        if qt & tt:
            match += 1
        else:
            mism.append(f"{p}: „{top['keys'][0]}“")
    F.check("Search Intent Alignment", "Top-Suchanfrage passt zu Titel/URL", match / tot_p if tot_p else None)
    if mism:
        F.add("P2", "Suchintention", "Seiten ranken hauptsächlich für Begriffe, die nicht im Titel stehen", len(mism), mism)
    # CTR-Chancen und Positions-Chancen
    pq = _query(cfg, svc, end - timedelta(days=27), end, ["page"], limit=5000)
    low = [r for r in pq if r["impressions"] >= 200 and r["position"] <= 10 and r["ctr"] < 0.03]
    metrics["low_ctr_pages"] = len(low)
    F.check("On-Page SEO", "Top-10-Seiten mit CTR ≥ 3 %", _ok(len(low), sum(1 for r in pq if r["impressions"] >= 200 and r["position"] <= 10)) if pq else None)
    if low:
        F.add("P1", "CTR", "Viele Impressionen in den Top 10, aber CTR < 3 % (Titel/Description verbessern)", len(low),
              [f"{urlparse(r['keys'][0]).path} – {int(r['impressions'])} Impr., Pos. {r['position']:.1f}, CTR {r['ctr']:.1%}" for r in low])
    q = _query(cfg, svc, end - timedelta(days=27), end, ["query"], limit=5000)
    chances = sorted([r for r in q if 4 <= r["position"] <= 20 and r["impressions"] >= 30], key=lambda r: -r["impressions"])
    metrics["chance_queries"] = len(chances)
    metrics["top_chances"] = [f"{r['keys'][0]} – Pos. {r['position']:.1f}, {int(r['impressions'])} Impr." for r in chances[:10]]
    # Markenanteil (Modelnamen/Visit-X) – Information für die Keyword-Map
    model_names = {d["path"].strip("/") for d in c["pages"].values() if d["kind"] == "Model-Profil"}
    clusters = Counter()
    for r in q:
        k = r["keys"][0].lower()
        if any(m.replace("-", "") in k.replace(" ", "") for m in model_names):
            clusters["A Modelnamen"] += r["clicks"]
        elif "visit" in k or "vx" in k.split():
            clusters["B Plattform (Visit-X)"] += r["clicks"]
        else:
            clusters["C Allgemein"] += r["clicks"]
    metrics["clicks_by_cluster_28d"] = dict(clusters)
    pq_rows = [r for r in q if "porn" in r["keys"][0].lower()]
    metrics["porn_queries_28d"] = {"anzahl": len(pq_rows), "klicks": int(sum(r["clicks"] for r in pq_rows)),
                                   "impressionen": int(sum(r["impressions"] for r in pq_rows)),
                                   "top": [f"{r['keys'][0]} – {int(r['clicks'])} Klicks, {int(r['impressions'])} Impr., Pos. {r['position']:.1f}"
                                           for r in sorted(pq_rows, key=lambda r: -r["impressions"])[:10]]}
    # Indexierung aus den Prüfständen der Pipeline
    idx_known = idx_yes = 0
    for f in Path(cfg.get("_data", "data")).joinpath("videos").glob("*.json"):
        if f.name.startswith("_"):
            continue
        try:
            for v in json.loads(f.read_text(encoding="utf-8")).get("videos", []):
                if v.get("status") == "published" and "indexed" in v:
                    idx_known += 1
                    idx_yes += bool(v["indexed"])
        except Exception:
            pass
    metrics["video_index_sample"] = f"{idx_yes}/{idx_known}"
    F.check("Crawling & Indexability", "Videoseiten indexiert (geprüfte Stichprobe)", idx_yes / idx_known if idx_known else None)


# ---------------------------------------------------------------- Bewertung und Bericht

def scores(F: Findings) -> tuple[dict, float | None, float]:
    out = {}
    for cat in WEIGHTS:
        vals = [r for _, r, _ in F.checks.get(cat, []) if r is not None]
        out[cat] = round(100 * st.mean(vals)) if vals else None
    have = {k: v for k, v in out.items() if v is not None}
    wsum = sum(WEIGHTS[k] for k in have)
    total = round(sum(WEIGHTS[k] * v for k, v in have.items()) / wsum) if wsum else None
    return out, total, wsum / 100


def _label(s):
    if s is None:
        return "nicht verifiziert"
    return "sehr gut" if s >= 90 else "gut" if s >= 75 else "ausbaufähig" if s >= 50 else "kritisch"


def _prev(root: Path, today: str) -> dict | None:
    d = root / "reports" / "audit"
    files = sorted(p for p in d.glob("*.json") if p.stem < today) if d.exists() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    return str(v)


def report(F: Findings, metrics: dict, sc: dict, total, coverage, prev: dict | None, live: bool) -> str:
    today = date.today()
    L = [f"# Audit top100amateure.com – {today:%d.%m.%Y}", "",
         "_Zweiwöchentliche Kontrolle nach dem Masterprompt. Alle Werte sind gemessen; Bereiche ohne Messdaten stehen als "
         "„nicht verifiziert“. Der Audit ändert nichts an der Website._", ""]
    # 1 Executive Summary
    p0 = [i for i in F.items if i["prio"] == "P0"]
    p1 = [i for i in F.items if i["prio"] == "P1"]
    L += ["## 1. Executive Summary", ""]
    L.append(f"- **Gesamtnote: {total if total is not None else '–'}/100** ({_label(total)}), "
             f"Messabdeckung {coverage:.0%} der Gewichtung" + (f" · vorher {prev['total']}/100" if prev and prev.get("total") is not None else ""))
    L.append(f"- {metrics['urls_total']} URLs im Build, davon {metrics['urls_indexable']} indexierbar, "
             f"{metrics['sitemap_urls']} in der Sitemap")
    L.append(f"- Funde: **{len(p0)} kritisch (P0)**, {len(p1)} sehr hoch (P1), "
             f"{sum(1 for i in F.items if i['prio'] == 'P2')} mittel (P2), {sum(1 for i in F.items if i['prio'] == 'P3')} niedrig (P3)")
    if "gsc_14d" in metrics:
        a, b = metrics["gsc_14d"], metrics["gsc_prev_14d"]
        dl = f" (Vorperiode {b['clicks']})" if b["clicks"] else ""
        L.append(f"- Google, 14 Tage bis {metrics['gsc_end']}: {a['clicks']} Klicks{dl}, {a['impr']} Impressionen, "
                 f"CTR {a['ctr']:.1%}, Ø Position {a['pos']:.1f}" if a["pos"] else "- Google: keine Daten")
    if not live:
        L.append("- Live-Prüfungen (Statuscodes, Weiterleitungen, Header, Labormessung) liefen nicht – nur im GitHub-Runner möglich.")
    L.append("")
    # 2 Bewertung
    L += ["## 2. Bewertung je Bereich", "", "| Bereich | Punkte | Einstufung | Gewicht | vorher |", "|---|---|---|---|---|"]
    for cat, w in WEIGHTS.items():
        s = sc[cat]
        pv = prev["scores"].get(cat) if prev else None
        L.append(f"| {cat} | {s if s is not None else '–'} | {_label(s)} | {w} | {pv if pv is not None else '–'} |")
    L += ["", "Gewichtung: Inhalte und Indexierung (je 10), Technik und Affiliate-Umsatz (je 8), On-Page (7), Conversion und "
          "interne Links (je 6), Suchintention, Keywords, Core Web Vitals, Mobil (je 5), übrige 1–4. Die Gesamtnote wird nur über "
          "gemessene Bereiche gebildet und neu normiert.", ""]
    L += ["<details><summary>Einzelprüfungen</summary>", "", "| Bereich | Prüfung | Ergebnis |", "|---|---|---|"]
    for cat in WEIGHTS:
        for name, r, det in F.checks.get(cat, []):
            L.append(f"| {cat} | {name} | {'nicht verifiziert' + (' – ' + det if det else '') if r is None else f'{r:.0%}'} |")
    L += ["", "</details>", ""]
    # 3 Schwachstellen nach Priorität
    L += ["## 3. Prioritätenmatrix", ""]
    names = {"P0": "P0 – kritisch", "P1": "P1 – sehr hoch", "P2": "P2 – mittel", "P3": "P3 – niedrig"}
    for pr in ("P0", "P1", "P2", "P3"):
        items = [i for i in F.items if i["prio"] == pr]
        if not items:
            continue
        L += [f"### {names[pr]}", ""]
        for i in items:
            how = "sicher automatisch behebbar" if i["auto"] else "**Freigabe nötig**"
            L.append(f"- **{i['area']}:** {i['text']}" + (f" – {i['count']}" if i["count"] else "") + f" · {how}"
                     + (f" · {i['fix']}" if i["fix"] else ""))
            for e in i["examples"]:
                L.append(f"  - `{e}`")
        L.append("")
    if not F.items:
        L += ["Keine Funde.", ""]
    # 6 Vorher/Nachher
    keys = [("urls_total", "URLs im Build"), ("sitemap_urls", "Sitemap-URLs"), ("broken_links", "kaputte interne Links"),
            ("orphans", "verwaiste Seiten"), ("depth_max", "max. Klicktiefe"), ("dup_titles", "doppelte Titel"),
            ("missing_desc", "fehlende Descriptions"), ("h1_bad", "Seiten mit H1-Fehler"), ("img_no_alt", "Bilder ohne Alt"),
            ("ld_errors", "JSON-LD-Fehler"), ("similarity_median", "Ähnlichkeit (Median)"), ("near_duplicates", "Beinahe-Duplikate"),
            ("explicit_pages", "Seiten mit expliziten Wörtern"), ("model_keyword_missing", "Seiten ohne Model-Pflichtkeywords"), ("affiliate_param_bad", "Affiliate-Links ohne Parameter"),
            ("cannibalized_queries", "kannibalisierte Suchanfragen"), ("low_ctr_pages", "Top-10-Seiten mit CTR < 3 %"),
            ("ttfb_median_ms", "TTFB Median (ms)"), ("live_bad", "Live-Fehler (Stichprobe)"),
            ("pipeline_errors_14d", "Pipeline-Fehler 14 Tage")]
    L += ["## 4. Vorher-Nachher", ""]
    if prev:
        L += [f"Vergleich mit dem Audit vom {prev['date']}.", "", "| Kennzahl | vorher | jetzt |", "|---|---|---|"]
        for k, lab in keys:
            if k in metrics or k in prev.get("metrics", {}):
                L.append(f"| {lab} | {_fmt(prev['metrics'].get(k, '–'))} | {_fmt(metrics.get(k, '–'))} |")
    else:
        L += ["Erster Audit – Ausgangszustand für den nächsten Vergleich:", "", "| Kennzahl | Wert |", "|---|---|"]
        for k, lab in keys:
            if k in metrics:
                L.append(f"| {lab} | {_fmt(metrics[k])} |")
    L.append("")
    # 5 Inhalte im Detail
    L += ["## 5. Inhalte und Struktur", ""]
    L.append("| Seitentyp | Anzahl | Wörter (Median) | Textbaustein-Anteil | eingehende Links (Median) |")
    L.append("|---|---|---|---|---|")
    for k, n in sorted(metrics["by_kind"].items(), key=lambda x: -x[1]):
        L.append(f"| {k} | {n} | {metrics['words_median_by_kind'].get(k, '–')} | "
                 f"{metrics['boilerplate_share_by_kind'].get(k, 0):.0%} | {metrics['inbound_median_by_kind'].get(k, '–')} |")
    L.append("")
    if metrics.get("clicks_by_cluster_28d"):
        L += ["**Klicks nach Keyword-Cluster (28 Tage):** " + " · ".join(f"{k}: {v:.0f}" for k, v in sorted(metrics["clicks_by_cluster_28d"].items())), ""]
    if metrics.get("porn_queries_28d"):
        pqm = metrics["porn_queries_28d"]
        L += [f"**Suchanfragen mit „Porno/Porn“ (28 Tage):** {pqm['anzahl']} Anfragen, {pqm['klicks']} Klicks, "
              f"{pqm['impressionen']} Impressionen", ""] + [f"- {x}" for x in pqm["top"]] + [""]
    if metrics.get("top_chances"):
        L += ["**Ranking-Chancen (Position 4–20, meiste Impressionen):**", ""] + [f"- {x}" for x in metrics["top_chances"]] + [""]
    # 6 Technik
    L += ["## 6. Technik, Performance, Sicherheit", ""]
    if metrics.get("lab"):
        L += ["| Seite | LCP | CLS | FCP | TTFB | Gewicht | Requests |", "|---|---|---|---|---|---|---|"]
        for k, v in metrics["lab"].items():
            if "error" in v:
                L.append(f"| {k} | Fehler: {v['error']} | | | | | |")
            else:
                L.append(f"| {k} | {v['lcp'] / 1000:.2f} s | {v['cls']:.3f} | {v['fcp'] / 1000:.2f} s | {v['ttfb']:.0f} ms | "
                         f"{v['bytes'] / 1024:.0f} KB | {v['requests']} |")
        L += ["", "_Labormessung (Chromium, mobil mit 4× CPU-Drosselung und langsamem 4G). Felddaten echter Nutzer (CrUX) "
              "sind für die Domain noch nicht verfügbar → nicht verifiziert._", ""]
    if metrics.get("redirects"):
        L.append("- Weiterleitungen: " + "; ".join(f"{k}: {v}" for k, v in metrics["redirects"].items()))
    if metrics.get("live_headers"):
        L.append("- Live-Header: " + "; ".join(f"{k}: {'ja' if v != '–' else 'fehlt'}" for k, v in metrics["live_headers"].items() if k != "cache-control"))
    if metrics.get("workflows_14d"):
        L.append("- Workflows 14 Tage (erfolgreich/gesamt): " + "; ".join(f"{k} {v}" for k, v in metrics["workflows_14d"].items()))
    L.append(f"- Externe Skripte: {', '.join(metrics.get('external_scripts') or []) or 'keine (GA lädt erst nach Einwilligung)'}")
    L.append("")
    # 7 nicht verifiziert
    L += ["## 7. Nicht verifiziert / offen", "",
          "- Affiliate-Umsatz je Landingpage, Registrierungs- und Kaufrate: VX Cash liefert keine Daten je Seite (eine "
          "Kampagne für alle Seiten, keine API-Anbindung). Möglich wäre es mit eigenen ws-Kampagnen je Seitentyp (Freigabe).",
          "- Affiliate-Klickrate: GA4-Ereignis `affiliate_click` ist eingerichtet, die Auswertung erfolgt in GA4 "
          "(nur Nutzer mit Einwilligung).",
          "- Suchvolumen und Wettbewerb: keine Keyword-Datenbank angebunden; bewertet wird mit Search-Console-Daten.",
          "- Barrierefreiheit: Kontraste und Screenreader-Bedienung nicht automatisch geprüft.",
          "- Rechtliche Fragen (Jugendschutz, Werbekennzeichnung) werden technisch geprüft, ersetzen aber keine Rechtsberatung.", ""]
    L += ["## 8. Nächste Schritte", "",
          "1. P0-Funde sofort beheben (Claude-Sitzung: „Audit umsetzen“).",
          "2. P1-Funde mit automatischer Behebung in derselben Sitzung umsetzen, danach erneuter Audit-Lauf.",
          "3. Punkte mit **Freigabe nötig** (Löschen, noindex, Weiterleitungen, Affiliate-Links, Rechtliches) gesammelt vorlegen.",
          "4. Ranking-Chancen (Position 4–20) gezielt mit Inhalt und internen Links stärken.", ""]
    return "\n".join(L)


def run(cfg: dict, store, root: Path | None = None, live: bool = True, browser: bool = True, log=None) -> tuple[str, Path]:
    root = Path(root or ".")
    logf = root / "reports" / f"{date.today().isoformat()}-audit.log"
    t_start = time.time()

    def log(msg, _p=print):   # Fortschritt auch in Datei (Runner-Logs sind von außen nicht lesbar)
        line = f"[{time.time() - t_start:6.0f}s] {msg}"
        _p(line, flush=True)
        with logf.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    F = Findings()
    metrics: dict = {}
    import faulthandler
    _fh = (root / "reports" / f"{date.today().isoformat()}-audit-stack.log").open("w", encoding="utf-8")
    faulthandler.dump_traceback_later(300, repeat=True, file=_fh)   # Diagnose bei Hängern: Stacks alle 5 min
    import signal
    import traceback

    class _Timeout(Exception):
        pass

    def _alarm(*_):
        raise _Timeout()
    signal.signal(signal.SIGALRM, _alarm)

    def stage(name: str, cat: str, limit: int, fn, *a):
        """Führt eine Prüfstufe mit Zeitlimit aus; Fehler oder Zeitüberschreitung landen im Bericht statt den Lauf zu stoppen."""
        log(f"{name} …")
        signal.alarm(limit)
        try:
            r = fn(*a)
            log(f"{name} fertig")
            return r
        except _Timeout:
            log(f"{name}: Zeitlimit {limit} s überschritten")
            F.check(cat, name, None, f"Zeitlimit {limit} s überschritten")
            F.add("P2", "Audit", f"Prüfstufe „{name}“ abgebrochen (Zeitlimit {limit} s)", 1, auto=True)
        except Exception as e:
            log(f"{name} FEHLER: {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
            F.check(cat, name, None, f"Fehler: {type(e).__name__}: {e}")
        finally:
            signal.alarm(0)
        return None

    log("Audit: lokaler Crawl …")
    c = crawl_local(cfg, store, root)
    offline_checks(c, F, cfg, metrics)
    model_keyword_checks(c, F, cfg, store, metrics)
    log(f"Audit: Offline-Prüfungen fertig ({metrics['urls_total']} URLs)")
    stage("Repository und Tests", "Code Quality", 300, repo_checks, F, metrics, root)
    stage("Workflow-Statistik", "Scalability", 120, workflow_checks, F, metrics)
    # zurückgezogene Videoseiten (für die Live-Prüfung)
    withdrawn = []
    for f in Path(cfg.get("_data", "data")).joinpath("videos").glob("*.json"):
        if f.name.startswith("_"):
            continue
        try:
            for v in json.loads(f.read_text(encoding="utf-8")).get("videos", []):
                if v.get("status") == "withdrawn" and v.get("vx_slug"):
                    withdrawn.append(f"/{f.stem}/videos/{v['id']}-{v['vx_slug']}/")
        except Exception:
            pass
    metrics["withdrawn_videos"] = len(withdrawn)
    ok_live = False
    if live:
        ok_live = bool(stage("Live-Prüfung", "Technical SEO", 300, live_checks, c, F, metrics, cfg, withdrawn))
        log(f"Live: erreichbar {ok_live}, Fehler {metrics.get('live_bad')}, TTFB {metrics.get('ttfb_median_ms')} ms")
        if ok_live and browser:
            stage("Labormessung", "Core Web Vitals", 420, browser_checks, c, F, metrics)
    stage("Search Console", "Keyword Strategy", 300, gsc_checks, cfg, F, metrics, c)
    faulthandler.cancel_dump_traceback_later()
    _fh.close()
    sc, total, coverage = scores(F)
    today = date.today().isoformat()
    prev = _prev(root, today)
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    F.items.sort(key=lambda i: (order[i["prio"]], -i["count"]))
    md = report(F, metrics, sc, total, coverage, prev, ok_live)
    (root / "reports" / "audit").mkdir(parents=True, exist_ok=True)
    js = {"date": today, "total": total, "coverage": coverage, "scores": sc, "metrics": metrics, "findings": F.items}
    (root / "reports" / "audit" / f"{today}.json").write_text(json.dumps(js, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    path = root / "reports" / f"{today}-audit.md"
    path.write_text(md, encoding="utf-8")
    log(f"Audit: Gesamtnote {total}, {len(F.items)} Funde → {path}")
    return md, path


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "lab":
        try:
            print(json.dumps(lab_measure(sys.argv[2], sys.argv[3])))
        except Exception as e:
            print(json.dumps({"error": f"{type(e).__name__}: {str(e)[:100]}"}))
