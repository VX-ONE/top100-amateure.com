"""Google Search Console: Sitemap einreichen, Indexierungsstatus prüfen, Leistung auswerten, Drosselung.

Hinweis: Die Indexing API von Google ist offiziell nur für Stellenanzeigen und Livestreams gedacht
und wird hier bewusst nicht verwendet. Neue Seiten werden über Sitemap und interne Verlinkung gefunden.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/webmasters"]


def service(cfg: dict):
    """Gibt den Search-Console-Client zurück oder None, wenn keine Zugangsdaten vorhanden sind."""
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    path = cfg["gsc"].get("service_account_file")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if raw:
        creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    elif path and Path(path).exists():
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    else:
        return None
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def submit_sitemap(cfg: dict, svc) -> str:
    sitemap = cfg["site"]["base_url"].rstrip("/") + "/sitemap.xml"
    svc.sitemaps().submit(siteUrl=cfg["gsc"]["property"], feedpath=sitemap).execute()
    return sitemap


def inspect(cfg: dict, svc, url: str) -> dict:
    body = {"inspectionUrl": url, "siteUrl": cfg["gsc"]["property"], "languageCode": "de"}
    res = svc.urlInspection().index().inspect(body=body).execute()["inspectionResult"]["indexStatusResult"]
    return {"indexed": res.get("verdict") == "PASS", "coverage": res.get("coverageState", "")}


def performance(cfg: dict, svc, days: int = 28) -> dict[str, dict]:
    end = date.today() - timedelta(days=2)  # GSC-Daten kommen mit Verzögerung
    body = {"startDate": (end - timedelta(days=days)).isoformat(), "endDate": end.isoformat(),
            "dimensions": ["page"], "rowLimit": 5000, "type": "web"}
    rows = svc.searchanalytics().query(siteUrl=cfg["gsc"]["property"], body=body).execute().get("rows", [])
    return {r["keys"][0].rstrip("/") + "/": {"clicks": int(r["clicks"]), "impressions": int(r["impressions"]),
                                             "position": round(r["position"], 1)} for r in rows}


def evaluate_health(pages: dict[str, dict], cfg: dict, today: date | None = None) -> dict:
    """Reine Auswertung ohne API-Zugriff: Drosselung und pausierte Themen."""
    m = cfg["monitor"]
    today = today or date.today()
    cutoff = (today - timedelta(days=m["grace_days"])).isoformat()
    mature = [p for p in pages.values() if p["published_at"] <= cutoff and p.get("indexed") is not None]
    indexed = [p for p in mature if p["indexed"]]
    ratio = len(indexed) / len(mature) if mature else None
    throttle = bool(ratio is not None and len(mature) >= m["min_sample"] and ratio < m["min_indexed_ratio"])

    topics: dict[str, dict] = {}
    for p in mature:
        t = topics.setdefault(p["topic"], {"total": 0, "indexed": 0})
        t["total"] += 1
        t["indexed"] += 1 if p["indexed"] else 0
    paused = {}
    for topic, t in topics.items():
        t["ratio"] = t["indexed"] / t["total"]
        if t["total"] >= m["min_topic_sample"] and t["ratio"] < m["min_topic_ratio"]:
            paused[topic] = f"{t['indexed']}/{t['total']} indexiert"
    return {"mature": len(mature), "indexed": len(indexed), "ratio": ratio, "throttle": throttle,
            "topics": topics, "paused": paused}


def write_report(cfg: dict, pages: dict, health: dict, state: dict) -> Path:
    d = date.today().isoformat()
    total = len(pages)
    idx = sum(1 for p in pages.values() if p.get("indexed"))
    clicks = sum(p.get("clicks", 0) for p in pages.values())
    impr = sum(p.get("impressions", 0) for p in pages.values())
    lines = [f"# Bericht {d}", "",
             f"- Veröffentlichte Seiten: {total}",
             f"- Davon indexiert (laut URL-Inspection): {idx}",
             f"- Klicks / Impressionen (28 Tage): {clicks} / {impr}"]
    if health["ratio"] is None:
        lines.append(f"- Indexierungsquote reifer Seiten: noch keine Daten (Seiten müssen "
                     f"{cfg['monitor']['grace_days']} Tage alt sein)")
    else:
        lines.append(f"- Indexierungsquote reifer Seiten: {health['ratio']:.0%} ({health['indexed']}/{health['mature']})")
    lines.append(f"- Drosselung aktiv: {'ja' if state['throttle']['active'] else 'nein'}")
    if state["paused_topics"]:
        lines.append("- Pausierte Themen: " + ", ".join(f"{k} ({v})" for k, v in state["paused_topics"].items()))
    lines += ["", "## Top-Seiten nach Impressionen", "", "| Seite | Thema | Indexiert | Impr. | Klicks | Pos. |", "|---|---|---|---|---|---|"]
    for p in sorted(pages.values(), key=lambda x: -x.get("impressions", 0))[:20]:
        flag = {True: "ja", False: "nein", None: "offen"}[p.get("indexed")]
        lines.append(f"| {p['slug']} | {p['topic']} | {flag} | {p.get('impressions', 0)} | {p.get('clicks', 0)} | {p.get('position') or '-'} |")
    waiting = [p for p in pages.values() if p.get("indexed") is False]
    if waiting:
        lines += ["", "## Noch nicht indexiert", ""]
        lines += [f"- {p['slug']} ({p.get('coverage') or 'unbekannt'}), veröffentlicht {p['published_at']}" for p in waiting[:50]]
    out = Path(cfg["_reports"]) / f"{d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def monitor(cfg: dict, store) -> dict:
    svc = service(cfg)
    pages = store.pages()
    if svc is None:
        return {"ok": False, "message": "Keine Search-Console-Zugangsdaten gefunden "
                                        "(GSC_SERVICE_ACCOUNT_JSON oder gsc.service_account_file)."}
    # 1) Indexierungsstatus: zuerst Seiten ohne Prüfung, dann die am längsten nicht geprüften
    order = sorted(pages.values(), key=lambda p: p.get("checked_at") or "")
    now = datetime.now(timezone.utc)
    checked = 0
    for p in order:
        if checked >= cfg["gsc"]["inspect_per_run"]:
            break
        last = p.get("checked_at")
        if p.get("indexed") and last and (now - datetime.fromisoformat(last)).days < 14:
            continue
        if last and (now - datetime.fromisoformat(last)).days < 3:
            continue
        try:
            p.update(inspect(cfg, svc, p["url"]))
            p["checked_at"] = now.isoformat(timespec="seconds")
            checked += 1
        except Exception as e:
            print(f"Inspection fehlgeschlagen für {p['url']}: {e}")
            break  # meist Kontingent oder Berechtigung: nicht weiter hämmern
    # 2) Leistung
    try:
        perf = performance(cfg, svc)
        for p in pages.values():
            p.update(perf.get(p["url"], {"clicks": 0, "impressions": 0, "position": None}))
    except Exception as e:
        print(f"Leistungsabfrage fehlgeschlagen: {e}")
    # 3) Auswertung und Entscheidung
    health = evaluate_health(pages, cfg)
    state = store.state()
    state["throttle"] = {"active": health["throttle"], "updated": date.today().isoformat()}
    state["paused_topics"] = health["paused"]
    store.save_pages(pages)
    store.save_state(state)
    report = write_report(cfg, pages, health, state)
    return {"ok": True, "checked": checked, "health": health, "report": str(report)}
