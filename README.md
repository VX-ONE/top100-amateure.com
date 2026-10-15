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
