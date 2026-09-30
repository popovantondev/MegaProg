# MegaProg

[Benutzerhandbuch](https://popovantondev.github.io/MegaProg/Guide-de.html)

**[Für macOS herunterladen](https://github.com/popovantondev/MegaProg/releases) · [Deutsch](README.de.md) · [Русский](README.ru.md) · [English](README.md)**

**Apple Silicon · 4.5.1 Preview (Vorschauversion) · GPL-3.0-only**

<p align="center"><img src="assets/megaprog.svg" width="112" alt="MegaProg"><br><strong>Plan freigeben. Ausführen. Fortschritt behalten.</strong></p>

MegaProg ist eine macOS-App, die freigegebene Entwicklungspläne über den verbundenen Ausführungsdienst ausführt. Sie speichert Anforderungen und Fortschritt im Projekt, bearbeitet Aufgaben nacheinander und führt die Prüfungen aus dem Plan aus.

## Demo: zwei Rechenfunktionen

<a href="docs/screenshots/de-plan.png"><img src="docs/screenshots/de-plan.png" width="100%" alt="Planprüfung auf Deutsch"></a>

<a href="docs/screenshots/de-result.png"><img src="docs/screenshots/de-result.png" width="100%" alt="Geprüftes Ergebnis auf Deutsch"></a>

Die Bilder zeigen den Übungsplan und seinen Abschluss nach zwei echten Aufgabenläufen. Der Text wurde für die Anzeige übersetzt, der Projektpfad durch einen Platzhalter ersetzt und private Daten sowie Nutzungswerte entfernt. [Russische](README.ru.md#пример-две-функции-калькулятора) und [englische](README.md#demo-two-calculator-functions) Bilder stehen in den jeweiligen Anleitungen.

Bereiten Sie einen JSON-Plan mit Ziel, erlaubten Dateien, Abnahmekriterien und Prüfkommandos vor. Prüfen Sie ihn vor dem Öffnen und Freigeben in MegaProg. Prüfen Sie Projektinformationen vor der Weitergabe an einen externen Dienst.

## Was MegaProg macht

- Speichert Anforderungen und Aufgaben außerhalb des Chatverlaufs.
- Führt freigegebene Aufgaben nacheinander innerhalb eines gemeinsamen Ausführungslimits aus.
- Führt die Prüfungen aus dem Plan aus und speichert deren Ergebnisse.
- Speichert den Fortschritt zum Pausieren und Fortsetzen.
- Zeigt Fortschritt, verfügbare Nutzungsdaten und den Grund für einen Stopp.

MegaProg garantiert weder, dass alle Anforderungen erfasst wurden, noch fehlerfreien Code oder einen geringeren Dienstverbrauch als die direkte Ausführung. Prüfen Sie Plan, Befehle, Änderungen und Ergebnisse.

## Erste Schritte auf macOS

Das Preview-Paket ist für **Apple-Silicon-Macs** vorgesehen. Die App ist ad hoc signiert, hat aber keine Apple-Developer-ID-Signatur und ist nicht notariell beglaubigt. macOS kann vor einem unbekannten Entwickler warnen. Öffnen Sie die App nur, wenn Sie Quellcode und Prüfsumme des Releases geprüft haben. Wenn Sie fortfahren möchten, bietet macOS nach dem ersten blockierten Start unter „Systemeinstellungen“ → „Datenschutz & Sicherheit“ die Option **Trotzdem öffnen** für diese App an. Gatekeeper nicht global deaktivieren. [Anleitung von Apple](https://support.apple.com/en-us/102445).

1. Laden Sie `MegaProg-4.5.1-macos-arm64.zip` aus den [Releases](https://github.com/popovantondev/MegaProg/releases) herunter.
2. Entpacken Sie das Archiv und verschieben Sie `MegaProg.app` in den Ordner „Programme“.
3. Öffnen Sie MegaProg. Mit „Mit Beispiel ausprobieren“ erstellen Sie ohne Terminal ein neues Übungsprojekt; alternativ wählen Sie ein bestehendes Git-Projekt. Klicken Sie dann auf „Verbindung prüfen“.
4. Verbinden Sie den unterstützten Ausführungsdienst und melden Sie sich mit Ihrem Abonnement an. API-Key-Umgebungsvariablen werden entfernt; es gibt keinen Wechsel zu einer separat abgerechneten API.
5. Bereiten Sie die für den Plan benötigten Projektinformationen vor. Prüfen Sie Dateien vor der Weitergabe an einen externen Dienst.
6. Legen Sie Ziel, Abnahmekriterien, Dateien, Prüfungen und Ausführungslimit fest. Prüfen Sie den Plan und speichern Sie ihn als reines JSON.
7. Öffnen Sie die JSON-Datei in MegaProg. Prüfen Sie Funktionen, erlaubte Dateien und jeden Prüfungsbefehl. Das Öffnen startet nichts.
8. Klicken Sie auf „Freigeben und starten“ oder „Nur speichern“. Mit „Eine Aufgabe, dann Pause“ pausieren Sie zwischen Aufgaben. Zum Fortsetzen öffnen Sie MegaProg erneut und wählen den gespeicherten Plan.

Prüfungsbefehle sind ausführbare Programme. Sie laufen auf Ihrem Mac mit Ihrem Benutzerkonto. Geben Sie nur Pläne und Befehle aus Quellen frei, denen Sie vertrauen. Erstellen Sie ein Git-Backup und prüfen Sie Änderungen, bevor Sie sie verwenden oder veröffentlichen.

„Mit Beispiel ausprobieren“ erstellt einen neuen Git-Projektordner und öffnet den enthaltenen Plan für zwei Funktionen mit einem Limit von zwei Modellrunden. Bestehende Ordner werden nicht überschrieben. Die Ausführung startet erst nach Ihrer Freigabe. [Plan](examples/two-features/approved-plan.json) und [Beispielquellen](examples/two-features/project/README.md) liegen auch im Repository.

## Voraussetzungen

- macOS auf Apple Silicon für das Preview-Paket. Andere macOS-Versionen wurden noch nicht unabhängig geprüft.
- Ein separat installierter Ausführungsanschluss mit aktiver Abonnement-Anmeldung. Die Einrichtung steht in der technischen Dokumentation zum freigegebenen Plan.
- Ein Git-Projekt. Pläne und Laufdaten werden in `.ai-dev/` im Projekt gespeichert.

Quellcode und Tests laufen außerdem mit Python 3.9 oder neuer. Für den App-Build benötigen Sie Python 3.12 mit PySide6 sowie die Werkzeuge aus `requirements-build.txt`.

## Aus dem Quellcode bauen

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-build.txt
python packaging/build.py --output /tmp/megaprog-release
```

Der Build erfordert einen sauberen Git-Checkout und speichert App, Quellcode, Prüfsummen und ZIP im gewählten Ordner. Siehe [Build und Release](docs/RELEASE.md).

## Datenschutz und Grenzen

MegaProg läuft lokal. Pläne, Status, Prüfprotokolle und Ausführungssitzungen liegen unter `.ai-dev/` im ausgewählten Projekt. Prüfen oder löschen Sie diese Daten bei Bedarf. Dieses Repository enthält keine Zugangsdaten. Die Ausführung nutzt den verbundenen externen Dienst und das Konto und den Workspace, bei dem Sie angemeldet sind. Ein Handoff kann lokale Pfade und Aufgabenbeschreibungen enthalten: Prüfen Sie ihn vor dem Anhängen an einen anderen Chat.

Informationen zur Mitarbeit: [CONTRIBUTING.md](CONTRIBUTING.md). Sicherheit: [SECURITY.md](SECURITY.md). Lizenz: [GPL-3.0-only](LICENSE). MegaProg ist ein unabhängiges Community-Projekt.

## Neuer Assistent

Projekt → Verbindung → Plan → Ausführung → Ergebnis. Nach der Prüfung «Freigeben und starten» oder «Nur speichern» wählen. Gespeicherte Pläne werden im ersten Schritt ausdrücklich ausgewählt. System-, helles und dunkles Design sowie vollständige deutsche, russische und englische Oberfläche. Das Paket bei Bedarf manuell an den verwendeten Planungsdienst übergeben. Aus Quellcode: `python -m pip install ".[gui]"`.
