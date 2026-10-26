"""Statische Seite aus den gespeicherten Inhalten bauen (HTML, Sitemap, robots.txt)."""
from __future__ import annotations

import hashlib
import re
import shutil
import zlib
from datetime import date
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import images as imgmod
from . import videos as vidmod
from . import models as modelmod
from .quality import article_text


def md(text: str) -> str:
    """Markdown -> HTML. Im Fließtext sind nur H3/H4 erlaubt: H1 und H2 setzt das Template (saubere Gliederung)."""
    text = re.sub(r"(?m)^#{1,2}\s+", "### ", text or "")
    text = re.sub(r"(?m)^#{5,6}\s+", "#### ", text)
    return markdown.markdown(text, extensions=["sane_lists"])


def plain(text: str) -> str:
    return text.replace("**", "").replace("*", "").replace("#", "").strip()


def affiliate_url(cfg: dict, meta: dict) -> str:
    """Linkziel pro Seite: Themen-Link falls konfiguriert, sonst Standardlink; {subid} = Nummer der Seite."""
    aff = cfg["affiliate"]
    template = (aff.get("topic_links") or {}).get(meta.get("topic")) or aff["url_template"]
    return template.replace("{subid}", str(meta.get("subid") or meta["slug"]))


def next_subid(cfg: dict, pages: dict) -> int:
    used = [p["subid"] for p in pages.values() if isinstance(p.get("subid"), int)]
    return max(used) + 1 if used else int(cfg["affiliate"].get("subid_start", 1))


def fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def related_slugs(contents: dict[str, dict], n: int = 5) -> dict[str, list[str]]:
    slugs = list(contents)
    if len(slugs) < 2:
        return {s: [] for s in slugs}
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    matrix = TfidfVectorizer(sublinear_tf=True).fit_transform([article_text(contents[s]) for s in slugs])
    sims = cosine_similarity(matrix)
    out = {}
    for i, s in enumerate(slugs):
        order = sims[i].argsort()[::-1]
        out[s] = [slugs[j] for j in order if j != i][:n]
    return out


SECTIONS = [
    {"key": "models", "path": "models", "label": "Top-Models", "color": "#be123c", "topics": ["profile"],
     "title": "Top-Models bei Visit-X: alle Amateur-Models im Überblick",
     "intro": "Die beliebtesten Amateur-Models bei Visit-X. Jedes Model hat eine eigene Seite mit Steckbrief, Videos, Livecam und Chat."},
    {"key": "angebote", "path": "live-cams-videos", "label": "Live-Cams & Kategorien", "color": "#ea580c",
     "topics": ["livecams", "webcam-models", "videos", "amateure", "nischen"],
     "title": "Live-Cams, Amateur-Videos und Kategorien bei Visit-X",
     "intro": "Live-Cams, Webcam-Models, Videos und Amateur-Kategorien: was es gibt, wie es funktioniert und wie du das Passende findest."},
    {"key": "ratgeber", "path": "ratgeber", "label": "Ratgeber", "color": "#0f766e",
     "topics": ["preise", "gratis", "sicherheit", "ablauf", "technik", "vergleich", "dach", "grundlagen"],
     "title": "Ratgeber: Kosten, Tokens, Anmeldung und Sicherheit bei Visit-X",
     "intro": "Kosten und Tokens, Zahlungsarten, Anmeldung, Sicherheit und Vergleiche: kurze, klare Antworten auf die häufigsten Fragen."},
]


def section_of(topic: str) -> dict:
    return next((x for x in SECTIONS if topic in x["topics"]), SECTIONS[-1])


def _fix_case(c: dict, primary: str) -> dict:
    """Kleingeschriebenes Haupt-Keyword im Fließtext ('**lana grey**') als Namen schreiben ('**Lana Grey**')."""
    import re as _r
    if not primary or primary != primary.lower():
        return c
    proper = " ".join(w[:1].upper() + w[1:] for w in primary.split())
    rx = _r.compile(rf"(?<![\w/.-]){_r.escape(primary)}(?![\w/.-])")
    bold_kw = _r.compile(r"\*\*([a-zäöüß0-9][a-zäöüß0-9 \-]{2,60})\*\*")   # fett gesetzte Suchvarianten entschärfen
    def fx(v):
        if isinstance(v, str):
            return bold_kw.sub(r"\1", rx.sub(proper, v))
        if isinstance(v, list):
            return [fx(x) for x in v]
        if isinstance(v, dict):
            return {k: (fx(x) if k not in ("slug", "url", "image") else x) for k, x in v.items()}
        return v
    return fx(c)


def _vx_profile(cfg: dict, name: str) -> str:
    m = modelmod.get_model(cfg, name) if name else None
    if not m:
        return ""
    return f"https://www.visit-x.net/de/amateur/{(m.get('platform_name') or m['name']).lower()}/"


def _security_headers(cfg: dict, site_dir: Path) -> None:
    """Ergänzt Sicherheitsheader in _headers. Die CSP erlaubt nur eigene Skripte, das eine Inline-Skript mit der
    GA-ID (per Hash) und Google Analytics; JSON-LD ist ein Datenblock und von script-src nicht betroffen."""
    import base64
    hp = site_dir / "_headers"
    if not hp.exists():
        return
    ga = cfg["site"].get("ga_id")
    hashes = []
    if ga:
        inline = f'window.GA_ID="{ga}";'
        hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode() + "'")
    # Hosts laut Google-Leitfaden für GA4 mit CSP (inkl. Google Signals)
    gtm = "https://*.googletagmanager.com"
    gac = ("https://*.google-analytics.com https://*.analytics.google.com https://*.googletagmanager.com "
           "https://*.g.doubleclick.net https://*.google.com")
    csp = "; ".join([
        "default-src 'self'",
        "script-src 'self' " + " ".join(hashes) + (f" {gtm}" if ga else ""),
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: https:",
        "connect-src 'self'" + (f" {gac}" if ga else ""),
        "font-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
        "upgrade-insecure-requests",
    ])
    extra = ("  Strict-Transport-Security: max-age=31536000; includeSubDomains\n"
             f"  Content-Security-Policy: {csp}\n"
             "  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()\n")
    txt = hp.read_text(encoding="utf-8")
    if "Content-Security-Policy" not in txt:
        txt = txt.replace("/*\n", "/*\n" + extra, 1)
        hp.write_text(txt, encoding="utf-8")


def build_site(cfg: dict, store) -> dict:
    root: Path = cfg["_root"]
    site_dir: Path = cfg["_site"]
    base = cfg["site"]["base_url"].rstrip("/")

    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)
    static = root / "static"
    if static.exists():
        for item in static.iterdir():
            if item.is_file():
                shutil.copy2(item, site_dir / item.name)
    _security_headers(cfg, site_dir)

    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["md"] = md
    env.filters["plain"] = plain
    pages = store.pages()
    contents = {s: c for s, c in store.all_content().items() if s in pages}
    related = related_slugs(contents)
    labels = cfg["topic_labels"]
    import re as _re
    _pc = _re.search(r"[?&](pc=[^&]+)", cfg["affiliate"]["url_template"])
    def has_bonus(url: str) -> bool:
        """Bonus hängt am Aktionscode (pc=...) des Standardlinks: jeder Link mit demselben Code zeigt den Hinweis."""
        return bool(cfg["affiliate"].get("bonus")) and bool(_pc) and _pc.group(1) in url
    ctx = {"bonus": cfg["affiliate"].get("bonus") or {}, "site": cfg["site"], "provider": cfg["affiliate"]["provider_name"],
           "cta_label": cfg["affiliate"]["cta_label"], "year": date.today().year}
    topic_cta = cfg["affiliate"].get("topic_cta") or {}
    hubs = set()
    for sl, mt in pages.items():
        if sl in contents:
            hubs.add(mt["topic"])

    import hashlib
    ctx["asset_v"] = hashlib.md5(b"".join((root / "static" / f).read_bytes() for f in ("style.css", "agegate.js", "consent.js")
                                         if (root / "static" / f).exists())).hexdigest()[:10]
    ctx["nav"] = [{"topic": t, "label": labels.get(t, t)} for t in [*cfg["topics"]] if t in hubs]
    ctx["sections"] = SECTIONS
    ctx["payment_note"] = cfg["affiliate"].get("payment_note", "")
    _key = ["visit-x-kosten", "visit-x-erfahrungen", "visit-x-serioes", "visit-x-anmeldung", "visit-x-kostenlos-testen",
            "visit-x-zahlungsmethoden", "visit-x-coins", "visit-x-kuendigen"]
    _lab = {"visit-x-kosten": "Visit-X Kosten", "visit-x-erfahrungen": "Visit-X Erfahrungen", "visit-x-serioes": "Ist Visit-X seriös?",
            "visit-x-anmeldung": "Visit-X Anmeldung", "visit-x-kostenlos-testen": "Visit-X kostenlos testen",
            "visit-x-zahlungsmethoden": "Visit-X Zahlungsmethoden", "visit-x-coins": "Visit-X Coins & Tokens",
            "visit-x-kuendigen": "Visit-X kündigen"}
    ctx["key_guides"] = [{"slug": k, "label": _lab[k]} for k in _key if k in pages]

    def write(rel: str, html: str) -> None:
        target = site_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    top_models = []
    for m in modelmod.load_models(cfg):
        sl = next((k for k, v in pages.items() if v.get("kind") == modelmod.PAGE_KIND and v.get("model") == m["name"] and k in contents), None)
        links = modelmod.links_for(cfg, m["name"])
        if sl and links:
            top_models.append({"name": m["name"], "slug": sl, "img": f"/img/{sl}.svg", "url": links[0]["url"],
                               "bonus": has_bonus(links[0]["url"])})
    ctx["top_models"] = top_models
    collected = vidmod.collect(cfg, store)
    ctx["has_videos"] = bool(collected)
    ctx["videostation"] = cfg["affiliate"].get("videostation_url") or cfg["affiliate"]["url_template"]
    newest_videos = sorted((dict(v, model=d["model"]["name"]) for d in collected.values() for v in d["videos"]),
                           key=lambda v: int(v["id"]), reverse=True)
    page_tpl = env.get_template("page.html")
    thumbs: dict[str, str] = {}
    for slug, c in contents.items():
        meta = pages[slug]
        rel = [{"slug": r, "title": contents[r]["h1"]} for r in related.get(slug, [])]
        is_model = meta.get("kind") == modelmod.PAGE_KIND
        if is_model:
            c = _fix_case(c, meta.get("primary", ""))
            ctas = [{"label": l["label"], "url": l["url"]} for l in modelmod.links_for(cfg, meta.get("model"))]
        else:
            ctas = []
        if not ctas:
            ctas = [{"label": topic_cta.get(meta["topic"]) or ctx["cta_label"], "url": affiliate_url(cfg, meta)}]
        for x in ctas:
            x["bonus"] = has_bonus(x["url"])
        if is_model:
            name = meta.get("model", "")
            c_img = dict(c, image={"svg": imgmod.model_svg(name), "alt": f"{name}: Top-Model bei {ctx['provider']}"})
            image = imgmod.write_files(site_dir, slug, c_img, cfg["site"]["name"])
        else:
            image = imgmod.write_files(site_dir, slug, c, cfg["site"]["name"])
        others = [m for m in top_models if m["slug"] != slug]
        if others:
            start = zlib.crc32(slug.encode()) % len(others)
            others = (others[start:] + others[:start])[:4] if not is_model else others[:8]
        if image:
            thumbs[slug] = image["svg"]
        write(f"{slug}/index.html", page_tpl.render(
            **ctx, c=c, slug=slug, image=image, og_image=(base + image["png"]) if image and image["png"] else "", meta=meta, canonical=f"{base}/{slug}/",
            ctas=ctas, is_model=is_model, model_cards=others, vx_profile=_vx_profile(cfg, meta.get("model", "")) if is_model else "", vids=(collected.get(slug) or {}).get("videos", [])[:6], vids_total=len((collected.get(slug) or {}).get("videos", [])), model_name=meta.get("model", ""),
            related=rel, topic_label=labels.get(meta["topic"], ""), section=section_of(meta["topic"]),
            hub_url=(f"{base}/models/" if is_model else f"{base}/themen/{meta['topic']}/"),
            published=fmt_date(meta.get("modified_at") or meta["published_at"]), robots="index,follow",
        ))

    groups: dict[str, list[dict]] = {}
    for slug, meta in sorted(pages.items(), key=lambda x: x[1]["published_at"], reverse=True):
        if slug in contents:
            groups.setdefault(meta["topic"], []).append({"slug": slug, "title": contents[slug]["h1"], "img": thumbs.get(slug),
                "kind": section_of(meta["topic"])["key"], "badge": labels.get(meta["topic"], ""), "name": meta.get("model"),
                "text": (contents[slug].get("meta_description") or "")[:150], "date": fmt_date(meta.get("modified_at") or meta["published_at"])})
    order = [t for t in [*cfg["topics"], modelmod.TOPIC] if t in groups]
    ordered = [{"topic": t, "label": labels.get(t, t), "items": groups[t][:6], "count": len(groups[t]),
                "intro": (cfg.get("topic_intro") or {}).get(t, "")} for t in order]
    latest = sorted((i for g in groups.values() for i in g if i["kind"] != "models"), key=lambda i: i["date"][-4:] + i["date"][3:5] + i["date"][:2], reverse=True)[:6]
    intros = cfg.get("topic_intro") or {}
    hub_tpl = env.get_template("hub.html")
    hub_topics = [t for t in order if t != modelmod.TOPIC]
    for t in hub_topics:
        write(f"themen/{t}/index.html", hub_tpl.render(
            **ctx, topic=t, label=labels.get(t, t), intro=intros.get(t, ""), items=groups[t], section=section_of(t),
            canonical=f"{base}/themen/{t}/", robots="index,follow", title=f"{labels.get(t, t)} bei Visit-X: Ratgeber | {cfg['site']['name']}",
            description=((intros.get(t) or labels.get(t, t)).rstrip(".") + f". {len(groups[t])} Ratgeber-Artikel zu {labels.get(t, t)} im Überblick.")[:158], og_image=""))
    sec_tpl = env.get_template("section.html")
    sec_data = []
    for sec in SECTIONS:
        ts = [t for t in sec["topics"] if t in groups and t != modelmod.TOPIC]
        items = sorted((i for t in ts for i in groups[t]), key=lambda i: i["date"][-4:] + i["date"][3:5] + i["date"][:2], reverse=True)
        tiles = [{"topic": t, "label": labels.get(t, t), "intro": intros.get(t, ""), "count": len(groups[t])} for t in ts]
        count = len(top_models) if sec["key"] == "models" else len(items)
        sec_data.append(dict(sec, tiles=tiles, items=items, count=count))
        if count:
            write(f"{sec['path']}/index.html", sec_tpl.render(
                **ctx, sec=sec, tiles=tiles, items=items, canonical=f"{base}/{sec['path']}/", robots="index,follow",
                title=sec["title"], description=sec["intro"][:160], og_image=f"{base}/logo.png",
                list_ld=[{"@type": "ListItem", "position": n + 1, "url": f"{base}/{x['slug']}/"}
                         for n, x in enumerate(top_models if sec["key"] == "models" else items)]))
    write("_redirects", "/themen/profile/ /models/ 301\n/themen/profile /models/ 301\n")
    write("index.html", env.get_template("index.html").render(
        **ctx, sec_data=sec_data, latest=latest, new_videos=newest_videos[:8], videos_total=len(newest_videos), bonus_url=cfg["affiliate"]["url_template"], canonical=f"{base}/", robots="index,follow",
        top_models_ld=[{"@type": "ListItem", "position": i + 1, "name": m["name"], "url": f"{base}/{m['slug']}/"}
                       for i, m in enumerate(top_models)],
        title=cfg["site"].get("home_title") or cfg["site"]["name"], description=cfg["site"]["tagline"],
        og_image=f"{base}/logo.png"))

    for name, title in (("impressum", "Impressum"), ("datenschutz", "Datenschutzerklärung")):
        write(f"{name}/index.html", env.get_template(f"{name}.html").render(
            **ctx, canonical=f"{base}/{name}/", robots="noindex,follow", title=title,
            description=title, operator=cfg["site"].get("operator", {})))
    write("404.html", env.get_template("404.html").render(
        **ctx, canonical=f"{base}/", robots="noindex,follow", title="Seite nicht gefunden", description=""))

    video_urls = vidmod.render(cfg, store, env, ctx, write, site_dir, base, collected)
    urls = [(f"{base}/", max((p["published_at"] for p in pages.values()), default=date.today().isoformat()))]
    urls += [(f"{base}/{x['path']}/", date.today().isoformat()) for x in sec_data if x["count"]]
    urls += [(f"{base}/themen/{t}/", max(pages[i["slug"]]["published_at"] for i in groups[t])) for t in hub_topics]
    urls += [(f"{base}/{s}/", pages[s].get("modified_at") or pages[s]["published_at"]) for s in contents]
    urls += video_urls
    def _sm(items):
        out = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        out += [f"  <url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in items]
        return "\n".join(out + ["</urlset>"]) + "\n"
    model_urls = {f"{base}/{s}/" for s in contents if pages[s].get("topic") == modelmod.TOPIC} | {f"{base}/models/"}
    vid_set = {u for u, _ in video_urls}
    parts = {"sitemap-seiten.xml": [x for x in urls if x[0] not in model_urls and x[0] not in vid_set],
             "sitemap-models.xml": [x for x in urls if x[0] in model_urls],
             "sitemap-videos.xml": [x for x in urls if x[0] in vid_set]}
    idx = ['<?xml version="1.0" encoding="UTF-8"?>', '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for name, items in parts.items():
        if items:
            write(name, _sm(items))
            idx.append(f"  <sitemap><loc>{base}/{name}</loc><lastmod>{max(d for _, d in items)}</lastmod></sitemap>")
    write("sitemap.xml", "\n".join(idx + ["</sitemapindex>"]) + "\n")
    nowkey = (cfg.get("indexnow") or {}).get("key")
    if nowkey and (cfg.get("indexnow") or {}).get("enabled"):
        write(f"{nowkey}.txt", nowkey)
    write("robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")
    return {"pages": len(contents), "sitemap_urls": len(urls)}
