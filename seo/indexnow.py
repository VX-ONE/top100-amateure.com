"""IndexNow: meldet neue und geänderte URLs an Bing, Yandex und andere Suchmaschinen (Google nutzt IndexNow nicht).

Der Schlüssel liegt öffentlich als Datei auf der Seite (<key>.txt), das ist so vorgesehen. Fehler sind unkritisch.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import date


def todays_urls(pages: dict, today: str | None = None) -> list[str]:
    today = today or date.today().isoformat()
    return [p["url"] for p in pages.values()
            if p.get("published_at") == today or p.get("modified_at") == today]


def submit(cfg: dict, urls: list[str], wait: bool = True) -> str:
    c = cfg.get("indexnow") or {}
    if not c.get("enabled") or not c.get("key"):
        return "IndexNow aus."
    if not urls:
        return "IndexNow: keine neuen URLs."
    host = cfg["site"]["base_url"].replace("https://", "").replace("http://", "").rstrip("/")
    body = json.dumps({"host": host, "key": c["key"], "keyLocation": f"https://{host}/{c['key']}.txt",
                       "urlList": urls[:10000]}).encode()
    if wait:
        time.sleep(int(c.get("delay_seconds", 150)))  # warten, bis die Seite und die Schlüsseldatei online sind
    req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"IndexNow: {len(urls)} URL(s) gemeldet (Status {r.status})."
    except Exception as e:
        return f"IndexNow fehlgeschlagen: {type(e).__name__}: {e}"
