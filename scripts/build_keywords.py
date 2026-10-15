"""Erzeugt die Nischen-Keywordliste: Genre-Wörter aus der Amateur-Erotik-Welt, ohne Namen von Personen.

Die Zahlen sind KEINE Suchvolumen, sondern eine Rangfolge-Schätzung (relative Nachfrage der Kernbegriffe laut
Keyword-Recherche, multipliziert mit einem Gewicht für den Zusatz). Sie bestimmen nur die Reihenfolge der Warteschlange.
Echte Zahlen kommen später aus der Search Console. Aufruf: python scripts/build_keywords.py
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from seo import config as cfgmod  # noqa: E402
from seo.keywords import Vocab, classify, load_blocked  # noqa: E402

# Kernbegriffe mit relativer Nachfrage (aus der Keyword-Recherche, nur als Rangfolge genutzt)
CORES = {
    "porno": 135000, "porn": 49500, "pornos": 18100, "nackt": 6600, "nude": 6600, "anal": 5400, "xxx": 5400,
    "wichsanleitung": 1900, "joi": 1900, "videos": 1600, "footjob": 1600, "blowjob": 1300, "creampie": 1300,
    "füße": 880, "squirt": 880, "latex": 1000, "sex": 1000, "sexvideos": 800, "livecam": 700, "cam": 700,
    "livechat": 500, "sexcam": 700, "sexchat": 500, "erotik": 600, "dirty talk": 400, "striptease": 400,
    "masturbation": 400, "fetisch": 400, "bdsm": 400, "dessous": 300, "lingerie": 300, "high heels": 300,
    "stiefel": 300, "rollenspiel": 300, "pov": 300, "handjob": 400, "clips": 500, "amateurclips": 400,
}
# Zusätze mit Gewicht
MODS = {
    "deutsch": 1.0, "deutsche": 0.7, "amateur": 0.9, "amateure": 0.6, "deutsche amateure": 0.8, "deutsche amateur": 0.4,
    "reife": 0.3, "milf": 0.3, "hausfrau": 0.2, "paar": 0.2, "pärchen": 0.15, "swinger": 0.12, "mollig": 0.1,
    "brünett": 0.05, "blond": 0.05, "rothaarig": 0.05, "tattoo": 0.05, "reife amateure": 0.2, "deutsches paar": 0.15,
}
SUFFIXES = {"": 1.0, "kostenlos": 0.5, "gratis": 0.3, "live": 0.3, "videos": 0.4, "cam": 0.3}
SUFFIX_CORES = {"porno", "porn", "nackt", "anal", "creampie", "blowjob", "sex", "erotik", "cam", "livecam"}


def main() -> None:
    cfg = cfgmod.load(None)
    vocab, blocked = Vocab(cfg), load_blocked(cfg)
    rows: dict[str, int] = {}
    for mod, mw in MODS.items():
        for core, cw in CORES.items():
            for suf, sw in SUFFIXES.items():
                if suf and (core not in SUFFIX_CORES or suf in core):
                    continue
                for kw in (f"{mod} {core}", f"{core} {mod}") if not suf else (f"{mod} {core} {suf}",):
                    ok, _, _ = classify(kw, cfg, vocab, blocked)
                    if ok:
                        rows[kw] = max(rows.get(kw, 0), int(cw * mw * sw / 10) or 1)
    out = ROOT / "examples" / "keywords_nischen.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["keyword", "impressions"])
        for kw, n in sorted(rows.items(), key=lambda x: (-x[1], x[0])):
            w.writerow([kw, n])
    print(f"{len(rows)} Keywords -> {out}")


if __name__ == "__main__":
    main()
