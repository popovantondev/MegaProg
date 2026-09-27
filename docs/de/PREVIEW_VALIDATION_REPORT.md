# MegaProg 4.5.1 Preview — Prüfung

## Finaler Kandidat, 27. September 2026

- Abschließende Offline-Suite: 556 Tests, 553 bestanden, 3 übersprungen. Der öffentliche Quellstand und die gepackte App bestanden den Source-Release-Check, die strenge macOS-Signaturprüfung, die CLI-Prüfung und den Fensterstart mit leerem `HOME` im aktuellen Benutzerkonto.
- Die App aus dem genauen Paket führte das echte Übungsprojekt aus: Beide Rechenfunktionen erhielten `VERIFIED`, beide Aufgaben `COMPLETED` nach je einem Versuch. Der gespeicherte Plan zeigt 2/2 Arbeitsrunden. Nach der Pause wurde die erste Aufgabe nicht wiederholt.
- Gespeicherte Nutzungswerte dieses Laufs: 115.471 Eingabetoken, davon 104.448 zwischengespeichert; 789 Ausgabetoken, davon 82 Reasoning-Token. Ein Vergleich mit direkter Codex-Nutzung wurde nicht durchgeführt.
- Sechs Sprachbilder von Plan und Ergebnis liegen in `docs/screenshots/`. Sie nutzen den gespeicherten Abschluss mit übersetztem Beispieltext und Platzhalterpfad. Private Pfade und Nutzungswerte sind nicht in den Bildern enthalten.
- Diese Prüfungen erfolgten lokal. Ein separates macOS-Konto, ein zweiter Mac und GitHub Actions wurden noch nicht geprüft.

Die älteren Messwerte darunter dokumentieren frühere 4.5.1-Zwischenstände.

## Bestätigt auf macOS 15.7.4 / Apple Silicon

- Offline-Suite: 547 Tests, 544 bestanden, 3 übersprungen; 243,315 Sekunden.
- 31 Assistententests: geänderte Pläne während der Bestätigung, Import der
  geprüften Daten, Anhalten/Schließen, Planauswahl, Sprachwechsel und
  Bedienelemente bei minimaler Breite und 125% Schriftgröße.
- Zwei Aufgaben über Fenster und getrennte CLI-Prozesse mit einem simulierten
  Modell: Fenster dazwischen geschlossen und neu geöffnet, erste Aufgabe nicht
  wiederholt, beide Funktionen geprüft, Kontextpaket exportiert. Keine echten Modellanfragen.
- Basis 4.5.0: 84 synthetische Aufnahmen: RU/DE/EN × hell/dunkel × 1120×780/860×620 ×
  fünf Seiten und blockiert/angehalten. Repräsentative Aufnahmen visuell geprüft.
- Deutsche und russische Qt-Dialogübersetzungen werden geladen.
- Unveränderte Kachelgeometrie; Messung aus 4.5.0: MegaProg 81,25%, Finder und Safari 80,47% der Fläche
  entlang jeder Achse. Größenunterschied etwa 0,97%, innerhalb der 5%-Vorgabe.
- Qt-Oberfläche; approved-plan-Schema v1 und Ausführungskern unverändert.

Prüfung: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v`.
Vorschau: `QT_QPA_PLATFORM=offscreen python packaging/preview_gui.py --output /tmp/megaprog-preview`.

## Ergänzung 4.5.1

- Nach den letzten Text-/Layoutänderungen: 39 Oberflächenprüfungen bestanden, 96,946 Sekunden.
- Eigene Meldungen für ZIP-Quellordner, Unterordner, fehlenden Ordner/Git und Zugriffsfehler. Der gewählte Pfad bleibt sichtbar; ein ungültiger Wechsel verwirft den vorherigen Plan. Der gewählte Ordner wird nicht verändert.
- Gegenüberliegende Symbolknoten stimmen überein: kleinere mintfarbene und größere türkise Knoten.
- Russische Ordnerfehlermeldung im hellen Design bei 860×620 visuell geprüft. Die obigen 84 Aufnahmen stammen aus 4.5.0.
- Ein Übungsplan mit zwei Funktionen und einem Budget von zwei Runden wurde vorbereitet. Vorbereitung und lokale Verbindungsprüfung starten keine Modellaufgaben.
- Auf dem korrigierten 4.5.1-Kandidaten (`89c2eb3`) wurde ein separater echter Codex-Lauf durchgeführt: Beide Übungsfunktionen wurden in genau zwei Worker-Runden abgeschlossen. Nach der ersten Aufgabe wurde die App angehalten, geschlossen und wieder geöffnet; beim Fortsetzen desselben Plans wurde die erste Aufgabe nicht wiederholt. Die Prüfungen beider Funktionen bestanden. Codex meldete 115.371 Eingabetoken (davon 106.496 aus dem Cache) und 594 Ausgabetoken; Laufzeit 1.559,497 Sekunden. Diese Werte beschreiben nur diesen Lauf und keinen Vergleich mit direkter Codex-Nutzung.

## Paketabnahme

Build, Signatur, Prüfsummen, Quellmanifest und Cocoa-Oberfläche des genauen
Pakets werden im separaten Kandidatenbericht festgehalten. Einheitstests
oder der echte Lauf auf dem korrigierten Kandidaten belegen diese Schritte
nicht automatisch.

Ein separates frisches macOS-Konto, ein zweiter Mac, entfernte GitHub Actions,
Windows und Intel sind ungeprüft. Kein Developer ID und keine Notarisierung.
Keine Behauptung nachgewiesener Codex-Kontingenteinsparung.
