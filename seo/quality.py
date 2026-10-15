"""Qualitäts-Gate: Seiten, die hier durchfallen, werden nicht veröffentlicht."""
from __future__ import annotations

import re

from .keywords import load_blocked, normalize

FAKE_EXPERIENCE = [
    "ich habe getestet", "wir haben getestet", "in meinem test", "in unserem test", "unser test ergab",
    "mein erfahrungsbericht", "unser erfahrungsbericht", "ich habe ausprobiert", "wir haben ausprobiert",
    "meine erfahrung mit", "unsere erfahrung mit",
]
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


def check(c: dict, cluster: dict, cfg: dict, facts: str, existing_texts: dict[str, str],
          relaxed: bool = False, blocked: list[str] | None = None,
          min_words: int | None = None, max_sim_limit: float | None = None) -> list[str]:
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

    primary = normalize(cluster["primary"])
    if primary not in normalize(c["title"] + " " + c["h1"]):
        issues.append("Das Hauptkeyword fehlt in Titel und Überschrift.")

    text = article_text(c)
    low = normalize(text)

    for term in (load_blocked(cfg) if blocked is None else blocked):
        if term in low:
            issues.append(f"Gesperrter Begriff im Text: {term}")
            break
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
