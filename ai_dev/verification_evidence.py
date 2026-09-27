"""Small, side-effect-free helpers for comparing verification evidence."""

import hashlib
import json
import re


_MAX_CHECKS = 32
_MAX_TAIL = 6000
# Normalize only elapsed times in recognizable test-runner summary lines.
_TIME = re.compile(
    r"(?m)(^(?:Ran \d+ tests? in |FAILED \([^\n]*\) in |"
    r"[= ]*\d+ (?:failed|passed|errors?)[^\n]*? in ))"
    r"\d+(?:\.\d+)?s\b"
)
_UNITTEST = re.compile(r"FAILED \((?:failures=(\d{1,9})(?:,\s*errors=(\d{1,9}))?|errors=(\d{1,9}))\)")
_PYTEST = re.compile(
    r"(?:\d{1,9} (?:failed|passed|errors?|skipped|deselected|xfailed|xpassed|warnings?))"
    r"(?:, \d{1,9} (?:failed|passed|errors?|skipped|deselected|xfailed|xpassed|warnings?))*"
    r" in \d+(?:\.\d+)?s(?: \(\d+:\d+:\d+\))?"
)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _command_id(command):
    return _digest(json.dumps(command, ensure_ascii=False, separators=(",", ":")))


def _fingerprint(command_id, exit_code, tail):
    normalized = _TIME.sub(r"\1<TIME>", tail[:_MAX_TAIL])
    return _digest("%s\0%d\0%s" % (command_id, exit_code, normalized))


def _test_counts(tail):
    counts = []
    for line in tail.splitlines():
        line = line.strip().strip("=").strip()
        match = _UNITTEST.fullmatch(line)
        if match:
            # unittest omits zero-valued categories in its exact FAILED summary.
            counts.append({"kind": "unittest", "failures": int(match[1] or 0),
                           "errors": int(match[2] or match[3] or 0)})
        elif _PYTEST.fullmatch(line):
            pairs = re.findall(r"(\d+) (failed|errors?)\b", line)
            values = {}
            for number, label in pairs:
                label = "failures" if label == "failed" else "errors"
                if label in values:
                    return None
                values[label] = int(number)
            if values:
                counts.append(dict(kind="pytest", failures=values.get("failures", 0),
                                   errors=values.get("errors", 0)))
    # Multiple summaries may refer to different runs; do not guess which applies.
    if len(counts) == 1 and counts[0]["failures"] + counts[0]["errors"] > 0:
        return counts[0]
    return None


def summarize(verification):
    """Return bounded, JSON-safe evidence without executing or reading anything."""
    result = {"status": "unknown", "complete": False, "command_ids": [],
              "failed_command_ids": [], "fingerprints": {}, "failing_tests": {}}
    if not isinstance(verification, dict):
        return result
    checks = verification.get("checks")
    if not isinstance(checks, list) or not checks:
        return result

    seen = set()
    records = []
    valid = len(checks) <= _MAX_CHECKS and isinstance(verification.get("ok"), bool)
    for check in checks[:_MAX_CHECKS]:
        if not isinstance(check, dict):
            valid = False
            continue
        command = check.get("command")
        code = check.get("exit_code")
        tail = check.get("tail")
        if (not isinstance(command, list) or not command or
                len(command) > 128 or
                not all(isinstance(arg, str) and len(arg) <= 4096 for arg in command) or
                not command[0] or
                not isinstance(code, int) or isinstance(code, bool) or
                abs(code) > 2147483647 or not isinstance(tail, str)):
            valid = False
            continue
        if len(tail) > _MAX_TAIL:
            valid = False
        command_id = _command_id(command)
        if command_id in seen:
            valid = False
        seen.add(command_id)
        records.append((command_id, code, tail))

    result["command_ids"] = [record[0] for record in records]
    for command_id, code, tail in records:
        if code != 0:
            result["failed_command_ids"].append(command_id)
            result["fingerprints"][command_id] = _fingerprint(command_id, code, tail)
            counts = _test_counts(tail) if len(tail) <= _MAX_TAIL else None
            if counts is not None:
                result["failing_tests"][command_id] = counts

    valid = valid and verification.get("ok") == (not result["failed_command_ids"])
    result["complete"] = valid and len(records) == len(checks)
    if result["complete"]:
        result["status"] = "pass" if not result["failed_command_ids"] else "fail"
    return result


def _counts(summary, command_id):
    value = summary.get("failing_tests", {}).get(command_id)
    if not isinstance(value, dict):
        return None
    failures, errors = value.get("failures"), value.get("errors")
    if not isinstance(failures, int) or not isinstance(errors, int):
        return None
    return failures + errors


def compare(previous, current):
    """Compare two raw verification records using only concrete evidence."""
    old, new = summarize(previous), summarize(current)
    unknown = {"outcome": "unknown", "reason": "insufficient validated evidence"}
    if not old["complete"] or not new["complete"]:
        return unknown
    if set(old["command_ids"]) != set(new["command_ids"]):
        return {"outcome": "unknown", "reason": "different command coverage"}
    old_failed, new_failed = set(old["failed_command_ids"]), set(new["failed_command_ids"])
    if old["status"] == "pass" and new["status"] == "pass":
        return {"outcome": "unchanged", "reason": "all commands passed"}
    if old["status"] == "fail" and new["status"] == "pass":
        return {"outcome": "improved", "reason": "all known failures passed"}
    if old["status"] == "pass" and new["status"] == "fail":
        return {"outcome": "regressed", "reason": "new failed commands"}
    if new_failed < old_failed:
        return {"outcome": "improved", "reason": "known failed-command subset"}
    if new_failed > old_failed:
        return {"outcome": "regressed", "reason": "more known failed commands"}
    if new_failed != old_failed:
        return unknown

    changes = []
    for command_id in sorted(old_failed):
        before, after = _counts(old, command_id), _counts(new, command_id)
        if (before is None or after is None or
                old["failing_tests"][command_id]["kind"] !=
                new["failing_tests"][command_id]["kind"]):
            changes = []
            break
        changes.append(after - before)
    if changes and all(change <= 0 for change in changes) and any(changes):
        return {"outcome": "improved", "reason": "fewer failing tests"}
    if changes and all(change >= 0 for change in changes) and any(changes):
        return {"outcome": "regressed", "reason": "more failing tests"}
    if old["fingerprints"] == new["fingerprints"]:
        return {"outcome": "unchanged", "reason": "identical failure fingerprints"}
    return {"outcome": "unknown", "reason": "changed failure evidence"}
