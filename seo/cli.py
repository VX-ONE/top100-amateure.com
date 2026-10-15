"""Kommandozeile: python -m seo.cli <befehl>"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path

from . import config as cfgmod
from . import generate, gsc, images, indexnow, keywords, models as modelmod, optimize, publish, quality, render
from .store import Store


def cmd_import(cfg, args) -> int:
    store = Store(cfg["_data"])
    stats = keywords.import_file(args.file, cfg, store, use_llm=not args.no_llm_screen)
    print("Import abgeschlossen:")
    for k in ("gelesen", "bekannt", "neu", "zu_bestehender_seite", "abgelehnt"):
        print(f"  {k}: {stats[k]}")
    if stats["gruende"]:
        print("  Ablehnungsgründe: " + ", ".join(f"{k} ({v})" for k, v in sorted(stats["gruende"].items(), key=lambda x: -x[1])))
    print("  Abgelehnte Keywords: data/rejected.csv" + (" (Sandbox)" if cfg["_sandbox"] else ""))
    return 0


def priority_bonus(c: dict, cfg: dict) -> int:
    """Seiten mit Kaufabsicht zuerst: Marke + Frage (kosten, kündigen, seriös ...) und Österreich/Schweiz."""
    b = (cfg["keywords"].get("priority") or {})
    k = c["primary"]
    bonus = 0
    if any(t in k for t in ("visit x", "visitx", "visit-x")):
        bonus += int(b.get("brand", 0))
    if c.get("topic") == "dach":
        bonus += int(b.get("dach", 0))
    bonus += int((b.get("topics") or {}).get(c.get("topic"), 0))
    return bonus


def harvest(cfg, store) -> dict | None:
    """Neue Keywords aus den Suchanfragen der Search Console in die Warteschlange stellen."""
    h = cfg["keywords"].get("harvest", {})
    if not h.get("enabled", False):
        return None
    try:
        svc = gsc.service(cfg)
        if svc is None:
            print("Keyword-Kreislauf übersprungen: keine Search-Console-Zugangsdaten.")
            return None
        found = gsc.queries(cfg, svc, days=h.get("days", 28), min_impressions=h.get("min_impressions", 1),
                            limit=h.get("max_queries", 500))
    except Exception as e:
        print(f"Keyword-Kreislauf fehlgeschlagen: {type(e).__name__}: {e}")
        return None
    stats = keywords.import_found(found, cfg, store)
    print(f"Keyword-Kreislauf: {stats['gelesen']} Suchanfragen, neu: {stats['neu']}, "
          f"zu bestehender Seite: {stats['zu_bestehender_seite']}, abgelehnt: {stats['abgelehnt']}.")
    return stats


def optimize_step(cfg, store, mock: bool) -> dict | None:
    """Rankings auswerten, Seiten mit Potenzial verbessern, Verschlechterungen zurückrollen."""
    svc = None
    if not mock:
        try:
            svc = gsc.service(cfg)
        except Exception as e:
            print(f"Optimierer: Search Console nicht erreichbar ({type(e).__name__}: {e}).")
    try:
        res = optimize.run(cfg, store, svc=svc, mock=mock)
    except Exception as e:
        print(f"Optimierer fehlgeschlagen: {type(e).__name__}: {e}")
        return None
    print(f"Optimierer: {len(res['applied'])} Änderung(en), {len(res['rejected'])} verworfen, "
          f"{len(res['effects'])} Wirkungsprüfung(en).")
    rep = Path(cfg["_reports"]) / f"{date.today().isoformat()}.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    with rep.open("a", encoding="utf-8") as f:
        f.write(optimize.report_section(res))
    return res


def cmd_optimize(cfg, args) -> int:
    store = Store(cfg["_data"])
    return 0 if optimize_step(cfg, store, args.mock) is not None else 1


def cmd_harvest(cfg, args) -> int:
    cfg["keywords"].setdefault("harvest", {})["enabled"] = True
    return 0 if harvest(cfg, Store(cfg["_data"])) is not None else 1


def run_models(cfg, store, pages, state, texts, titles, facts, mock, today) -> int:
    """Erzeugt neutrale Profilseiten für aktive Models (zusätzlich zum Tageslimit der Themenseiten)."""
    if state["throttle"]["active"] or modelmod.TOPIC in state["paused_topics"]:
        return 0
    lim = cfg["models"]
    todo = max(0, lim["per_day"] - sum(1 for p in pages.values()
                                       if p.get("kind") == modelmod.PAGE_KIND and p["published_at"] == today))
    created = 0
    for model in modelmod.load_models(cfg):
        if created >= todo:
            break
        if not model["active"] or model["slug"] in pages:
            continue
        keywords_ok = modelmod.valid_keywords(model, cfg)
        if not keywords_ok or not model["facts"] or not model["links"]:
            print(f"  [{model['slug']}] übersprungen: Keywords, facts und landing_links müssen in data/models.yaml stehen.")
            continue
        cluster = {"slug": model["slug"], "primary": keywords_ok[0], "keywords": keywords_ok, "topic": modelmod.TOPIC}
        issues: list[str] | None = None
        content = None
        for attempt in range(1, cfg["limits"]["max_attempts"] + 1):
            try:
                draft = generate.generate_model(model, cluster, cfg, facts, titles, issues, mock=mock)
            except Exception as e:
                issues = [f"Generierung fehlgeschlagen: {type(e).__name__}: {e}"]
                print(f"  [{model['slug']}] Versuch {attempt}: {issues[0]}")
                continue
            issues = quality.check(draft, cluster, cfg, "\n".join(model["facts"]) + "\n" + facts, texts,
                                   relaxed=mock, blocked=[], min_words=lim["min_words"],
                                   max_sim_limit=lim["max_similarity"], titles=titles)
            issues += modelmod.text_problems(quality.article_text(draft), model, cfg)
            if not issues:
                content = draft
                break
            print(f"  [{model['slug']}] Versuch {attempt} abgelehnt: {issues[0]}")
        if content is None:
            continue
        images.ensure(content, modelmod.TOPIC, model["slug"], cfg, mock=mock)
        store.save_content(model["slug"], content)
        pages[model["slug"]] = {
            "slug": model["slug"], "primary": cluster["primary"], "keywords": keywords_ok, "topic": modelmod.TOPIC,
            "kind": modelmod.PAGE_KIND, "model": model["name"],
            "title": content["title"], "published_at": today,
            "url": cfg["site"]["base_url"].rstrip("/") + f"/{model['slug']}/",
            "indexed": None, "coverage": None, "checked_at": None, "clicks": 0, "impressions": 0, "position": None,
        }
        texts[model["slug"]] = quality.article_text(content)
        titles.append(content["title"])
        created += 1
        print(f"  Profilseite veröffentlicht: /{model['slug']}/  ({cluster['primary']})")
    return created


def cmd_run(cfg, args) -> int:
    mock = args.mock
    if not mock:
        problems = cfgmod.live_problems(cfg)
        if problems:
            print("Der Live-Lauf startet erst, wenn diese Punkte erledigt sind:")
            for p in problems:
                print(f"  - {p}")
            return 2

    store = Store(cfg["_data"])
    removed = modelmod.apply_blocklist(cfg, store)
    if removed:
        print(f"Model-Seiten entfernt (inaktiv oder gesperrt): {', '.join(removed)}")
    state = store.state()
    pages = store.pages()
    today = date.today().isoformat()
    lim = cfg["limits"]

    budget = min(args.limit or state.get("daily_pages") or lim["daily_pages"], lim["max_daily_pages"])
    if state["throttle"]["active"]:
        budget = min(budget, cfg["monitor"]["throttle_to"])
        print(f"Drosselung aktiv: höchstens {budget} Seite(n) heute.")
    budget -= sum(1 for p in pages.values() if p["published_at"] == today and p.get("kind") != modelmod.PAGE_KIND)
    if budget <= 0:
        print("Das Tageslimit für Themenseiten ist bereits erreicht.")
        budget = 0

    if not mock:
        harvest(cfg, store)
    optimize_step(cfg, store, mock)
    pages = store.pages()  # der Optimierer hat Seiten geändert: neu laden, sonst überschreibt das Speichern unten seine Änderungen
    clusters = store.clusters()
    paused = set(state["paused_topics"])
    queue = sorted((c for c in clusters if c["status"] == "queued" and c["topic"] not in paused),
                   key=lambda c: (-(c["impressions"] + priority_bonus(c, cfg)), c["primary"]))
    if not queue:
        print("Die Keyword-Warteschlange ist leer (oder alle Themen sind pausiert). Importiere neue Keywords.")

    facts = generate.load_facts(cfg["_root"])
    contents = store.all_content()
    texts = {s: quality.article_text(c) for s, c in contents.items()}
    titles = [c["title"] for c in contents.values()]
    per_topic_cap = max(1, math.ceil(budget / 2))
    topic_count: dict[str, int] = {}
    published = tried = 0
    runlog: list[str] = []

    for cluster in queue:
        if published >= budget or tried >= budget * 3:
            break
        if topic_count.get(cluster["topic"], 0) >= per_topic_cap:
            continue
        tried += 1
        issues: list[str] | None = None
        content = None
        for attempt in range(1, lim["max_attempts"] + 1):
            try:
                draft = generate.generate(cluster, cfg, facts, titles, issues, mock=mock)
            except Exception as e:
                issues = [f"Generierung fehlgeschlagen: {type(e).__name__}: {e}"]
                print(f"  [{cluster['slug']}] Versuch {attempt}: {issues[0]}")
                runlog.append(f"{cluster['slug']} Versuch {attempt}: {issues[0]}")
                continue
            issues = quality.check(draft, cluster, cfg, facts, texts, relaxed=mock, titles=titles)
            if not issues:
                content = draft
                break
            print(f"  [{cluster['slug']}] Versuch {attempt} abgelehnt: {issues[0]}")
            runlog.append(f"{cluster['slug']} Versuch {attempt} ({quality.word_count(draft)} Wörter): " + " | ".join(issues))
        if content is None:
            cluster["last_issues"] = (issues or [])[:3]
            cluster["fail_count"] = cluster.get("fail_count", 0) + 1
            if cluster["fail_count"] >= 3:
                cluster["status"] = "failed"
                cluster["reason"] = "; ".join(issues or [])[:300]
            continue

        slug = cluster["slug"]
        images.ensure(content, cluster["topic"], slug, cfg, mock=mock)
        store.save_content(slug, content)
        pages[slug] = {
            "slug": slug, "primary": cluster["primary"], "keywords": cluster["keywords"], "topic": cluster["topic"],
            "title": content["title"], "published_at": today, "subid": render.next_subid(cfg, pages),
            "url": cfg["site"]["base_url"].rstrip("/") + f"/{slug}/",
            "indexed": None, "coverage": None, "checked_at": None, "clicks": 0, "impressions": 0, "position": None,
        }
        cluster["status"] = "published"
        texts[slug] = quality.article_text(content)
        titles.append(content["title"])
        topic_count[cluster["topic"]] = topic_count.get(cluster["topic"], 0) + 1
        published += 1
        print(f"  veröffentlicht: /{slug}/  ({cluster['primary']})")

    if runlog:
        lp = Path(cfg["_reports"]) / f"{today}-lauf.log"
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("\n".join(runlog) + "\n", encoding="utf-8")
    published += run_models(cfg, store, pages, state, texts, titles, facts, mock, today)

    store.save_clusters(clusters)
    store.save_pages(pages)
    info = render.build_site(cfg, store)
    print(f"Seite gebaut: {info['pages']} Seiten, {info['sitemap_urls']} Sitemap-URLs.")
    print(f"Heute neu: {published} (Themenseiten geplant: {budget}).")

    if mock or args.no_publish:
        print("Veröffentlichung übersprungen.")
        return 0
    print(publish.publish(cfg, published))
    print(indexnow.submit(cfg, indexnow.todays_urls(pages)))
    if not args.no_gsc and published:
        try:
            svc = gsc.service(cfg)
            if svc is None:
                print("Sitemap-Einreichung übersprungen: keine Search-Console-Zugangsdaten.")
            else:
                print("Sitemap eingereicht:", gsc.submit_sitemap(cfg, svc))
        except Exception as e:
            print(f"Sitemap-Einreichung fehlgeschlagen: {e}")
    return 0


def cmd_monitor(cfg, args) -> int:
    result = gsc.monitor(cfg, Store(cfg["_data"]))
    if not result["ok"]:
        print(result["message"])
        return 1
    h = result["health"]
    ratio = "keine Daten" if h["ratio"] is None else f"{h['ratio']:.0%}"
    print(f"Geprüft: {result['checked']} URLs. Indexierungsquote reifer Seiten: {ratio}.")
    print(f"Drosselung: {'AKTIV' if h['throttle'] else 'aus'}. Pausierte Themen: {', '.join(h['paused']) or 'keine'}.")
    print(f"Bericht: {result['report']}")
    return 0


def cmd_build(cfg, args) -> int:
    store = Store(cfg["_data"])
    removed = modelmod.apply_blocklist(cfg, store)
    if removed:
        print(f"Model-Seiten entfernt (inaktiv oder gesperrt): {', '.join(removed)}")
    info = render.build_site(cfg, store)
    print(f"Seite gebaut: {info['pages']} Seiten, {info['sitemap_urls']} Sitemap-URLs -> {cfg['_site']}")
    return 0


def cmd_images(cfg, args) -> int:
    """Bilder für bestehende Seiten nachziehen (oder mit --force neu erzeugen)."""
    store = Store(cfg["_data"])
    pages = store.pages()
    n = 0
    for slug, c in store.all_content().items():
        if slug in pages and images.ensure(c, pages[slug]["topic"], slug, cfg, mock=args.mock, force=args.force):
            store.save_content(slug, c)
            n += 1
    print(f"Bilder erzeugt: {n}")
    return 0


def cmd_status(cfg, args) -> int:
    store = Store(cfg["_data"])
    clusters, pages, state = store.clusters(), store.pages(), store.state()
    by: dict[str, int] = {}
    for c in clusters:
        by[c["status"]] = by.get(c["status"], 0) + 1
    print(f"Cluster: {len(clusters)} gesamt, " + ", ".join(f"{k}: {v}" for k, v in sorted(by.items())))
    print(f"Veröffentlichte Seiten: {len(pages)}, indexiert: {sum(1 for p in pages.values() if p.get('indexed'))}")
    print(f"Drosselung: {'aktiv' if state['throttle']['active'] else 'aus'}; pausierte Themen: {', '.join(state['paused_topics']) or 'keine'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="seo", description="Themenseiten-Pipeline")
    ap.add_argument("--config", help="Pfad zur config.yaml")
    ap.add_argument("--sandbox", action="store_true", help="Daten und Seite in .sandbox/ statt im Projekt ablegen")
    ap.add_argument("--mock", action="store_true", help="Testlauf ohne API und ohne Veröffentlichung (nutzt die Sandbox)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import-keywords", help="Keywords aus GSC-Export oder Textdatei einlesen")
    p.add_argument("file")
    p.add_argument("--no-llm-screen", action="store_true", help="KI-Vorprüfung überspringen")
    p = sub.add_parser("run", help="Neue Seiten erzeugen, bauen und veröffentlichen")
    p.add_argument("--limit", type=int, help="Seiten für diesen Lauf (max. max_daily_pages)")
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--no-gsc", action="store_true")
    sub.add_parser("optimize", help="Rankings auswerten und Seiten verbessern")
    sub.add_parser("harvest", help="Neue Keywords aus den Suchanfragen der Search Console übernehmen")
    sub.add_parser("monitor", help="Indexierung und Leistung prüfen, Bericht schreiben")
    p = sub.add_parser("images", help="Bilder für Seiten ohne Bild erzeugen")
    p.add_argument("--force", action="store_true", help="vorhandene Bilder neu erzeugen")
    sub.add_parser("build", help="Seite neu bauen")
    sub.add_parser("status", help="Stand anzeigen")

    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.config, sandbox=args.sandbox or args.mock)
    return {"import-keywords": cmd_import, "run": cmd_run, "monitor": cmd_monitor, "harvest": cmd_harvest, "optimize": cmd_optimize, "images": cmd_images,
            "build": cmd_build, "status": cmd_status}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
