# MegaProg 4.5.1 Preview — validation

## Final release candidate, 2026-09-27

- Final offline suite: 556 tests run, 553 passed, 3 skipped. The public source snapshot and its packaged app passed source release-check, strict macOS signature verification, CLI help and window startup with a fresh `HOME` in the current user account.
- The exact packaged app was used for a real tutorial run: both calculator functions reached `VERIFIED`; both tasks reached `COMPLETED` after one attempt each. The saved plan reports 2/2 worker turns. The first task remained complete when work continued after the pause.
- Saved usage for that run: 115,471 input tokens, including 104,448 cached input tokens; 789 output tokens, including 82 reasoning tokens. No direct-Codex comparison was performed.
- Six localized screenshots of the tutorial plan and completed state are in `docs/screenshots/`. They use the saved completed state with translated demonstration text and a placeholder path. Private paths and usage figures are omitted from the images.
- The source tree is public; these results were obtained locally. A separate macOS account, a second Mac and remote GitHub Actions have not been checked.

The older measurements below describe earlier 4.5.1 checkpoints and are retained as history.

## Confirmed on macOS 15.7.4 / Apple Silicon

- Offline suite: 547 tests run, 544 passed, 3 skipped; 243.315 seconds.
- Desktop regression group: 31 wizard cases, including source changes during
  confirmation, immutable import, stop/close lifecycle, explicit plan selection,
  language changes and minimum-width controls at 125% text size.
- Two-task GUI/CLI scenario: passed with a synthetic executor in separate CLI
  processes. Close/reopen between tasks, no repeat of the first task, both
  features verified, portable handoff exported. No real model requests.
- Previous 4.5.0 baseline: 84 synthetic desktop captures: RU/DE/EN × light/dark × 1120×780/860×620 ×
  five pages plus blocked/stopped states. Representative captures visually reviewed.
- Qt's Russian and German standard-dialog translations load successfully.
- Unchanged tile geometry; 4.5.0 baseline icon opaque bounds: MegaProg 81.25% of canvas; Finder and Safari 80.47%.
  The visible tile is about 0.97% larger, within the requested 5% tolerance.
- New application window uses Qt; approved-plan schema v1 and engine remain unchanged.

Commands: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v`;
`QT_QPA_PLATFORM=offscreen python packaging/preview_gui.py --output /tmp/megaprog-preview`.

## 4.5.1 follow-up

- Final desktop regression rerun after wording/layout adjustments: 39 tests passed in 96.946 seconds.
- Plain source ZIP, nested folder, missing folder/Git and Git permission errors have separate messages; the attempted path is retained. A failed selection clears the previous plan. No selected folder is initialized or modified.
- The two diagonals now have matching opposite-node colors/sizes: smaller mint and larger turquoise.
- The Russian light-theme folder error was visually checked at 860×620. The full 84-capture matrix above is historical, not a fresh 4.5.1 measurement.
- The prepared two-function tutorial has a two-turn budget. Preparation and local connection checks do not execute its model tasks.
- On the corrected 4.5.1 candidate (`89c2eb3`), a separate live Codex run completed both tutorial functions in exactly two worker turns. The app paused after the first task, was closed and reopened, and resumed the same saved plan without repeating that task. Both functions passed their checks. Codex reported 115,371 input tokens (106,496 cached) and 594 output tokens; elapsed time was 1,559.497 seconds. These figures describe this run, not a comparison with direct Codex use.

## Package acceptance

The exact archive's build, signature, checksum, source manifest and Cocoa GUI
results belong in the candidate's separate review receipt. Do not infer
those results from unit tests or from the live run on the corrected candidate.

A separate clean macOS user account, a second Mac, remote GitHub Actions,
Windows and Intel remain unverified. Developer ID and notarization are not
included. No claim of proven Codex quota savings is made.
