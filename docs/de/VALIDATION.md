# Vorschau validieren

Der Bericht nennt Commit, Build-Rechner, macOS-Version, Architektur, SHA-256 der Pakete und Testergebnisse. Offline-Tests und Prüfungen mit Codex-Anmeldung getrennt angeben. Keine Nutzungseinsparung ohne vergleichbare Messungen behaupten.

Vor einer Vorschauveröffentlichung:

- Offline-Tests ausführen und Fehler untersuchen; CI darf keine kostenpflichtige API aufrufen.
- App und Quellcode-Archiv aus demselben sauberen Commit erstellen.
- Manifest, Lizenz, Sprachdokumente und Datenschutzinhalt des Archivs prüfen.
- Ersten Start und Planimport in einem sauberen Apple-Silicon-Benutzerkonto testen.
- Zwei kleine Aufgaben mit höchstens zwei Worker-Runden testen: zwischen Aufgaben anhalten, App neu starten und bestätigen, dass die erste Aufgabe nicht erneut ausgeführt wurde.
- Nicht verfügbare Nutzungs- oder Umgebungsdaten als unbekannt markieren.
- Windows und Intel-Macs erst nach tatsächlichem Test als geprüft kennzeichnen.
