# Themenseiten-Pipeline für Visit-X (Affiliate)

Erzeugt täglich neue, eigenständige Ratgeberseiten zu Suchbegriffen, die du vorgibst, veröffentlicht sie
automatisch als statische Seite, reicht die Sitemap bei Google ein und überwacht, ob die Seiten indexiert
werden und Traffic bringen. Läuft ohne eigenen Server über GitHub Actions und Cloudflare Pages.

## So arbeitet die Pipeline

1. **Import:** Keywords aus dem Search-Console-Export (oder einer Textdatei) einlesen. Nur Begriffe aus dem
   erlaubten Vokabular (`config.yaml`, Abschnitt `topics`) kommen durch. Namen, Künstlernamen, Marken Dritter,
   explizite Medienbegriffe und unbekannte Wörter werden aussortiert (`data/rejected.csv` zeigt, warum).
   Zusätzlich prüft Claude die Liste auf Personen und Minderjährige.
2. **Clustering:** Ähnliche Keywords landen auf einer Seite, damit sich deine Seiten nicht gegenseitig Konkurrenz machen.
3. **Generierung:** Claude schreibt pro Cluster einen eigenständigen Artikel (Einleitung, Abschnitte, FAQ).
   Konkrete Angaben über den Anbieter nur aus `facts.md`.
4. **Qualitäts-Gate:** Zu kurz, zu ähnlich zu bestehenden Seiten, erfundene Preise oder Erfahrungsberichte,
   gesperrte Begriffe: die Seite wird verworfen, neu generiert (bis zu 3 Versuche) oder zurückgestellt.
5. **Bauen und Veröffentlichen:** statisches HTML mit Canonical, Schema.org (Article, FAQ, Breadcrumb),
   interner Verlinkung, RTA-Label, Altersabfrage, Werbekennzeichnung, Sitemap, robots.txt. Dann Git-Push,
   Cloudflare deployt automatisch.
6. **Monitoring:** Search Console API liefert Indexierungsstatus, Impressionen, Klicks und Positionen pro Seite
   (`reports/JJJJ-MM-TT.md`). Liegt die Indexierungsquote reifer Seiten unter 30 %, wird die Tagesmenge
   automatisch auf 1 gedrosselt. Themen mit schlechter Quote werden pausiert.

## Einrichtung (einmalig, ca. 30 Minuten)

1. **Repository anlegen:** Projekt in ein neues (privates) GitHub-Repository hochladen.
2. **`config.yaml` ausfüllen:** `site.base_url` (deine Domain), `site.operator` (Name, Anschrift, E-Mail für das
   Impressum, Pflicht in Deutschland), `gsc.property`. Der VX-Cash-Link ist schon eingetragen. Prüfe im Partnerbereich,
   ob und wie eine SubID angehängt werden kann, und ergänze dann `?subid={subid}` (Parametername aus dem Partnerbereich).
3. **`facts.md` pflegen:** Trage nur geprüfte Angaben zu Visit-X ein (Preismodell, Zahlungsarten, Altersprüfung ...).
   Ohne Einträge bleiben die Artikel bewusst allgemein.
4. **Cloudflare Pages:** Repository verbinden, Build-Befehl leer lassen, Ausgabeverzeichnis `site`, Domain zuordnen.
5. **Search Console:** Property für die Domain anlegen. In Google Cloud einen Service-Account mit aktivierter
   "Search Console API" erstellen, den JSON-Schlüssel herunterladen und die E-Mail-Adresse des Service-Accounts in der
   Search Console als Nutzer mit voller Berechtigung eintragen.
6. **GitHub-Secrets:** `ANTHROPIC_API_KEY` und `GSC_SERVICE_ACCOUNT_JSON` (kompletter Inhalt der JSON-Datei).
7. **Keywords importieren** (lokal oder im Codespace): `pip install -r requirements.txt`, dann
   `python -m seo.cli import-keywords pfad/zur/datei.csv` und die entstandene `data/clusters.json` einchecken.
8. **Testlauf:** `python -m seo.cli --mock run` baut eine Probeseite in `.sandbox/site` (ohne API, ohne Veröffentlichung).
9. **Scharf schalten:** Workflow "Tageslauf" in GitHub unter Actions einmal manuell starten, danach läuft er täglich.

## Befehle

| Befehl | Zweck |
|---|---|
| `python -m seo.cli import-keywords DATEI` | Keywords einlesen, filtern, clustern (`--no-llm-screen` überspringt die KI-Vorprüfung) |
| `python -m seo.cli run [--limit N]` | Seiten erzeugen, bauen, veröffentlichen, Sitemap einreichen |
| `python -m seo.cli monitor` | Indexierung und Leistung prüfen, Bericht schreiben, Drosselung bewerten |
| `python -m seo.cli build` | Seite aus den gespeicherten Inhalten neu bauen |
| `python -m seo.cli status` | Stand der Warteschlange und der Seiten |
| `--mock` / `--sandbox` (vor dem Befehl) | Testlauf ohne API bzw. in `.sandbox/` |

## Model-Profilseiten (data/models.yaml)

Zusätzlich zu den Themenseiten erzeugt die Pipeline neutrale Profilseiten für Models, die über VX Cash offiziell
beworben werden (bis zu `models.per_day` neue Seiten pro Tag).

- **Eintrag pro Model** in `data/models.yaml`: Bühnenname, Landingpage-Links mit deiner Kampagne, `facts` und `keywords`.
- **Inhalt:** nur Aussagen aus `facts` (was du auf der offiziellen Landingpage siehst, ohne Zahlen). Keine Angaben zu
  Aussehen, Alter, Wohnort oder Privatleben, keine expliziten Schilderungen, kein Leak- oder Fremdmaterial.
- **Keywords:** Name plus erlaubte Zusatzwörter (`models.keyword_words` in `config.yaml`, z. B. porno, videos, live chat,
  profil, visit x, gratis). Alles andere, etwa einzelne Praktiken, Privatangaben, "nudes", "xxx", "onlyfans", fällt automatisch heraus.
- **Entfernen:** `active: false` setzen oder den Namen in `data/blocked_names.txt` eintragen. Die Seite verschwindet beim nächsten
  Lauf oder mit `python -m seo.cli build`. Danach den Workflow "Tageslauf" einmal starten, damit die Änderung online geht.
- **Kennzeichnung:** Jede Profilseite trägt den Hinweis, dass sie ein unabhängiger Ratgeber ist und nicht vom Model oder
  vom Anbieter betrieben wird, dazu die Werbekennzeichnung.
- Namen und Landingpages stimmst du bitte mit den Bedingungen von VX Cash und, wo nötig, mit den Models ab.

## Menge hochfahren

Starte mit 5 Seiten pro Tag (`limits.daily_pages`). Erhöhe erst, wenn der Bericht nach 2 bis 3 Wochen zeigt,
dass die Mehrzahl der Seiten indexiert wird. Neue Domains werden von Google vorsichtig behandelt, und Masse ohne
Nutzen führt zu Abwertung der ganzen Domain. `limits.max_daily_pages` (30) ist die harte Obergrenze.

## Wichtige Hinweise

- **Keine Garantie für Rankings.** Die Pipeline schafft gute Voraussetzungen, Google entscheidet. Ratgeber mit echtem
  Nutzen werden eher indexiert als dünne Seiten. Das Qualitäts-Gate verwirft deshalb lieber, als Schwaches zu veröffentlichen.
- **Rechtliches prüfen lassen:** Impressum und Datenschutzerklärung sind Grundtexte aus deinen Angaben. Lass sie, ebenso
  die Altersabfrage nach JMStV, einmal von einer Fachperson prüfen. Die Altersabfrage der Seiten ist ein Hinweis und
  keine Altersverifikation im Sinne des Jugendmedienschutzes.
- **Programmbedingungen:** Prüfe die Bedingungen von VX Cash zu SEO-Traffic, Affiliate-Seiten und Kennzeichnung.
- **Namen als Keywords:** Der Filter lässt Personen- und Künstlernamen bewusst nicht durch. Eigene Sperrbegriffe kannst du
  in `data/blocked_names.txt` (eine Zeile pro Begriff) ergänzen.
- **Indexing API:** Wird bewusst nicht benutzt, weil Google sie nur für Stellenanzeigen und Livestreams freigibt.

## Keyword-Kreislauf (Search Console)

Der Tageslauf (`run`) holt vor der Seitenerzeugung die Suchanfragen der letzten 28 Tage aus der Search Console
und übernimmt neue in die Warteschlange. Sie durchlaufen dieselben Filter wie jeder Import: Wortliste, Sperrliste,
KI-Vorprüfung (Namen, Minderjährige, Leaks). Bekannte und abgelehnte Anfragen werden nicht erneut geprüft.
Ähnliche Anfragen werden bestehenden Seiten zugeordnet. Abgelehnte stehen in `data/rejected.csv`.
Einstellungen: `keywords.harvest` in `config.yaml` (`enabled: false` schaltet den Kreislauf ab).
Manuell: `python -m seo.cli harvest`.

## Bilder (automatisch pro Seite)

Zu jeder neuen Seite erzeugt die KI eine plakative Vektorgrafik (SVG) zum Thema: Gegenstände, Symbole, kurze Schrift,
auf Wunsch auch Stiefel oder High Heels als flache Silhouette. Keine Personen, keine Körper, keine Fotos, keine Marken.
Das SVG wird streng gefiltert (nur erlaubte Elemente, keine Skripte oder Links). Klappt die KI-Erzeugung nicht, entsteht
eine einfache Grafik aus Farbe, Form und Titel. Dazu kommt ein Vorschaubild (PNG, 1200x630) fürs Teilen.
Bestehende Seiten nachziehen: `python -m seo.cli images` (mit `--force` neu erzeugen).
Werbematerial von VX Cash wird nicht verändert und nicht automatisch geholt; es kommt als Datei von dir.

## Optimierer

Im Tageslauf (`run`, nach der Auswertung) wertet der Optimierer Position, Klickrate und Suchanfragen je Seite aus
(Einstellungen: `optimize` in `config.yaml`):

- **title_ctr:** Position <= 10, aber kaum Klicks: Titel, Meta-Beschreibung und H1 neu.
- **striking:** Position 8 bis 30: bis zu 2 Abschnitte und 2 FAQ zu den Suchanfragen ergänzen, die noch nicht abgedeckt sind.
- **no_traction:** indexiert, aber nach 60 Tagen ohne Impressionen: einmal mit anderem Blickwinkel neu schreiben.
- **not_indexed:** nur im Bericht.

Schutz: Mindestalter 21 Tage, 28 Tage Pause je Seite, höchstens 3 Seiten pro Tag, jede Änderung geht durch das
Qualitäts-Gate (Model-Seiten zusätzlich durch Sperrbegriffe und facts), die alte Fassung liegt in `data/history/`.
Nach 21 Tagen wird verglichen. Ist die Position um 4 Plätze oder mehr schlechter, stellt der Optimierer die alte Fassung
wieder her. Alles steht im Tagesbericht unter "Optimierung". Manuell: `python -m seo.cli optimize`.

## Nischen-Keywords (Genres, ohne Namen)

`python scripts/build_keywords.py` erzeugt `examples/keywords_nischen.csv`: Genre-Wörter der Amateur-Erotik
(deutsch, amateur, reife, milf, paar, swinger ... mal Kernbegriffe wie porno, nackt, anal, creampie, joi ...,
mit Zusätzen wie kostenlos, videos, live, cam). Die Zahlen sind eine Rangfolge-Schätzung, keine Suchvolumen.
Import: `python -m seo.cli import-keywords examples/keywords_nischen.csv`. Ähnliche Begriffe (Synonyme wie porn/porno,
deutsche/deutsch) landen auf einer Seite. Thema: "nischen" (eigene Überschrift, eigener Hinweis an das Modell).

Grenzen im Code: gesperrt sind Bezüge zu Minderjährigen, fehlender Einwilligung, Leaks, Fälschungen, versteckten
Kameras, Familie/Inzest, Gewalt, Tieren und Extremem (Liste `blocked_terms`, Treffer am Wortanfang). Namen von Personen
und Fremdmarken sind nicht in der Wortliste und fallen automatisch heraus. Die Artikel bleiben nicht-explizit: ein
Filter lehnt Vulgärsprache ab (`quality.EXPLICIT`). Sie erklären das Genre, nennen legale Angebote und Alternativen.

## Prüfung und Wachstum (Stand Audit)

- **Blickwinkel:** Jede Seite bekommt einen festen Schwerpunkt (Kosten, Checkliste, Einsteiger, Sicherheit, Vergleich, Fehler,
  Ablauf, Mythen, Fragen, Recht). Scheitert eine Seite am Ähnlichkeitsfilter, wird sie beim nächsten Versuch mit einem anderen
  Blickwinkel geschrieben, erst nach drei Fehlversuchen gilt sie als gescheitert. Doppelte Titel werden abgelehnt.
- **Themen-Hubs:** `/themen/<thema>/` listet alle Ratgeber eines Themas, die Startseite zeigt die neuesten und verlinkt dorthin.
  Die Brotkrumen führen zum Hub (stärkt die interne Verlinkung).
- **Button:** direkt nach der Einleitung und am Ende, Beschriftung je Thema (`affiliate.topic_cta`).
- **Tagesmenge wächst automatisch:** Ist die Indexierungsquote reifer Seiten >= 60 %, steigt die Menge alle 7 Tage um 5 bis zur
  Obergrenze (`monitor.ramp`). Bei Drosselung wächst nichts.
- **IndexNow:** neue und geänderte URLs gehen nach dem Deploy an Bing, Yandex u. a. (`indexnow` in `config.yaml`).
- **Optimierer und Tageslauf** laden Seiten vor dem Speichern neu, damit sich beide nicht überschreiben.

## Österreich und Schweiz, Kaufabsicht zuerst

- Thema `dach` (`examples/keywords_dach.txt`): Seiten zu Visit-X, Live-Cams und Webcam-Chat in Österreich und der Schweiz (Zahlung,
  Anmeldung, Rechtslage, Jugendschutz), ohne erfundene Angaben zu Zahlungsarten oder Preisen.
- Warteschlange: Seiten mit Marke + Frage (Visit-X kosten, kündigen, seriös, Erfahrungen ...) und DACH-Seiten kommen zuerst
  (`keywords.priority` in `config.yaml`), danach die Genre-Seiten.
- Cluster werden nur noch innerhalb desselben Themas zusammengelegt.
- KI-Antworten kommen als Werkzeugaufruf mit festem Schema (`generate.ask_json`). Das behebt die JSON-Fehler des ersten Laufs
  (abgeschnittene oder fehlerhafte Antworten), `max_tokens` ist auf 9000 erhöht.
