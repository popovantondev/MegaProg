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
