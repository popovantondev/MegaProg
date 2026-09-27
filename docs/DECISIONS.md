# Product decisions

- Audience: people new to coding agents.
- First supported release target: Apple Silicon macOS. Other platforms are not declared verified.
- The user prepares and approves the plan in ChatGPT, then imports the JSON plan into MegaProg.
- First public release is a preview with an ad-hoc signature, no Apple Developer ID signature, and no notarization. macOS security remains enabled.
- License: GPL-3.0-only.
- User interface and documentation languages: Russian, German, and English.
- No claim of guaranteed quota savings or error-free execution.
- Do not publish credentials, private project state, personal handoffs, or local machine paths.

- Desktop redesign: PySide6 6.10.2 Widgets, a five-step wizard, system/light/dark themes, centred node/check icon with restrained depth. Existing approved-plan schema v1 and execution rules stay compatible.

- 4.5.1: opposite icon nodes share color and size; the mint diagonal is smaller and the turquoise diagonal is larger. Opening source ZIPs does not create Git history; project errors must distinguish this from access problems and show the selected path.
