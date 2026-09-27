# macOS-Preview erstellen

Der Builder zielt auf Apple Silicon. Die festgelegte PyInstaller-Version bündelt Oberfläche, Python und Qt. Ein separater ZIP enthält die erlaubten Quellen, Tests, Lizenztexte der Laufzeitkomponenten, GPL-3.0-only und ein Prüfsummenmanifest. `SHA256SUMS.txt` enthält Hashes für beide Archive. Der fertige App-Bundle wird ad hoc signiert und geprüft; er erhält keine Apple-Developer-ID-Signatur und wird nicht notariert, installiert oder hochgeladen.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-build.txt
git status --short
python packaging/build.py --output /tmp/megaprog-release
```

Der Ausgabeordner muss leer sein. Der Build benötigt einen sauberen Git-Checkout auf Apple Silicon. App und Quellcode müssen aus demselben Commit stammen. Vor dem Release Prüfsummen kontrollieren, Quellen prüfen, App in einem sauberen Benutzerkonto starten und den begrenzten Live-Test aus [VALIDATION.md](VALIDATION.md) abschließen.

Fügen Sie `MegaProg-4.5.1-third-party-source.zip` mit geprüften Kopien der Qt- und PySide6-Quellen bei. Die enthaltenen Quellen stehen in [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md); Links zu Qt allein ersetzen dieses Archiv nicht.
Die Prüfsummendatei des Builders deckt seine zwei Archive ab. Erstellen Sie im endgültigen Release-Ordner `SHA256SUMS.txt` für alle drei Archive neu.

Die Vorschau ist ad hoc signiert, hat aber keine Apple-Developer-ID-Signatur und ist nicht notarisiert. Erklären Sie die app-spezifische Option **Trotzdem öffnen** nur Nutzern, die dem Build vertrauen. Gatekeeper nicht global deaktivieren.

Keine privaten Handoffs, Projektspeicher, Laufprotokolle, Zugangsdaten, persönlichen Projektdaten, alten Archive oder lokale Rechnerkonfiguration aufnehmen. Vor der Veröffentlichung muss ein Mensch die konkreten Dateien und den Release-Text prüfen.
