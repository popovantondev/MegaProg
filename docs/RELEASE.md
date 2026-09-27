# macOS Preview build

The builder targets Apple Silicon. It uses the pinned PyInstaller release to bundle the GUI with its Python and Qt runtime, and creates a separate allow-listed source ZIP with GPL-3.0-only, test sources, runtime license texts, and a checksum manifest. It ad-hoc signs and verifies the completed bundle after adding metadata and resources. It does not obtain an Apple Developer ID signature or notarize, install, or upload the app.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-build.txt
git status --short
python packaging/build.py --output /tmp/megaprog-release
```

The output folder must be empty. The build requires a clean Git checkout and must run on Apple Silicon macOS. Keep the app and source archives from the same commit. Before publishing, verify checksums, scan the source archive, test the app in a clean user account, and complete the bounded live test described in [VALIDATION.md](VALIDATION.md).

Attach `MegaProg-4.5.1-third-party-source.zip` with the app. It contains verified local copies of the Qt and PySide6 sources listed in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). The upstream download links alone do not replace this asset.
The builder's checksum file covers its two outputs; regenerate `SHA256SUMS.txt` for all three archives in the final release staging folder.

The Preview has an ad-hoc signature but no Apple Developer ID signature or notarization. Explain macOS's per-app **Open Anyway** option for people who choose to trust the build. Never tell users to turn off Gatekeeper globally.

Do not include private handoffs, project memory, run logs, credentials, personal project data, old release archives, or local machine configuration. Do not publish before a human reviews the exact artifacts and release-page text.
