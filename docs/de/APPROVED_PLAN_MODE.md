# Freigegebenen Plan ausführen

MegaProg übernimmt eine JSON-Datei aus dem Chat, die der Nutzer geprüft und freigegeben hat. Ein Chat kann lokale Dateien nicht sehen, solange sie nicht angehängt oder relevante Angaben eingefügt werden. MegaProg prüft die Planstruktur vor dem Start.

## Format

Der Plan verwendet `schema_version: 1`, eine eindeutige `plan_id`, `objective`, `budget.max_model_turns`, `features` und `tasks`. Jede Funktion enthält Abnahmekriterien und Prüfkommandos. Jede Aufgabe enthält Funktion, Anweisungen, Abhängigkeiten, erlaubte relative Pfade, Prüfkommandos und optional Kontextdateien. Luna und Sol werden unterstützt; Standard ist Luna medium. Unbekannte Abhängigkeiten und Zyklen werden abgelehnt.

## Ablauf

1. Öffnen Sie die Datei und prüfen Sie Funktionen, Pfade, Budget und jedes Prüfkommando. Das Öffnen startet keinen Befehl.
2. Wählen Sie „Freigeben und speichern“.
3. Starten Sie den gesamten Plan oder führen Sie eine Aufgabe aus und halten Sie danach an.
4. Öffnen Sie nach einem Neustart dasselbe Git-Projekt und setzen Sie den gespeicherten Plan fort.
5. Prüfen Sie Testergebnisse, Berichte und Änderungen in Git.

Prüfungen sind echte Programme. Sie laufen mit Ihrem Benutzerkonto. Geben Sie nur Befehle frei, deren Zweck Sie verstehen und denen Sie vertrauen.

## Speicherung und Grenzen

Das unveränderliche JSON liegt unter `.ai-dev/approved-plans/PLAN_ID/plan.json`; Status, Protokolle und Nachweise werden daneben gespeichert. Eine Funktion gilt als geprüft, wenn ihre Aufgaben erfolgreich geprüft und frühere Funktionen erneut geprüft wurden. Modellrundenbudget, vorhandenes Versuchslimit, Sandbox und Bestätigungen bleiben aktiv. Die Ausführung ist sequenziell; Installation und Veröffentlichung erfolgen nicht automatisch.
