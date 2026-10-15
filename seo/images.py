"""Automatische Bilder pro Seite: KI-generierte Vektorgrafik (SVG) als Titelbild plus Vorschaubild (PNG) fürs Teilen.

Grenzen, die im Code erzwungen werden:
- Keine Fotos, keine Personen, keine Körperteile, keine Marken oder Logos: nur Motive, Formen und kurze Schrift.
- Das SVG wird streng gefiltert (nur erlaubte Elemente und Attribute, keine Skripte, keine externen Verweise).
- Schlägt die KI-Erzeugung fehl oder besteht die Prüfung nicht, entsteht eine einfache Grafik aus Farbe, Form und Titel.
"""
from __future__ import annotations

import hashlib
import json
import re
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

W, H = 1200, 630

MOTIFS = {
    "preise": "Münzen, Preisschild, Geldbörse oder Taschenrechner",
    "gratis": "Geschenkschleife, Stern, Gutschein oder Haken",
    "sicherheit": "Schloss, Schild, Schlüssel oder Fingerabdruck-Symbol",
    "ablauf": "Schritte mit Pfeilen, Checkliste, Häkchen oder Wegweiser",
    "technik": "Smartphone, Laptop, Kamera-Symbol, Kopfhörer oder WLAN-Zeichen",
    "vergleich": "Waage, zwei Karten nebeneinander, Sterne oder Balken",
    "webcam-models": "Webcam auf Stativ, Ringlicht, Bildschirm mit Chatfenster, Kopfhörer, Herz",
    "livecams": "Bildschirm mit LIVE-Badge, Kamera, Sprechblasen, Herz",
    "videos": "Play-Button, Filmstreifen, Bildschirm, Kamera, Popcorn-freie Symbole",
    "amateure": "Smartphone-Kamera, Ringlicht, Sofa-Silhouette, Herz, Sprechblase",
    "grundlagen": "Kamera-Symbol, Sprechblasen, Herz, Bildschirm, Mikrofon oder Lippenstift",
    "profile": "Bühnenvorhang, Scheinwerfer, Sprechblasen, Sterne oder Bildschirm",
}
# Stiefel und High Heels sind erlaubt, wenn das Thema passt (reine Gegenstände, ohne Körper).
FASHION_HINT = ("Wenn das Thema zu Mode, Outfits oder Accessoires passt, darfst du als Gegenstand Stiefel, "
                "High Heels oder Handschuhe als flache Silhouette zeichnen, immer ohne Körper.")

PALETTES = [
    ("#b91d47", "#4a0f2a"), ("#6d28d9", "#1e1b4b"), ("#0f766e", "#042f2e"),
    ("#c2410c", "#431407"), ("#1d4ed8", "#0b1b4d"), ("#be185d", "#3b0a24"),
]

ALLOWED_TAGS = {"svg", "g", "defs", "rect", "circle", "ellipse", "path", "polygon", "polyline", "line",
                "lineargradient", "radialgradient", "stop", "text", "tspan", "title"}
ALLOWED_ATTRS = {"viewbox", "xmlns", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
                 "d", "points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "opacity",
                 "fill-opacity", "stroke-opacity", "transform", "id", "offset", "stop-color", "stop-opacity",
                 "gradientunits", "gradienttransform", "fx", "fy", "font-size", "font-weight", "font-family",
                 "text-anchor", "letter-spacing", "dominant-baseline", "fill-rule", "clip-rule"}
BAD_VALUE = re.compile(r"(javascript:|data:|http:|https:|//|expression\(|@import)", re.I)
MAX_SVG = 24000


def _palette(slug: str) -> tuple[str, str]:
    return PALETTES[int(hashlib.sha1(slug.encode()).hexdigest(), 16) % len(PALETTES)]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def sanitize_svg(svg: str) -> str | None:
    """Gibt ein bereinigtes SVG zurück oder None, wenn etwas nicht erlaubt ist (dann wird das Bild verworfen)."""
    svg = svg.strip()
    if len(svg) > MAX_SVG or "<!DOCTYPE" in svg.upper() or "<!ENTITY" in svg.upper():
        return None
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return None
    if _local(root.tag) != "svg":
        return None
    for el in root.iter():
        if _local(el.tag) not in ALLOWED_TAGS:
            return None
        for k, v in list(el.attrib.items()):
            key = _local(k)
            if key not in ALLOWED_ATTRS or BAD_VALUE.search(v):
                return None
        if el.text and _local(el.tag) in ("text", "tspan") and len(el.text) > 40:
            return None
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    out = ET.tostring(root, encoding="unicode").replace("ns0:", "").replace(":ns0", "")
    if 'xmlns="http://www.w3.org/2000/svg"' not in out[:400]:
        out = out.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    try:  # muss wohlgeformtes XML sein, sonst zeigt der Browser nichts an
        ET.fromstring(out)
    except ET.ParseError:
        return None
    return out


# ---------- Rückfall und Mock: einfache Grafik aus Farbe, Form und Kurztitel ----------

def fallback_svg(slug: str, topic: str, word: str) -> str:
    a, b = _palette(slug)
    shape = {
        "preise": '<circle cx="880" cy="320" r="150" fill="#fff" fill-opacity=".14"/><circle cx="880" cy="320" r="100" fill="none" stroke="#fff" stroke-width="10" stroke-opacity=".5"/>',
        "sicherheit": '<path d="M880 150 L1010 200 V330 C1010 420 950 480 880 510 C810 480 750 420 750 330 V200 Z" fill="#fff" fill-opacity=".16"/>',
        "technik": '<rect x="790" y="140" width="190" height="340" rx="28" fill="#fff" fill-opacity=".16"/><rect x="810" y="170" width="150" height="260" rx="10" fill="#fff" fill-opacity=".12"/>',
        "gratis": '<polygon points="880,130 920,250 1040,250 945,320 980,440 880,370 780,440 815,320 720,250 840,250" fill="#fff" fill-opacity=".16"/>',
    }.get(topic, '<circle cx="880" cy="300" r="170" fill="#fff" fill-opacity=".12"/><circle cx="960" cy="400" r="90" fill="#fff" fill-opacity=".14"/>')
    word = re.sub(r"[<>&\"']", "", word)[:28]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{a}"/>'
            f'<stop offset="1" stop-color="{b}"/></linearGradient></defs>'
            f'<rect width="{W}" height="{H}" fill="url(#g)"/>{shape}'
            f'<text x="70" y="340" font-size="84" font-weight="800" fill="#fff" font-family="system-ui,Arial,sans-serif">{word}</text></svg>')


# ---------- KI-Erzeugung ----------

IMG_SYSTEM = """Du bist Grafiker für einen Ratgeber. Du zeichnest ein flaches, plakatives Titelbild als SVG.
Regeln:
- Format: viewBox="0 0 1200 630". Nur diese Elemente: svg, g, defs, linearGradient, radialGradient, stop, rect, circle, ellipse, path, polygon, polyline, line, text, tspan.
- Keine Bilder, keine Skripte, keine Schriftarten laden, keine Links, keine externen Verweise, keine Filter.
- Zeige Gegenstände und Symbole zum Thema, keine Menschen, keine Körper oder Körperteile, keine Nacktheit, nichts Sexuelles, keine Marken, Logos oder bekannten Figuren.
- Ein kurzes, plakatives Wort (höchstens 3 Wörter, höchstens 28 Zeichen) als <text> mit font-family="system-ui,Arial,sans-serif" und font-weight="800", gut lesbar, mit viel Kontrast.
- Hintergrund als Verlauf aus den angegebenen Farben. Insgesamt unter 6000 Zeichen.
Antwort genau so, ohne weiteren Text:
ALT: <Bildbeschreibung auf Deutsch, höchstens 110 Zeichen, nüchtern>
<svg ...>...</svg>"""


def _prompt(content: dict, topic: str, slug: str) -> str:
    a, b = _palette(slug)
    return (f"Thema der Seite: {content['h1']}\nThemenbereich: {topic}\n"
            f"Passende Motive: {MOTIFS.get(topic, MOTIFS['grundlagen'])}\n{FASHION_HINT}\n"
            f"Farben: {a} und {b} (Verlauf), Schrift weiß.")


def _call(cfg: dict, user: str) -> str:
    import anthropic  # nur bei Bedarf

    client = anthropic.Anthropic(max_retries=5)
    resp = client.messages.create(model=cfg["llm"]["model"], max_tokens=3500, system=IMG_SYSTEM,
                                  messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if b.type == "text")


def _short_word(content: dict) -> str:
    words = re.findall(r"[\wäöüÄÖÜß-]+", content.get("h1", ""))
    return " ".join(words[:3])[:28] or "Ratgeber"


def make_image(content: dict, topic: str, slug: str, cfg: dict, mock: bool = False) -> dict:
    """Gibt {'alt': ..., 'svg': ...} zurück. Fällt bei Fehlern auf die einfache Grafik zurück."""
    alt = f"Titelbild zum Thema {content.get('h1', '')}"[:110]
    if not mock:
        for _ in range(2):
            try:
                raw = _call(cfg, _prompt(content, topic, slug))
                m = re.search(r"<svg.*</svg>", raw, re.S | re.I)
                clean = sanitize_svg(m.group(0)) if m else None
                if clean:
                    am = re.search(r"^ALT:\s*(.+)$", raw, re.M)
                    return {"alt": (am.group(1).strip()[:110] if am else alt), "svg": clean}
            except Exception as e:
                print(f"  Bild für {slug}: KI-Erzeugung fehlgeschlagen ({type(e).__name__}), Rückfallgrafik.")
                break
    return {"alt": alt, "svg": fallback_svg(slug, topic, _short_word(content))}


# ---------- Vorschaubild (PNG) fürs Teilen ----------

def _font(size: int):
    from PIL import ImageFont

    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def write_og_png(path: Path, title: str, slug: str, site_name: str) -> None:
    from PIL import Image, ImageDraw

    a, b = _palette(slug)
    ca, cb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    ca, cb = tuple(ca), tuple(cb)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(0, W):
            t = (x / W + y / H) / 2
            px[x, y] = tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    d = ImageDraw.Draw(img, "RGBA")
    d.ellipse((780, 90, 1140, 450), fill=(255, 255, 255, 30))
    d.ellipse((900, 330, 1100, 530), fill=(255, 255, 255, 36))
    f = _font(66)
    y = 150
    for line in textwrap.wrap(title, 24)[:4]:
        d.text((70, y), line, font=f, fill=(255, 255, 255, 255))
        y += 84
    d.text((70, H - 80), site_name, font=_font(32), fill=(255, 255, 255, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)


def ensure(content: dict, topic: str, slug: str, cfg: dict, mock: bool = False, force: bool = False) -> bool:
    """Ergänzt content um 'image' (alt, svg), falls noch keines da ist. True, wenn etwas geändert wurde."""
    if content.get("image") and not force:
        return False
    content["image"] = make_image(content, topic, slug, cfg, mock=mock)
    return True


def write_files(site_dir: Path, slug: str, content: dict, site_name: str) -> dict | None:
    """Schreibt SVG und PNG in site/img/. Gibt Pfade fürs Template zurück."""
    img = content.get("image")
    if not img:
        return None
    (site_dir / "img").mkdir(parents=True, exist_ok=True)
    (site_dir / "img" / f"{slug}.svg").write_text(img["svg"], encoding="utf-8")
    png = None
    try:
        write_og_png(site_dir / "img" / f"{slug}.png", content.get("h1", site_name), slug, site_name)
        png = f"/img/{slug}.png"
    except Exception as e:  # Pillow fehlt o. ä.: Seite funktioniert auch ohne Vorschaubild
        print(f"Vorschaubild für {slug} nicht erzeugt: {type(e).__name__}: {e}")
    return {"svg": f"/img/{slug}.svg", "png": png, "alt": img["alt"]}
