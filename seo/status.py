"""Täglicher Statusbericht: Search Console (Leistung, Suchanfragen, Indexierung) und Pipeline-Ergebnisse.

Schreibt reports/<datum>-status.md. Der Workflow "Statusbericht" stellt den Text zusätzlich als
GitHub-Issue ein, damit eine Benachrichtigung (E-Mail / GitHub-App) ankommt.
Alle Zahlen kommen aus der Search Console oder aus den Daten der Pipeline – nichts wird geschätzt.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import gsc
from . import videos as vidmod

LAG = 0  # dataState "all" liefert auch frische Daten; das letzte Datum mit Daten wird angezeigt


def _query(cfg, svc, start: date, end: date, dims: list[str], limit: int = 1000) -> list[dict]:
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dims,
            "rowLimit": limit, "type": "web", "dataState": "all"}  # inkl. noch nicht finaler (frischer) Daten
    return svc.searchanalytics().query(siteUrl=cfg["gsc"]["property"], body=body).execute().get("rows", [])


def _tot(rows: list[dict]) -> dict:
    c = sum(r["clicks"] for r in rows)
    i = sum(r["impressions"] for r in rows)
    pos = (sum(r["position"] * r["impressions"] for r in rows) / i) if i else None
    return {"clicks": int(c), "impr": int(i), "ctr": (c / i if i else 0.0), "pos": pos}


def _kind(path: str, model_slugs: set[str]) -> str:
    if re.match(r"^/[^/]+/videos/\d", path):
        return "Videoseiten"
    if path.endswith("/videos/"):
        return "Videoübersichten"
    seg = path.strip("/").split("/")[0] if path.strip("/") else ""
    if seg in model_slugs:
        return "Model-Seiten"
    if path == "/":
        return "Startseite"
    return "Ratgeber & Bereiche"


def _fmt_pos(p):
    return "–" if p is None else f"{p:.1f}".replace(".", ",")


def _fmt_pct(x):
    return f"{x * 100:.1f} %".replace(".", ",")


def _delta(a, b, kind="num"):
    if b in (None, 0) or a is None:
        return ""
    if kind == "pos":  # kleiner ist besser
        d = a - b
        return f" ({'+' if d > 0 else ''}{d:.1f})".replace(".", ",")
    d = (a - b) / b * 100
    return f" ({'+' if d >= 0 else ''}{d:.0f} %)"


def inspect_videos(cfg, svc, sample: int = 60) -> dict:
    """Prüft täglich eine Stichprobe Videoseiten per URL-Inspection (noch nie geprüfte zuerst)."""
    base = cfg["site"]["base_url"].rstrip("/")
    store_all = []
    for f in sorted(vidmod.vdir(cfg).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for v in d.get("videos", []):
            if v.get("status") == "published":
                store_all.append((f, d, v))
    todo = sorted(store_all, key=lambda x: (x[2].get("indexed") is True, x[2].get("checked_at") or ""))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed, checked = set(), 0
    for f, d, v in todo:
        if checked >= sample:
            break
        if v.get("indexed") and (v.get("checked_at") or "") > (date.today() - timedelta(days=14)).isoformat():
            continue
        url = f"{base}/{f.stem}/videos/{vidmod.page_slug(v)}/"
        try:
            r = gsc.inspect(cfg, svc, url)
        except Exception as e:
            print(f"Video-Inspection abgebrochen: {e}")
            break
        v["indexed"], v["coverage"], v["checked_at"] = r["indexed"], r["coverage"], now
        changed.add(f)
        checked += 1
    files = {f: d for f, d, _ in store_all}
    for f in changed:
        f.write_text(json.dumps(files[f], ensure_ascii=False, indent=1), encoding="utf-8")
    pub = [v for _, _, v in store_all]
    known = [v for v in pub if "indexed" in v]
    return {"published": len(pub), "checked_ever": len(known), "indexed": sum(1 for v in known if v["indexed"]),
            "checked_today": checked}


def build(cfg: dict, store) -> tuple[str, Path]:
    today = date.today()
    svc = gsc.service(cfg)
    pages = store.pages()
    model_slugs = {s for s, p in pages.items() if p.get("topic") == "profile"}
    lines = [f"# Statusbericht top100amateure.com – {today.strftime('%d.%m.%Y')}", ""]

    # ---------- Pipeline (aus den eigenen Daten, immer verfügbar)
    rep = Path(cfg["_reports"])
    vlog = rep / f"{today.isoformat()}-videos.log"
    vtext = vlog.read_text(encoding="utf-8") if vlog.exists() else ""
    m_new = re.search(r"Einlesen: \{[^}]*'neu': (\d+)", vtext)
    m_txt = re.search(r"Texte: \{'erzeugt': (\d+)", vtext)
    m_fail = re.search(r"Einlesen: \{[^}]*'fehler': (\d+)", vtext)
    new_pages = [s for s, p in pages.items() if p.get("published_at") == today.isoformat()]
    refreshed = [s for s, p in pages.items() if p.get("modified_at") == today.isoformat()]
    vids_total = sum(1 for f in vidmod.vdir(cfg).glob("*.json")
                     for v in json.loads(f.read_text(encoding="utf-8")).get("videos", []) if v.get("status") == "published")

    if svc is None:
        lines += ["Keine Search-Console-Zugangsdaten – nur Pipeline-Daten verfügbar.", ""]
        perf_ok = False
    else:
        perf_ok = True
        try:   # letzter Tag, für den Google schon Daten liefert (inkl. frischer Daten)
            recent = _query(cfg, svc, today - timedelta(days=10), today, ["date"])
            end = max((date.fromisoformat(r["keys"][0]) for r in recent), default=today - timedelta(days=3))
        except Exception:
            end = today - timedelta(days=3)
        lag_days = (today - end).days
        w1s, w0s, w0e = end - timedelta(days=6), end - timedelta(days=13), end - timedelta(days=7)
        try:
            cur = _tot(_query(cfg, svc, w1s, end, ["date"]))
            prev = _tot(_query(cfg, svc, w0s, w0e, ["date"]))
            m28 = _tot(_query(cfg, svc, end - timedelta(days=27), end, ["date"]))
            daily = sorted(_query(cfg, svc, end - timedelta(days=13), end, ["date"]), key=lambda r: r["keys"][0])
            q7 = sorted(_query(cfg, svc, w1s, end, ["query"], 500), key=lambda r: (-r["clicks"], -r["impressions"]))
            p7 = sorted(_query(cfg, svc, w1s, end, ["page"], 1000), key=lambda r: (-r["clicks"], -r["impressions"]))
            qp28 = _query(cfg, svc, end - timedelta(days=27), end, ["query", "page"], 2000)
        except Exception as e:
            perf_ok = False
            lines += [f"Search-Console-Abfrage fehlgeschlagen: {e}", ""]

    # ---------- Kurzfazit
    fazit = []
    if perf_ok:
        if lag_days > 3:
            fazit.append(f"Hinweis: Google hat seit {end.strftime('%d.%m.')} keine neuen Daten gemeldet ({lag_days} Tage Verzögerung) – "
                         "die Zahlen ändern sich erst, wenn Google nachliefert.")
        fazit.append(f"Letzte 7 Tage ({w1s.strftime('%d.%m.')}–{end.strftime('%d.%m.')}): **{cur['clicks']} Klicks** "
                     f"und **{cur['impr']} Impressionen**{_delta(cur['impr'], prev['impr'])} bei Ø-Position {_fmt_pos(cur['pos'])}.")
    fazit.append(f"Heute neu: {len(new_pages)} Seite(n), {len(refreshed)} überarbeitet, "
                 f"{m_txt.group(1) if m_txt else 0} neue Videoseite(n) (insgesamt {vids_total}).")
    if m_fail and int(m_fail.group(1)) > 0:
        fazit.append(f"Achtung: Video-Einlesen hatte {m_fail.group(1)} Fehler.")
    if "Einlesen" in vtext and m_new is None:
        fazit.append("Achtung: Video-Einlesen lieferte kein Ergebnis.")
    if not vtext:
        fazit.append("Hinweis: Heute lief noch kein Video-Operator (oder ohne Protokoll).")
    lines += ["## Kurzfazit", "", *[f"- {x}" for x in fazit], ""]

    if perf_ok:
        lines += ["## Leistung in der Google-Suche", "",
                  f"Daten bis einschließlich {end.strftime('%d.%m.%Y')} – das ist der letzte Tag, den Google bisher gemeldet hat "
                  f"({lag_days} Tage Verzögerung; jüngere Tage können noch unvollständig sein).", "",
                  "| Zeitraum | Klicks | Impressionen | CTR | Ø-Position |", "|---|---|---|---|---|",
                  f"| Letzte 7 Tage | {cur['clicks']}{_delta(cur['clicks'], prev['clicks'])} | {cur['impr']}{_delta(cur['impr'], prev['impr'])} "
                  f"| {_fmt_pct(cur['ctr'])} | {_fmt_pos(cur['pos'])}{_delta(cur['pos'], prev['pos'], 'pos')} |",
                  f"| Vorwoche | {prev['clicks']} | {prev['impr']} | {_fmt_pct(prev['ctr'])} | {_fmt_pos(prev['pos'])} |",
                  f"| Letzte 28 Tage | {m28['clicks']} | {m28['impr']} | {_fmt_pct(m28['ctr'])} | {_fmt_pos(m28['pos'])} |", ""]
        if daily:
            lines += ["### Verlauf (14 Tage)", "", "| Tag | Klicks | Impressionen | Ø-Position |", "|---|---|---|---|"]
            lines += [f"| {['Mo','Di','Mi','Do','Fr','Sa','So'][datetime.fromisoformat(r['keys'][0]).weekday()] + datetime.fromisoformat(r['keys'][0]).strftime(' %d.%m.')} | {int(r['clicks'])} | {int(r['impressions'])} "
                      f"| {_fmt_pos(r['position'])} |" for r in daily[::-1]]
            lines.append("")
        # nach Seitentyp
        by = {}
        for r in p7:
            path = re.sub(r"^https?://[^/]+", "", r["keys"][0]) or "/"
            k = _kind(path, model_slugs)
            b = by.setdefault(k, [])
            b.append(r)
        if by:
            lines += ["### Nach Seitentyp (7 Tage)", "", "| Typ | Seiten mit Impressionen | Klicks | Impressionen | Ø-Position |",
                      "|---|---|---|---|---|"]
            for k, rows in sorted(by.items(), key=lambda x: -_tot(x[1])["impr"]):
                t = _tot(rows)
                lines.append(f"| {k} | {len(rows)} | {t['clicks']} | {t['impr']} | {_fmt_pos(t['pos'])} |")
            lines.append("")
        if q7:
            lines += ["### Top-Suchanfragen (7 Tage)", "", "| Suchanfrage | Klicks | Impressionen | Ø-Position |", "|---|---|---|---|"]
            lines += [f"| {r['keys'][0]} | {int(r['clicks'])} | {int(r['impressions'])} | {_fmt_pos(r['position'])} |" for r in q7[:15]]
            lines.append("")
        if p7:
            lines += ["### Top-Seiten (7 Tage)", "", "| Seite | Klicks | Impressionen | Ø-Position |", "|---|---|---|---|"]
            lines += [f"| {re.sub(r'^https?://[^/]+', '', r['keys'][0])} | {int(r['clicks'])} | {int(r['impressions'])} "
                      f"| {_fmt_pos(r['position'])} |" for r in p7[:15]]
            lines.append("")
        chances = sorted([r for r in qp28 if 4 <= r["position"] <= 20 and r["impressions"] >= 3],
                         key=lambda r: -r["impressions"])[:12]
        if chances:
            lines += ["### Chancen: Position 4–20 (28 Tage)", "",
                      "Suchanfragen knapp unter den Top 3 – hier bringt Nachschärfen am meisten.", "",
                      "| Suchanfrage | Seite | Impressionen | Klicks | Ø-Position |", "|---|---|---|---|---|"]
            lines += [f"| {r['keys'][0]} | {re.sub(r'^https?://[^/]+', '', r['keys'][1])} | {int(r['impressions'])} | {int(r['clicks'])} "
                      f"| {_fmt_pos(r['position'])} |" for r in chances]
            lines.append("")
        lowctr = sorted([r for r in p7 if r["impressions"] >= 20 and r["clicks"] / r["impressions"] < 0.02],
                        key=lambda r: -r["impressions"])[:8]
        if lowctr:
            lines += ["### Viele Impressionen, kaum Klicks (7 Tage)", "", "| Seite | Impressionen | CTR | Ø-Position |", "|---|---|---|---|"]
            lines += [f"| {re.sub(r'^https?://[^/]+', '', r['keys'][0])} | {int(r['impressions'])} | {_fmt_pct(r['clicks'] / r['impressions'])} "
                      f"| {_fmt_pos(r['position'])} |" for r in lowctr]
            lines.append("")

    # ---------- Indexierung
    idx_rows = []
    groups = {"Model-Seiten": [p for s, p in pages.items() if s in model_slugs],
              "Ratgeber & Themen": [p for s, p in pages.items() if s not in model_slugs]}
    for k, ps in groups.items():
        chk = [p for p in ps if "indexed" in p]
        idx_rows.append(f"| {k} | {len(ps)} | {len(chk)} | {sum(1 for p in chk if p.get('indexed'))} |")
    vstat = None
    if svc is not None:
        try:
            vstat = inspect_videos(cfg, svc, sample=int((cfg.get("status") or {}).get("video_inspect_per_day", 60)))
            idx_rows.append(f"| Videoseiten | {vstat['published']} | {vstat['checked_ever']} | {vstat['indexed']} |")
        except Exception as e:
            print(f"Video-Indexprüfung fehlgeschlagen: {e}")
    lines += ["## Indexierung (URL-Prüfung der Search Console)", "",
              "| Seitentyp | Veröffentlicht | Davon geprüft | Davon indexiert |", "|---|---|---|---|", *idx_rows, ""]
    if vstat:
        lines += [f"Videoseiten werden täglich stichprobenartig geprüft (heute {vstat['checked_today']}).", ""]
    not_idx = [p for p in pages.values() if p.get("indexed") is False][:10]
    if not_idx:
        lines += ["Noch nicht indexiert (Auswahl):", ""]
        lines += [f"- {p['url'].replace(cfg['site']['base_url'].rstrip('/'), '')} – {p.get('coverage') or 'unbekannt'}" for p in not_idx]
        lines.append("")

    # ---------- Pipeline heute
    lines += ["## Pipeline heute", "",
              f"- Neue Themen-/Model-Seiten: {len(new_pages)}" + (f" ({', '.join(new_pages[:8])})" if new_pages else ""),
              f"- Überarbeitete Seiten: {len(refreshed)}" + (f" ({', '.join(refreshed[:10])})" if refreshed else ""),
              f"- Video-Operator: {m_new.group(1) if m_new else '–'} neue Videos erkannt, "
              f"{m_txt.group(1) if m_txt else '–'} Videoseiten erzeugt; insgesamt {vids_total} Videoseiten live",
              "", "_Traffic-Quelle: nur Google Search Console. Klicks auf Visit-X und Umsätze siehst du in VX Cash "
              "(Kampagne ws=2610001, Themenseiten mit eigener ws-Nummer)._", ""]
    text = "\n".join(lines)
    path = rep / f"{today.isoformat()}-status.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text, path
