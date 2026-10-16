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
8. Jugendschutz, Datenschutz und Einwilligung behandelst du knapp in höchstens einem Abschnitt. Der Hauptteil beantwortet, was die Suchenden wirklich wissen oder finden wollen.
9. Titel und H1: Hauptkeyword in natürlicher Schreibweise und Groß-/Kleinschreibung (z. B. "Deutsche Pornos" statt "deutsch porno", "Reife Frauen" statt "porno reife"), Titel klickstark und konkret, keine Doppelpunkt-Floskeln wie "Was du wissen musst".
10. Schreibe konkret, abwechslungsreich und mit Beispielen statt Floskeln. Vermeide typische Füllsätze ("In der heutigen Zeit", "Zusammenfassend lässt sich sagen", "Es ist wichtig zu beachten").
Gib ausschließlich ein JSON-Objekt zurück, ohne Text davor oder danach."""

INTENT_HINTS = {
    "preise": "Die Leser wollen verstehen, wie Preise und Abrechnung grundsätzlich funktionieren und worauf sie achten sollten.",
    "gratis": "Die Leser fragen, was ohne Kosten möglich ist und wo die Grenzen liegen.",
    "sicherheit": "Die Leser sorgen sich um Anonymität, Datenschutz, Abbuchungen und Seriosität.",
    "ablauf": "Die Leser wollen eine klare Schritt-für-Schritt-Orientierung.",
    "technik": "Die Leser wollen wissen, was technisch nötig ist und wie es auf ihrem Gerät funktioniert.",
    "vergleich": "Die Leser vergleichen Angebote und brauchen verlässliche Kriterien statt Marketingaussagen.",
    "grundlagen": "Die Leser wollen eine verständliche Einführung und eine Einordnung.",
    "dach": ("Die Leser kommen aus Österreich oder der Schweiz. Beantworte ihre Frage konkret (Anmeldung, Zahlung, Kosten, "
             "Rechtslage, Jugendschutz) und weise darauf hin, dass sich Zahlungsarten, Währungen und Regeln je nach Land "
             "unterscheiden können und beim Anbieter geprüft werden sollten. Keine rechtlichen Zusagen und keine "
             "erfundenen Angaben zu Zahlungsarten oder Preisen."),
    "webcam-models": ("Die Leser wollen verstehen, wie Webcam-Models und Live-Shows funktionieren: Ablauf eines Chats, Unterschied zwischen "
                      "öffentlichem und privatem Chat, respektvoller Umgang, Auswahl nach Profil und Bewertungen, Kosten und Sicherheit. "
                      "Keine Beschreibung sexueller Handlungen, keine Angaben zu konkreten Personen, nichts erfinden."),
    "livecams": ("Die Leser wollen Live-Cams und Livechat verstehen: Ablauf, Chatarten, Technik, Kosten, Anonymität, Sicherheit. "
                 "Sachlich, ohne explizite Schilderung."),
    "videos": ("Die Leser suchen Videos und Clips für Erwachsene: wie Videoangebote (Kauf, Streaming, Abrechnung) grundsätzlich funktionieren, "
               "woran man seriöse Anbieter erkennt, Jugendschutz und Datenschutz. Ohne explizite Schilderung."),
    "amateure": ("Die Leser interessieren sich für Amateur-Erotik: was sie ausmacht, Unterschied zu Studioproduktionen, wie Einvernehmlichkeit "
                 "und Altersprüfung erkennbar sind, wie man Angebote seriös prüft. Ohne explizite Schilderung."),
    "nischen": ("Die Leser suchen ein Genre oder eine Nische der Amateur-Erotik. Erkläre sachlich und ohne explizite "
                "Schilderung, was der Begriff meint, wie legale und einvernehmliche Angebote zu erkennen sind "
                "(Anbieter mit Altersprüfung, Verträgen und Einwilligung der Beteiligten), was Live-Cams und Videos "
                "dazu grundsätzlich bieten, worauf bei Kosten, Datenschutz und Jugendschutz zu achten ist, und nenne "
                "Alternativen. Keine Beschreibung von Handlungen, keine Wörter aus der Vulgärsprache."),
}

ANGLES = [
    "Kosten und Abrechnung: was man wissen und prüfen sollte",
    "Checkliste zum Abhaken vor der Nutzung",
    "Einsteiger-Guide: erste Schritte und typische Fragen",
    "Sicherheit, Datenschutz und Diskretion",
    "Vergleich und Alternativen mit klaren Kriterien",
    "Häufige Fehler und wie man sie vermeidet",
    "Ablauf Schritt für Schritt",
    "Mythen und Fakten",
    "Fragen und Antworten aus Sicht der Suchenden",
    "Rechtliches, Jugendschutz und Einwilligung",
]


# Für Suchen, bei denen Leute ein Angebot finden wollen (Genres, Livecams, Videos, Models, Amateure), passt ein
# Ratgeber über Fehler oder Rechtliches nicht zur Suchabsicht. Diese Blickwinkel beantworten, was die Leute suchen.
OFFER_TOPICS = {"nischen", "livecams", "videos", "webcam-models", "amateure", "dach"}
OFFER_ANGLES = [
    "Überblick: Wo du das findest, was es bei Visit-X dazu gibt (Live-Cams, Videos, Amateure) und wie du startest",
    "Auswahl-Guide: worauf du bei Models, Profilen und Inhalten achtest, damit es zu deinen Vorlieben passt",
    "Live-Cam oder Video? Unterschiede, Kosten in Tokens und was zu wem passt",
    "Einstieg in 5 Schritten: Konto, Guthaben in Tokens aufladen, passende Models finden, Chat oder Video",
    "Was die Kategorie ausmacht, warum sie beliebt ist und wie du die besten Inhalte dazu findest",
]


INTENT_ANGLES = [
    (("erfahrungen", "test", "bewertung"), "Erfahrungen und Überblick: was dich erwartet, Stärken, Schwächen und für wen es passt"),
    (("alternativen", "vergleich"), "Vergleich und Alternativen mit klaren Kriterien"),
    (("seriös", "abzocke", "sicher", "anonym", "betrug"), "Sicherheit, Seriosität und Diskretion: woran du das erkennst"),
    (("anmeldung", "anmelden", "registrieren", "login", "konto"), "Ablauf Schritt für Schritt"),
    (("kündigen", "löschen", "abmelden"), "Ablauf Schritt für Schritt (Kündigen bzw. Konto löschen) und typische Stolpersteine"),
    (("kosten", "preise", "coins", "tokens", "guthaben", "zahlungsmethoden", "paypal", "bezahlen"),
     "Kosten und Bezahlung: Tokens, Zahlungsarten und worauf man achten sollte"),
    (("app", "handy", "iphone", "android"), "Technik: App, Handy und Browser, was du brauchst"),
    (("kostenlos", "gratis", "free"), "Was kostenlos geht, wo die Grenzen liegen und wie du günstig startest"),
]


def pick_angle(cluster: dict) -> str:
    import zlib
    words = set(cluster.get("primary", "").split())
    for keys, angle in INTENT_ANGLES:
        if words & set(keys):
            return angle
    if cluster.get("topic") in OFFER_TOPICS:
        return OFFER_ANGLES[(zlib.crc32(cluster["slug"].encode()) + cluster.get("fail_count", 0)) % len(OFFER_ANGLES)]
    return ANGLES[(zlib.crc32(cluster["slug"].encode()) + cluster.get("fail_count", 0)) % len(ANGLES)]


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
        f"Blickwinkel dieser Seite (gibt Aufbau und Schwerpunkt vor): {pick_angle(cluster)}",
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


CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "meta_description": {"type": "string"}, "h1": {"type": "string"},
        "intro": {"type": "string"}, "key_takeaways": {"type": "array", "items": {"type": "string"}},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "h2": {"type": "string"}, "body": {"type": "string"}}, "required": ["h2", "body"]}},
        "faq": {"type": "array", "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string"}}, "required": ["q", "a"]}},
        "cta_text": {"type": "string"},
    },
    "required": ["title", "meta_description", "h1", "intro", "key_takeaways", "sections", "faq", "cta_text"],
}


def ask_json(cfg: dict, system: str, user: str, schema: dict, max_tokens: int | None = None) -> dict:
    """Antwort als Werkzeugaufruf mit festem Schema, damit das Ergebnis gültiges JSON ist.
    Manche Modelle erlauben kein erzwungenes Werkzeug (tool_choice "tool"); dann wird "auto" mit klarer Anweisung
    genutzt, und als letzter Rückfall wird JSON aus dem Text gelesen."""
    import anthropic  # nur bei Bedarf

    client = anthropic.Anthropic(max_retries=5)
    tools = [{"name": "antwort", "description": "Gibt das fertige Ergebnis im vorgegebenen Format zurück. Immer aufrufen.",
              "input_schema": schema}]
    base = dict(model=cfg["llm"]["model"], max_tokens=max_tokens or cfg["llm"]["max_tokens"], tools=tools)
    note = "\n\nGib dein Ergebnis ausschließlich über einen Aufruf des Werkzeugs \"antwort\" zurück."
    resp = None
    if cfg["llm"].get("tool_mode", "auto") == "force":
        try:
            resp = client.messages.create(**base, system=system, tool_choice={"type": "tool", "name": "antwort"},
                                          messages=[{"role": "user", "content": user}])
        except anthropic.BadRequestError:
            resp = None
    if resp is None:
        resp = client.messages.create(**base, system=system + note, tool_choice={"type": "auto"},
                                      messages=[{"role": "user", "content": user + note}])
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise ValueError("Antwort wurde abgeschnitten (max_tokens)")
    for b in resp.content:
        if b.type == "tool_use":
            return _normalize(b.input, schema)
    text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
    return _normalize(parse_json(text), schema)


def _normalize(data, schema: dict) -> dict:
    """Ohne erzwungenes Werkzeug liefert das Modell manchmal verpackte Daten: das ganze Objekt als JSON-Text,
    unter einem Hüllschlüssel ({"antwort": {...}}) oder Listen als JSON-Text. Das wird hier ausgepackt."""
    if isinstance(data, str):
        data = parse_json(data)
    props = set((schema or {}).get("properties", {}))
    if isinstance(data, dict) and props and not (props & set(data)) and len(data) == 1:
        inner = next(iter(data.values()))
        if isinstance(inner, str):
            try:
                inner = parse_json(inner)
            except Exception:
                pass
        if isinstance(inner, dict):
            data = inner
    if isinstance(data, dict):
        for k, v in list(data.items()):
            if isinstance(v, str) and v.strip()[:1] in "[{":
                try:
                    data[k] = json.loads(v)
                except Exception:
                    pass
    return data


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
    return clean_content(ask_json(cfg, SYSTEM, build_prompt(cluster, cfg, facts, other_titles, issues), CONTENT_SCHEMA))


# ---------- Model-Profilseiten ----------

MODEL_SYSTEM = """Du bist Redakteur einer unabhängigen Informationsseite über Live-Cam- und Videoangebote für Erwachsene. Du schreibst eine neutrale, sachliche Profilseite über ein Model auf Deutsch für Leser ab 18 Jahren.

Harte Regeln:
1. Nenne ausschließlich den Bühnennamen aus der Eingabe. Keine echten Namen, kein Alter, keine Angaben zu Aussehen oder Körper (Fakten zu Stil und Genre dürfen genannt werden), kein Wohnort, kein Privatleben, keine Social-Media-Kontonamen, keine Aussagen über andere Personen.
2. Über das Model und das Angebot behauptest du nur, was im Abschnitt FAKTEN steht. Zahlen (z. B. Videoanzahl, Rhythmus) nur genau so, wie sie in FAKTEN stehen. Keine Bewertungen, keine Auszeichnungen, keine Preise, keine Aussagen zu Inhalten, die dort nicht genannt sind. Alles Weitere formulierst du allgemein ("laut Anbieter", "die aktuellen Bedingungen stehen beim Anbieter").
3. Keine expliziten Schilderungen und keine Beschreibung sexueller Handlungen. Die Seite informiert: was es beim Anbieter zu dem Namen gibt, wie Videos, Direkt-Chat und Live-Shows grundsätzlich funktionieren, worauf man bei Kosten, Abrechnung, Datenschutz und Jugendschutz achten sollte.
4. Keine erfundenen Erfahrungsberichte, Testimonials oder Ich-/Wir-Tests. Keine Hinweise auf geleaktes, privates oder fremdes Material. Weise darauf hin, dass nur Inhalte genutzt werden sollten, die der Anbieter offiziell bereitstellt.
5. Stelle klar, dass die Seite ein unabhängiger Ratgeber ist und nicht vom Model oder vom Anbieter betrieben wird.
6. Das Hauptkeyword steht im Titel und in der Hauptüberschrift. Es darf dort wörtlich vorkommen, im Text bleibst du sachlich.
7. Keine Links und kein HTML. Nur Markdown (Absätze, **fett**, Listen).
Gib ausschließlich ein JSON-Objekt zurück, ohne Text davor oder danach."""


def build_model_prompt(model: dict, cluster: dict, cfg: dict, facts: str, other_titles: list[str],
                       issues: list[str] | None) -> str:
    lim = cfg["models"]
    extra = [k for k in cluster["keywords"] if k != cluster["primary"]][:10]
    parts = [
        f"Bühnenname des Models: {model['name']} (immer exakt so schreiben, auch im Titel und in der H1)",
        f"Hauptkeyword: {cluster['primary']}",
        f"Weitere Suchbegriffe, die natürlich vorkommen sollen: {', '.join(extra) if extra else '(keine)'}",
        f"Anbieter: {cfg['affiliate']['provider_name']}",
        f"Umfang: mindestens {lim['min_words']} Wörter, mindestens {cfg['limits']['min_sections']} Abschnitte, "
        f"mindestens {cfg['limits']['min_faq']} FAQ-Einträge.",
        "FAKTEN über das Model und das Angebot (einzige zulässige Quelle):\n- " + "\n- ".join(model["facts"]),
        "Allgemeine Fakten über den Anbieter:\n" + (facts.strip() or "(keine)"),
        "Titel-Muster (abwandeln, nicht wörtlich kopieren): '<Bühnenname>: Videos, Livecam und Chat bei Visit-X'. "
        "Ton: wie ein Fan-Guide, positiv und konkret, nicht defensiv. Keine Sätze wie 'Mehr behaupten wir nicht'. "
        "Jugendschutz und Datenschutz nur in einem kurzen Absatz, nicht als eigener langer Abschnitt. "
        "Der erste Abschnitt heißt 'Steckbrief' und listet die FAKTEN als kurze Markdown-Liste. "
        "Danach lebendige, konkrete Abschnitte, die die FAKTEN einordnen (was Fans erwartet, wie man sie erreicht). "
        "Mögliche weitere Abschnitte: Was es bei Visit-X zu diesem Namen gibt (nur FAKTEN); Videos, Direkt-Chat und "
        "Live-Shows allgemein erklärt; Kosten und Abrechnung, worauf man achten sollte; Jugendschutz, Datenschutz "
        "und legale Nutzung; Alternativen und Vergleich mit anderen Angeboten.",
    ]
    if other_titles:
        parts.append("Bereits vorhandene Artikel (anderer Schwerpunkt):\n- " + "\n- ".join(other_titles[-30:]))
    if issues:
        parts.append("Der letzte Entwurf wurde abgelehnt. Behebe diese Punkte:\n- " + "\n- ".join(issues))
    parts.append("Antworte nur mit einem JSON-Objekt in genau diesem Format:\n" + SCHEMA)
    return "\n\n".join(parts)


def generate_model(model: dict, cluster: dict, cfg: dict, facts: str, other_titles: list[str],
                   issues: list[str] | None = None, mock: bool = False) -> dict:
    if mock:
        return clean_content(mock_generate(cluster, cfg, issues))
    return clean_content(ask_json(cfg, MODEL_SYSTEM, build_model_prompt(model, cluster, cfg, facts, other_titles, issues), CONTENT_SCHEMA))
