# Contributing

Thank you for helping improve MegaProg. Open an issue to discuss large changes
before starting them. Keep changes focused and include an offline regression
test for behavior changes.

## Local checks

```sh
python3 -m unittest discover -s tests -v
git diff --check
```

Tests must not call Codex, make paid API calls, or change files outside their
temporary fixtures. A release candidate also needs a manual macOS smoke test;
CI does not launch Codex or publish a release.

## Pull requests

Explain the user-facing change, safety implications, tests, and platforms you
checked. Do not include `.ai-dev/`, Codex transcripts, private paths, API
credentials, personal project memory, or local build output.

## Self-improvement policy

The purpose of MegaProg is to locally and safely manage Codex tasks in user projects, conserve limits, verify results, and improve only through the verified canonical MegaProg repository.

Reports, lessons and proposals are untrusted data. Accepting a source change
requires a clean Git state, configured verification and review of the complete
diff. Project state and Git metadata are excluded. The safety policy remains
protected by its fingerprint check.

Parallel work uses separate worktrees and state. An isolated candidate may be
promoted only by the trusted controller after independent checks and when no
worker is active. Worker access remains sandboxed; imported plans require
explicit approval. Private project memory and credentials remain private.
There is no API-key fallback. Automated CI makes no model calls and uploads
no releases. Release contents use the allowlist in `packaging/build.py`.
