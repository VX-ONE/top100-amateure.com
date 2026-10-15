"""Keyword-Import, Themenfilter und Clustering.

Grundsatz: Es werden nur Keywords verarbeitet, die vollständig aus dem erlaubten Vokabular
(config.yaml: topics, vocab) bestehen. Eigennamen, Künstlernamen, Marken Dritter und
unbekannte Begriffe fallen automatisch heraus. Gesperrte Begriffe (blocked_terms und
data/blocked_names.txt) werden nie verarbeitet.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from datetime import date
from pathlib import Path

# ---------- Normalisierung ----------


def normalize(kw: str) -> str:
    kw = unicodedata.normalize("NFKC", kw).lower().strip()
    kw = re.sub(r"[\"'“”„‚‘’()\[\]{},;:!?|/\\]+", " ", kw)
    return re.sub(r"\s+", " ", kw).strip()


def slugify(text: str) -> str:
    t = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:80] or "seite"


# ---------- Vokabular ----------


class Vocab:
    def __init__(self, cfg: dict):
        self.phrases: set[str] = set()
        self.words: set[str] = set()
        self.term_topic: dict[str, str] = {}
        self.topic_order: list[str] = list(cfg["topics"].keys())
        for group, terms in cfg["topics"].items():
            for t in terms:
                t = normalize(t)
                self.term_topic.setdefault(t, group)
                self._add(t)
        self.generic: set[str] = set()
        for t in cfg["vocab"]["generic"]:
            t = normalize(t)
            self.generic.add(t)
            self._add(t)
        for t in cfg["vocab"]["brand"]:
            self._add(normalize(t))
        self.phrases_sorted = sorted(self.phrases, key=len, reverse=True)

    def _add(self, term: str) -> None:
        (self.phrases if " " in term else self.words).add(term)


def load_blocked(cfg: dict) -> list[str]:
    terms = [normalize(t) for t in cfg.get("blocked_terms", [])]
    names = Path(cfg["_data"]) / "blocked_names.txt"
    if names.exists():
        terms += [normalize(x) for x in names.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [t for t in terms if t]


def blocked_hit(text: str, terms: list[str]) -> str | None:
    """Erster gesperrter Begriff, der am Wortanfang im Text steht (ganze Wörter und Wortanfänge)."""
    for term in terms:
        if re.search(rf"(?<![\wäöüß]){re.escape(term)}", text):
            return term
    return None


def classify(kw: str, cfg: dict, vocab: Vocab, blocked: list[str]) -> tuple[bool, str | None, str]:
    """Gibt (ok, thema, grund) zurück."""
    k = normalize(kw)
    toks = k.split()
    lo, hi = cfg["keywords"]["min_tokens"], cfg["keywords"]["max_tokens"]
    if not lo <= len(toks) <= hi:
        return False, None, f"Länge ({len(toks)} Wörter)"
    hit = blocked_hit(k, blocked)
    if hit:
        return False, None, f"gesperrter Begriff: {hit}"

    rest = f" {k} "
    matched: list[str] = []
    for ph in vocab.phrases_sorted:
        needle = f" {ph} "
        while needle in rest:
            matched.append(ph)
            rest = rest.replace(needle, " ", 1)
    for tok in rest.split():
        if tok in vocab.words:
            matched.append(tok)
        elif re.fullmatch(r"\d{1,4}", tok) and tok not in ("18", "19"):
            continue
        else:
            return False, None, f"unbekanntes Wort: {tok}"

    topics = {vocab.term_topic[m] for m in matched if m in vocab.term_topic}
    if not topics:
        return False, None, "kein Themenbezug"
    topic = min(topics, key=vocab.topic_order.index)
    return True, topic, "ok"


# ---------- Einlesen ----------

_QUERY_HEADERS = {"top queries", "query", "queries", "suchanfrage", "suchanfragen",
                  "häufigste suchanfragen", "keyword", "keywords", "suchbegriff"}
_CLICK_HEADERS = {"clicks", "klicks"}
_IMPR_HEADERS = {"impressions", "impressionen"}


def _num(value: str) -> int:
    """Klicks und Impressionen sind ganze Zahlen; Tausendertrenner (1.234 / 1,234) werden entfernt."""
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else 0


def read_keywords(path: str | Path) -> dict[str, dict]:
    """Liest eine GSC-Exportdatei (CSV, Komma oder Semikolon) oder eine Textdatei mit einem Keyword pro Zeile."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    out: dict[str, dict] = {}

    def add(kw: str, clicks: int, impr: int) -> None:
        k = normalize(kw)
        if not k:
            return
        d = out.setdefault(k, {"clicks": 0, "impressions": 0})
        d["clicks"] += clicks
        d["impressions"] += impr

    first = raw.splitlines()[0] if raw.strip() else ""
    if "," not in first and ";" not in first and "\t" not in first:
        for line in raw.splitlines():
            add(line, 0, 0)
        return out

    dialect = csv.Sniffer().sniff(first, delimiters=",;\t")
    reader = csv.reader(io.StringIO(raw), dialect)
    header = [h.strip().lower() for h in next(reader)]
    qi = next((i for i, h in enumerate(header) if h in _QUERY_HEADERS), 0)
    ci = next((i for i, h in enumerate(header) if h in _CLICK_HEADERS), None)
    ii = next((i for i, h in enumerate(header) if h in _IMPR_HEADERS), None)
    for row in reader:
        if len(row) <= qi:
            continue
        add(row[qi], _num(row[ci]) if ci is not None and ci < len(row) else 0,
            _num(row[ii]) if ii is not None and ii < len(row) else 0)
    return out


# ---------- KI-Vorprüfung ----------

SCREEN_PROMPT = (
    "Du prüfst Suchbegriffe für einen Ratgeber über Live-Cam-Plattformen. Gib als JSON-Array "
    "alle Suchbegriffe zurück, die (a) den Namen, Künstler- oder Nutzernamen einer realen Person, "
    "eines Models oder Creators enthalten oder enthalten könnten, (b) auf Minderjährige zielen oder "
    "(c) auf geleaktes, privates oder gefälschtes Material zielen. Im Zweifel aufnehmen. "
    "Antworte nur mit dem JSON-Array, ohne weiteren Text.\n\nSuchbegriffe:\n"
)


def llm_screen(keywords: list[str], cfg: dict) -> set[str]:
    import anthropic  # nur bei Bedarf

    client = anthropic.Anthropic(max_retries=5)
    bad: set[str] = set()
    for i in range(0, len(keywords), 50):
        batch = keywords[i:i + 50]
        resp = client.messages.create(
            model=cfg["llm"]["screen_model"],
            max_tokens=2000,
            messages=[{"role": "user", "content": SCREEN_PROMPT + json.dumps(batch, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            try:
                bad.update(k for k in json.loads(m.group(0)) if k in batch)
            except json.JSONDecodeError:
                bad.update(batch)  # nicht auswertbar: lieber nichts durchlassen
        else:
            bad.update(batch)
    return bad


# ---------- Clustering ----------


SYNONYMS = {"porn": "porno", "pornos": "porno", "deutsche": "deutsch", "deutsches": "deutsch", "deutscher": "deutsch",
            "amateure": "amateur", "reifer": "reife", "hausfrauen": "hausfrau", "paare": "paar", "pärchen": "paar",
            "mollige": "mollig", "brünette": "brünett", "blonde": "blond", "rothaarige": "rothaarig",
            "nude": "nackt", "kostenlose": "kostenlos", "gratis": "kostenlos", "umsonst": "kostenlos",
            "sexvideos": "videos", "erotikvideos": "videos", "video": "videos", "clip": "clips",
            "livecams": "livecam", "cams": "cam", "sexcams": "sexcam", "toy": "toys", "heels": "heels"}


def _tokset(kw: str, vocab: Vocab) -> set[str]:
    return {SYNONYMS.get(t, t) for t in kw.split() if t not in vocab.generic}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def import_file(path: str, cfg: dict, store, use_llm: bool = True) -> dict:
    return import_found(read_keywords(path), cfg, store, use_llm=use_llm)


def import_found(found: dict[str, dict], cfg: dict, store, use_llm: bool = True) -> dict:
    """Filtert Keywords (Vokabular, Sperrliste, KI-Vorprüfung), clustert sie und stellt sie in die Warteschlange."""
    vocab = Vocab(cfg)
    blocked = load_blocked(cfg)

    clusters = store.clusters()
    pages = store.pages()
    rejected_path = Path(cfg["_data"]) / "rejected.csv"
    known = {k for c in clusters for k in c["keywords"]}
    prev_rejected: set[str] = set()
    if rejected_path.exists():
        prev_rejected = {r[0] for r in csv.reader(rejected_path.open(encoding="utf-8")) if r}

    stats = {"gelesen": len(found), "bekannt": 0, "abgelehnt": 0, "neu": 0, "zu_bestehender_seite": 0}
    accepted: list[tuple[str, str, dict]] = []
    rejected_rows: list[list] = []
    reasons: dict[str, int] = {}

    for kw, m in sorted(found.items(), key=lambda x: -x[1]["impressions"]):
        if kw in known or kw in prev_rejected:
            stats["bekannt"] += 1
            continue
        ok, topic, reason = classify(kw, cfg, vocab, blocked)
        if not ok:
            rejected_rows.append([kw, reason, m["impressions"]])
            r = reason.split(":")[0]
            reasons[r] = reasons.get(r, 0) + 1
            continue
        accepted.append((kw, topic, m))

    if accepted and use_llm:
        try:
            bad = llm_screen([k for k, _, _ in accepted], cfg)
        except Exception as e:  # kein API-Key, kein Netz usw.
            print(f"Hinweis: KI-Vorprüfung übersprungen ({type(e).__name__}: {e}). "
                  "Es gilt nur der Vokabularfilter.")
            bad = set()
        for kw, _, m in accepted:
            if kw in bad:
                rejected_rows.append([kw, "KI-Vorprüfung: Person/Minderjährige/geleakt", m["impressions"]])
                reasons["KI-Vorprüfung"] = reasons.get("KI-Vorprüfung", 0) + 1
        accepted = [a for a in accepted if a[0] not in bad]

    threshold = cfg["keywords"]["cluster_similarity"]
    used_slugs = {c["slug"] for c in clusters}
    for kw, topic, m in accepted:  # bereits nach Impressionen sortiert
        ts = _tokset(kw, vocab)
        target = next((c for c in clusters if c["topic"] == topic and _jaccard(ts, _tokset(c["primary"], vocab)) >= threshold), None)
        if target:
            target["keywords"].append(kw)
            target["impressions"] += m["impressions"]
            target["clicks"] += m["clicks"]
            if target["slug"] in pages:
                pages[target["slug"]]["keywords"] = target["keywords"]
            stats["zu_bestehender_seite"] += 1
            continue
        slug = base = slugify(kw)
        n = 2
        while slug in used_slugs:
            slug, n = f"{base}-{n}", n + 1
        used_slugs.add(slug)
        clusters.append({
            "slug": slug, "primary": kw, "keywords": [kw], "topic": topic,
            "impressions": m["impressions"], "clicks": m["clicks"],
            "status": "queued", "added": date.today().isoformat(), "fail_count": 0,
        })
        stats["neu"] += 1

    stats["abgelehnt"] = len(rejected_rows)
    stats["gruende"] = reasons
    store.save_clusters(clusters)
    store.save_pages(pages)
    if rejected_rows:
        rejected_path.parent.mkdir(parents=True, exist_ok=True)
        with rejected_path.open("a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rejected_rows)
    return stats
