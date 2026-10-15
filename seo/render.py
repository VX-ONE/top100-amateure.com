"""Statische Seite aus den gespeicherten Inhalten bauen (HTML, Sitemap, robots.txt)."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import images as imgmod
from . import models as modelmod
from .quality import article_text


def md(text: str) -> str:
    return markdown.markdown(text or "", extensions=["sane_lists"])


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

    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["md"] = md
    env.filters["plain"] = plain
    pages = store.pages()
    contents = {s: c for s, c in store.all_content().items() if s in pages}
    related = related_slugs(contents)
    labels = cfg["topic_labels"]
    ctx = {"site": cfg["site"], "provider": cfg["affiliate"]["provider_name"],
           "cta_label": cfg["affiliate"]["cta_label"], "year": date.today().year}
    topic_cta = cfg["affiliate"].get("topic_cta") or {}
    hubs = set()
    for sl, mt in pages.items():
        if sl in contents:
            hubs.add(mt["topic"])

    def write(rel: str, html: str) -> None:
        target = site_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    page_tpl = env.get_template("page.html")
    for slug, c in contents.items():
        meta = pages[slug]
        rel = [{"slug": r, "title": contents[r]["h1"]} for r in related.get(slug, [])]
        is_model = meta.get("kind") == modelmod.PAGE_KIND
        if is_model:
            ctas = [{"label": l["label"], "url": l["url"]} for l in modelmod.links_for(cfg, meta.get("model"))]
        else:
            ctas = []
        if not ctas:
            ctas = [{"label": topic_cta.get(meta["topic"]) or ctx["cta_label"], "url": affiliate_url(cfg, meta)}]
        image = imgmod.write_files(site_dir, slug, c, cfg["site"]["name"])
        write(f"{slug}/index.html", page_tpl.render(
            **ctx, c=c, slug=slug, image=image, og_image=(base + image["png"]) if image and image["png"] else "", meta=meta, canonical=f"{base}/{slug}/",
            ctas=ctas, is_model=is_model, model_name=meta.get("model", ""),
            related=rel, topic_label=labels.get(meta["topic"], ""), hub_url=f"{base}/themen/{meta['topic']}/",
            published=fmt_date(meta.get("modified_at") or meta["published_at"]), robots="index,follow",
        ))

    groups: dict[str, list[dict]] = {}
    for slug, meta in sorted(pages.items(), key=lambda x: x[1]["published_at"], reverse=True):
        if slug in contents:
            groups.setdefault(meta["topic"], []).append({"slug": slug, "title": contents[slug]["h1"]})
    order = [t for t in [*cfg["topics"], modelmod.TOPIC] if t in groups]
    ordered = [{"topic": t, "label": labels.get(t, t), "items": groups[t][:8], "count": len(groups[t])} for t in order]
    intros = cfg.get("topic_intro") or {}
    hub_tpl = env.get_template("hub.html")
    for t in order:
        write(f"themen/{t}/index.html", hub_tpl.render(
            **ctx, topic=t, label=labels.get(t, t), intro=intros.get(t, ""), items=groups[t],
            canonical=f"{base}/themen/{t}/", robots="index,follow", title=f"{labels.get(t, t)}: alle Ratgeber",
            description=(intros.get(t) or labels.get(t, t))[:160], og_image=""))
    write("index.html", env.get_template("index.html").render(
        **ctx, groups=ordered, canonical=f"{base}/", robots="index,follow",
        title=cfg["site"]["name"], description=cfg["site"]["tagline"]))

    for name, title in (("impressum", "Impressum"), ("datenschutz", "Datenschutzerklärung")):
        write(f"{name}/index.html", env.get_template(f"{name}.html").render(
            **ctx, canonical=f"{base}/{name}/", robots="noindex,follow", title=title,
            description=title, operator=cfg["site"].get("operator", {})))
    write("404.html", env.get_template("404.html").render(
        **ctx, canonical=f"{base}/", robots="noindex,follow", title="Seite nicht gefunden", description=""))

    urls = [(f"{base}/", max((p["published_at"] for p in pages.values()), default=date.today().isoformat()))]
    urls += [(f"{base}/themen/{t}/", max(pages[i["slug"]]["published_at"] for i in groups[t])) for t in order]
    urls += [(f"{base}/{s}/", pages[s].get("modified_at") or pages[s]["published_at"]) for s in contents]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sitemap += [f"  <url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in urls]
    sitemap.append("</urlset>")
    write("sitemap.xml", "\n".join(sitemap) + "\n")
    nowkey = (cfg.get("indexnow") or {}).get("key")
    if nowkey and (cfg.get("indexnow") or {}).get("enabled"):
        write(f"{nowkey}.txt", nowkey)
    write("robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")
    return {"pages": len(contents), "sitemap_urls": len(urls)}
