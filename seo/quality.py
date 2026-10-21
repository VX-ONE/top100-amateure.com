"""Qualitäts-Gate: Seiten, die hier durchfallen, werden nicht veröffentlicht."""
from __future__ import annotations

import json
import re

from .keywords import blocked_hit, load_blocked, normalize

FAKE_EXPERIENCE = [
    "ich habe getestet", "wir haben getestet", "in meinem test", "in unserem test", "unser test ergab",
    "mein erfahrungsbericht", "unser erfahrungsbericht", "ich habe ausprobiert", "wir haben ausprobiert",
    "meine erfahrung mit", "unsere erfahrung mit",
]
# Artikel bleiben nicht-explizit: Vulgärsprache und Schilderungen werden abgelehnt (ganze Wörter).
EXPLICIT = re.compile(r"(?<![\wäöüß])(fick\w*|gefickt\w*|schwanz\w*|muschi\w*|fotze\w*|titten\w*|sperma\w*|"
                      r"abspritz\w*|wichs(?:en|t|e)|blasen|lecken|pimmel\w*|arsch\w*|geil\w*|orgasmus\w*)(?![\wäöüß])", re.I)
_PRICE = re.compile(r"\d+(?:[.,]\d+)?\s?(?:€|eur\b|euro\b)", re.I)


def article_text(c: dict) -> str:
    parts = [c.get("title", ""), c.get("intro", "")]
    parts += c.get("key_takeaways", [])
    for s in c.get("sections", []):
        parts += [s["h2"], s["body"]]
    for f in c.get("faq", []):
        parts += [f["q"], f["a"]]
    parts.append(c.get("cta_text", ""))
    return "\n".join(parts)


def word_count(c: dict) -> int:
    return len(re.findall(r"\w+", article_text(c)))


def max_similarity(text: str, existing: dict[str, str]) -> tuple[float, str | None]:
    if not existing:
        return 0.0, None
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    slugs = list(existing)
    docs = [existing[s] for s in slugs] + [text]
    matrix = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True).fit_transform(docs)
    sims = cosine_similarity(matrix[-1], matrix[:-1])[0]
    i = int(sims.argmax())
    return float(sims[i]), slugs[i]


def keyword_in(keyword: str, text: str) -> bool:
    """Alle Wörter des Keywords kommen vor, Reihenfolge und Beugung egal ("deutsch porno" passt zu "Deutsche Pornos")."""
    if normalize(keyword).replace(" ", "").replace("-", "") in normalize(text).replace(" ", "").replace("-", ""):
        return True
    words = re.split(r"[\s\-]+", normalize(text))
    for t in normalize(keyword).replace("-", " ").split():
        if len(t) < 3:
            continue
        stem = t[:max(4, len(t) - 2)]
        if not any(w.startswith(stem) for w in words):
            return False
    return True


def check(c: dict, cluster: dict, cfg: dict, facts: str, existing_texts: dict[str, str],
          relaxed: bool = False, blocked: list[str] | None = None,
          min_words: int | None = None, max_sim_limit: float | None = None,
          titles: list[str] | None = None) -> list[str]:
    """Gibt eine Liste von Problemen zurück. Leer = Seite darf veröffentlicht werden.

    blocked/min_words/max_sim_limit überschreiben die Standardwerte (genutzt für Model-Profilseiten)."""
    lim = cfg["limits"]
    issues: list[str] = []

    for key in ("title", "meta_description", "h1", "intro", "cta_text"):
        if not str(c.get(key, "")).strip():
            issues.append(f"Feld fehlt: {key}")
    if issues:
        return issues

    wc = word_count(c)
    min_words = 150 if relaxed else (min_words or lim["min_words"])
    if wc < min_words:
        issues.append(f"Zu kurz: {wc} Wörter, gefordert sind mindestens {min_words}.")
    if len(c.get("sections", [])) < lim["min_sections"]:
        issues.append(f"Zu wenige Abschnitte (mindestens {lim['min_sections']}).")
    if len(c.get("faq", [])) < lim["min_faq"]:
        issues.append(f"Zu wenige FAQ-Einträge (mindestens {lim['min_faq']}).")
    if not 20 <= len(c["title"]) <= 65:
        issues.append(f"Titel hat {len(c['title'])} Zeichen, erlaubt sind 20 bis 65.")
    if not 80 <= len(c["meta_description"]) <= 165:
        issues.append(f"Meta-Beschreibung hat {len(c['meta_description'])} Zeichen, erlaubt sind 80 bis 165.")

    if titles and normalize(c["title"]) in {normalize(t) for t in titles}:
        issues.append("Der Titel existiert schon bei einer anderen Seite: eigenen, klar anderen Titel wählen.")
    if not keyword_in(cluster["primary"], c["title"] + " " + c["h1"]):
        issues.append("Das Hauptkeyword fehlt in Titel und Überschrift.")
    core = [w for w in normalize(cluster["primary"]).replace("-", " ").split() if len(w) >= 3]
    h2_hits = sum(1 for s in c.get("sections", [])
                  if any(re.search(rf"\b{re.escape(w[:max(4, len(w) - 2)])}", normalize(s.get("h2", ""))) for w in core))
    if core and h2_hits < 2:
        issues.append("Zu wenige H2-Überschriften mit dem Hauptkeyword oder einer Variante (mindestens 2).")

    text = article_text(c)
    low = normalize(text)

    if blocked is None:
        allowed = {normalize(t) for t in cfg.get("text_allowed_terms", [])}
        terms = [t for t in load_blocked(cfg) if t not in allowed]
    else:
        terms = blocked
    hit = blocked_hit(low, terms)
    if hit:
        issues.append(f"Gesperrter Begriff im Text: {hit}")
    m = EXPLICIT.search(text)
    if m:
        issues.append(f"Zu explizit ('{m.group(0)}'): sachlich und ohne Vulgärsprache formulieren.")
    raw = json.dumps(c, ensure_ascii=False)
    stuffed = len(re.findall(r"(?i)\bnach \*\*[^*]{2,60}\*\* (?:sucht|suchst|googelt)|\*\*[a-zäöüß0-9 \-]{3,60}\*\* (?:sucht|suchst)", raw))
    if stuffed:
        issues.append("Keyword-Stuffing: keine Sätze wie 'Wer nach **…** sucht' und keine fett gesetzten Suchbegriffe; "
                      "Suchbegriffe nur natürlich und in normaler Schreibweise einbauen.")
    for phrase in FAKE_EXPERIENCE:
        if phrase in low:
            issues.append(f"Erfundener Erfahrungsbericht ('{phrase}'): bitte neutral als Ratgeber formulieren.")
            break

    facts_norm = facts.lower().replace(" ", "")
    for m in _PRICE.findall(text):
        if m.lower().replace(" ", "") not in facts_norm:
            issues.append(f"Preisangabe '{m}' steht nicht in facts.md: entfernen oder allgemein formulieren.")
            break

    max_sim = 0.9 if relaxed else (max_sim_limit or lim["max_similarity"])
    sim, other = max_similarity(text, existing_texts)
    if sim >= max_sim:
        issues.append(f"Zu ähnlich zu '{other}' (Ähnlichkeit {sim:.2f}, erlaubt unter {max_sim}): "
                      "anderen Schwerpunkt und Aufbau wählen.")
    return issues
