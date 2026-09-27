# Validation for the public Preview

A release report must identify the exact commit, build host, macOS version, architecture, package SHA-256, and test results. Distinguish automated offline tests from tests requiring a live Codex login. Do not include a usage-savings claim without a comparable measured baseline.

Before a Preview release:

- Run the offline test suite and inspect failures; never connect paid APIs in CI.
- Build the app and source archive from the same clean commit.
- Verify the source archive manifest, license, three language guides, and privacy scan.
- Test first launch and plan import on a clean Apple Silicon account.
- Run the bounded two-task demonstration with a maximum of two worker turns; stop between tasks, restart, and verify completed work is not repeated.
- Record any missing environment or usage data as unavailable.
- Keep Windows and Intel Mac marked untested until actually checked.
