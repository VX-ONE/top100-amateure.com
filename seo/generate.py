"""Artikelgenerierung mit Claude (oder Mock-Generator für Tests)."""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

SYSTEM = """Du bist Redakteur eines unabhängigen Ratgebers rund um Live-Cam-Plattformen und Erotik-Angebote für Erwachsene (Amateur-Cams, Clips, Videos). Du schreibst sachliche, hilfreiche, nicht-explizite Artikel auf Deutsch für Leser ab 18 Jahren. Bei Amateur-Themen gehst du auf Einvernehmlichkeit, Legalität, Urheberrecht und den Schutz der Privatsphäre ein und weist darauf hin, dass nur Angebote genutzt werden sollten, bei denen die Beteiligten ausdrücklich zugestimmt haben. Begriffe wie JOI erklärst du sachlich und ohne Schilderung sexueller Handlungen.

Harte Regeln:
1. Nenne und beschreibe keine realen Personen, Models, Creator oder Künstlernamen. Keine Aussagen über einzelne Personen.
2. Keine expliziten sexuellen Schilderungen, keine Bezüge zu Minderjährigen, kein Hinweis auf geleaktes, privates oder gefälschtes Material.
3. Erfinde keine Fakten: Preise, Zahlen, Funktionen, Auszeichnungen, Bewertungen oder Studien nennst du nur, wenn sie im Abschnitt FAKTEN stehen. Sonst formulierst du allgemein und vorsichtig ("in der Regel", "prüfe vor der Nutzung die aktuellen Angaben des Anbieters").
4. Schreibe keine erfundenen Erfahrungsberichte, Testimonials oder Ich-/Wir-Tests. Statt Erfahrungsberichten lieferst du Kriterien, Checklisten, Einordnung und Hinweise, worauf Nutzer achten sollten.
5. Keine Links und kein HTML. Nur Markdown (Absätze, **fett**, Listen).
6. Jeder Artikel beantwortet die Suchintention hinter dem Hauptkeyword konkret und eigenständig, mit eigenem Blickwinkel. Er darf nicht wie eine Variante anderer Artikel wirken.
7. Den Anbieter nennst du neutral und sachlich als Option oder Beispiel. Keine Heilsversprechen, kein Druck, keine Superlative ohne Beleg.
8. Gehe auf Jugendschutz, Datenschutz und verantwortungsvolle Nutzung ein, wo es zum Thema passt.
Gib ausschließlich ein JSON-Objekt zurück, ohne Text davor oder danach."""

INTENT_HINTS = {
    "preise": "Die Leser wollen verstehen, wie Preise und Abrechnung grundsätzlich funktionieren und worauf sie achten sollten.",
    "gratis": "Die Leser fragen, was ohne Kosten möglich ist und wo die Grenzen liegen.",
    "sicherheit": "Die Leser sorgen sich um Anonymität, Datenschutz, Abbuchungen und Seriosität.",
    "ablauf": "Die Leser wollen eine klare Schritt-für-Schritt-Orientierung.",
    "technik": "Die Leser wollen wissen, was technisch nötig ist und wie es auf ihrem Gerät funktioniert.",
    "vergleich": "Die Leser vergleichen Angebote und brauchen verlässliche Kriterien statt Marketingaussagen.",
    "grundlagen": "Die Leser wollen eine verständliche Einführung und eine Einordnung.",
}

SCHEMA = """{
  "title": "SEO-Titel, 35-62 Zeichen, enthält das Hauptkeyword",
  "meta_description": "Beschreibung, 110-155 Zeichen, nennt den Nutzen, kein Clickbait",
  "h1": "Hauptüberschrift",
  "intro": "Einleitung in Markdown, 2 Absätze, beantwortet die Kernfrage direkt",
  "key_takeaways": ["3 bis 5 kurze Kernaussagen"],
  "sections": [{"h2": "Zwischenüberschrift", "body": "Markdown-Text, mindestens 2 Absätze oder Absatz plus Liste"}],
  "faq": [{"q": "Frage", "a": "Antwort in 2-4 Sätzen"}],
  "cta_text": "Ein neutraler Satz, der zum Anbieter überleitet, ohne Druck"
}"""


def clean_text(s: str) -> str:
    """Entfernt HTML-Tags und URLs aus generiertem Text."""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\bwww\.\S+", "", s)
    return s.strip()


def clean_content(c: dict) -> dict:
    out = {k: clean_text(v) if isinstance(v, str) else v for k, v in c.items()}
    out["key_takeaways"] = [clean_text(x) for x in c.get("key_takeaways", [])]
    out["sections"] = [{"h2": clean_text(s["h2"]), "body": clean_text(s["body"])} for s in c.get("sections", [])]
    out["faq"] = [{"q": clean_text(f["q"]), "a": clean_text(f["a"])} for f in c.get("faq", [])]
    return out


FACTS_MARKER = "## Eingetragene Fakten"


def load_facts(root: Path) -> str:
    """Nur der Abschnitt nach der Überschrift zählt. Die Formatbeispiele oben werden nie an das Modell gegeben."""
    p = root / "facts.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    body = text.split(FACTS_MARKER, 1)[1] if FACTS_MARKER in text else ""
    body = body.replace("(noch keine eingetragen)", "").strip()
    return body


def build_prompt(cluster: dict, cfg: dict, facts: str, other_titles: list[str], issues: list[str] | None) -> str:
    lim = cfg["limits"]
    extra = [k for k in cluster["keywords"] if k != cluster["primary"]][:12]
    parts = [
        f"Hauptkeyword: {cluster['primary']}",
        f"Weitere Suchbegriffe, die natürlich vorkommen sollen: {', '.join(extra) if extra else '(keine)'}",
        f"Thema: {cfg['topic_labels'].get(cluster['topic'], cluster['topic'])}. {INTENT_HINTS.get(cluster['topic'], '')}",
        f"Anbieter, auf den der Artikel am Ende neutral hinführt: {cfg['affiliate']['provider_name']}",
        f"Umfang: mindestens {lim['min_words']} Wörter insgesamt, mindestens {lim['min_sections']} Abschnitte, "
        f"mindestens {lim['min_faq']} FAQ-Einträge.",
        "FAKTEN (einzige zulässige Quelle für konkrete Angaben über den Anbieter):\n" + (facts.strip() or "(keine)"),
    ]
    if other_titles:
        sample = other_titles[-40:]
        parts.append("Bereits vorhandene Artikel. Wähle einen klar anderen Schwerpunkt und Aufbau:\n- " + "\n- ".join(sample))
    if issues:
        parts.append("Der letzte Entwurf wurde abgelehnt. Behebe diese Punkte:\n- " + "\n- ".join(issues))
    parts.append("Antworte nur mit einem JSON-Objekt in genau diesem Format:\n" + SCHEMA)
    return "\n\n".join(parts)


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("keine JSON-Antwort")
    return json.loads(m.group(0))


def call_claude(cfg: dict, user: str) -> str:
    import anthropic  # nur bei Bedarf

    client = anthropic.Anthropic(max_retries=5)
    resp = client.messages.create(
        model=cfg["llm"]["model"],
        max_tokens=cfg["llm"]["max_tokens"],
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


# ---------- Mock (nur für Tests ohne API) ----------

_POOL = [
    "Wer sich einen ersten Überblick verschaffen will, sollte zuerst die Grundlagen klären.",
    "Vergleiche die Angaben des Anbieters immer mit den aktuellen Bedingungen auf der Seite selbst.",
    "Eine klare Preisübersicht vor der Nutzung verhindert unangenehme Überraschungen bei der Abrechnung.",
    "Achte darauf, dass die Verbindung verschlüsselt ist und der Anbieter ein Impressum nennt.",
    "Seriöse Plattformen verlangen eine Altersprüfung und erklären transparent, wie Zahlungen funktionieren.",
    "Setze dir vorab ein festes Budget, damit die Kosten jederzeit überschaubar bleiben.",
    "Lies die Nutzungsbedingungen, bevor du ein Konto anlegst oder Guthaben auflädst.",
    "Wer Wert auf Diskretion legt, prüft, welche Angaben im Kontoprofil sichtbar sind.",
    "Ein stabiler Internetzugang sorgt dafür, dass Bild und Ton ohne Unterbrechung laufen.",
    "Kündigungswege sollten leicht auffindbar sein und ohne versteckte Fristen funktionieren.",
    "Gerade Einsteiger profitieren von einer kurzen Checkliste mit den wichtigsten Prüfpunkten.",
    "Verwende ein eigenes Passwort für das Konto und aktiviere zusätzliche Schutzfunktionen, falls vorhanden.",
    "Unabhängige Informationen helfen dabei, Werbeversprechen realistisch einzuordnen.",
    "Bezahlmethoden unterscheiden sich in Gebühren, Bearbeitungsdauer und Datenschutz.",
    "Auf dem Smartphone gelten teilweise andere Einstellungen als im Desktop-Browser.",
    "Wer unsicher ist, startet mit kleinen Beträgen und beobachtet die Abrechnung genau.",
    "Die Qualität der Übertragung hängt von Gerät, Verbindung und Einstellungen ab.",
    "Hinweise zum Jugendschutz gehören bei Angeboten für Erwachsene zu den Grundanforderungen.",
    "Ein Blick in die Hilfeseiten zeigt oft schnell, wie erreichbar der Support wirklich ist.",
    "Persönliche Daten sollten nur dort eingegeben werden, wo sie wirklich benötigt werden.",
    "Ein Vergleich mehrerer Angebote lohnt sich, weil sich Preismodelle deutlich unterscheiden können.",
    "Transparente Abrechnungen mit nachvollziehbaren Einzelposten sind ein gutes Qualitätsmerkmal.",
    "Prüfe regelmäßig den Kontostand und die letzten Buchungen, um Fehler früh zu erkennen.",
    "Wer das Angebot nur testen möchte, sollte vorab klären, welche Funktionen ohne Zahlung verfügbar sind.",
    "Verantwortungsvolle Nutzung bedeutet auch, Zeit und Ausgaben bewusst zu begrenzen.",
    "Datenschutzerklärungen verraten, welche Informationen gespeichert und weitergegeben werden.",
    "Ein ruhiger Ort und ein aktuelles Gerät verbessern das Nutzungserlebnis spürbar.",
    "Fragen zur Abrechnung lassen sich meist am besten direkt über den Support des Anbieters klären.",
    "Wer Alternativen vergleicht, sollte nicht nur auf den Preis, sondern auch auf Sicherheit achten.",
    "Eine schrittweise Anleitung hilft, typische Fehler bei der Anmeldung zu vermeiden.",
]


def mock_generate(cluster: dict, cfg: dict, issues=None) -> dict:
    rnd = random.Random(cluster["slug"])
    kw = cluster["primary"]
    cap = kw[:1].upper() + kw[1:]

    def paragraph(n: int) -> str:
        return " ".join(rnd.sample(_POOL, n))

    sections = [{"h2": f"{cap}: Punkt {i + 1}", "body": paragraph(5) + "\n\n" + paragraph(4)} for i in range(5)]
    return {
        "title": f"{cap}: Ratgeber und Überblick",
        "meta_description": f"Alles Wichtige zu {kw}: verständlich erklärt, mit Checkliste und Antworten auf häufige Fragen.",
        "h1": f"{cap}: das solltest du wissen",
        "intro": f"{cap} ist eine häufige Frage. " + paragraph(3) + "\n\n" + paragraph(3),
        "key_takeaways": [rnd.choice(_POOL) for _ in range(4)],
        "sections": sections,
        "faq": [{"q": f"Was bedeutet {kw}?", "a": paragraph(2)},
                {"q": f"Worauf sollte ich bei {kw} achten?", "a": paragraph(2)},
                {"q": f"Wie starte ich am besten mit {kw}?", "a": paragraph(2)}],
        "cta_text": f"Wenn du dir selbst ein Bild machen möchtest, findest du das Angebot direkt beim Anbieter.",
    }


def generate(cluster: dict, cfg: dict, facts: str, other_titles: list[str],
             issues: list[str] | None = None, mock: bool = False) -> dict:
    if mock:
        return clean_content(mock_generate(cluster, cfg, issues))
    text = call_claude(cfg, build_prompt(cluster, cfg, facts, other_titles, issues))
    return clean_content(parse_json(text))
