# Third-party notices

MegaProg is GPL-3.0-only. Runtime license texts are distributed under
`Contents/Resources/LICENSES/`; the same texts are in the source archive's
`licenses/` folder. No third-party ownership is claimed by MegaProg.

| Component | Version | License / corresponding source |
|---|---|---|
| CPython | 3.12.14 | PSF; `CPython-3.12.14-LICENSE.txt`; https://www.python.org/downloads/source/ |
| PySide6 and Shiboken6 | 6.10.2 | GPL-3.0-only option; GPLv3 text in MegaProg's `LICENSE`; corresponding source in the separate third-party source archive |
| Qt Core, DBus, Gui, Network, OpenGL, Svg, Widgets and packaged plugins | 6.10.2 | Applicable Qt GPLv3/LGPLv3 terms and third-party notices below; corresponding source in the separate third-party source archive |
| PyInstaller bootloader | 6.22.2 | GPL-2.0-or-later with bootloader exception; `PyInstaller-6.22.2-COPYING.txt` |
| PyInstaller runtime hooks | 6.22.2 | Apache-2.0; included in PyInstaller's `COPYING.txt` |

The Qt libraries are dynamically packaged without local source modifications.
The matching `MegaProg-4.5.1-third-party-source.zip` release asset contains
unmodified Qt 6.10.2 source archives for qtbase, qtsvg, qtimageformats and
qttranslations, plus PySide6/Shiboken6 source. Its `upstream-sources.json`
records official Qt download URLs and verified SHA-256 hashes. Distribute this
asset alongside the macOS app; an upstream link alone is insufficient as a
source offer. Original license files for PySide6/Shiboken6 and Qt 6.10.2
qtbase, qtsvg, qtimageformats and qttranslations accompany the app.
`Qt-6.10.2-third-party-attributions.json` preserves 88 upstream
attribution records, source URLs and the full referenced license texts. This
superset includes platform-specific components that may not be present in the
macOS binary. The app is not distributed under a commercial Qt license.

Tcl/Tk license texts are retained for the source tree's legacy interface and
its compatibility tests; the new Qt application does not use the old window.
Build dependencies are pinned in `requirements-build.txt`; they are not all
runtime components. Original assets in `assets/` are part of MegaProg and
covered by its GPL-3.0-only license.
