"""Deterministic, bounded anti-loop evidence for PlanContract tasks."""

import hashlib
import json
import re

MAX_PACKET_BYTES = 32 * 1024
TARGET_PACKET_BYTES = 24 * 1024

_SECRET = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|cookie)\b\s*[:=]\s*)([^\s,;]+)")
_TEMP_PATH = re.compile(r"(?i)(?:/private)?/tmp/[^\s:'\"]+|/var/folders/[^\s:'\"]+|[A-Z]:\\Users\\[^\\\s]+\\AppData\\Local\\Temp\\[^\\\s]+")
_LINE_NO = re.compile(r"(?<=\.py:)[0-9]+(?=[:)])")
_FRAME = re.compile(r"(?m)^\s*File \"([^\"]+)\", line \d+, in ([^\n]+)")
_EXCEPTION = re.compile(r"(?m)^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Interrupt)):\s*(.*)$")
_UNTEST_COUNTS = re.compile(r"FAILED \((?:failures=(\d+)(?:, errors=(\d+))?|errors=(\d+))\)")


def _sha(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def redact(value):
    text = str(value or "")
    text = _SECRET.sub(r"\1[REDACTED]", text)
    return _TEMP_PATH.sub("<TEMP_PATH>", text)


def _normalized_error(tail):
    text = redact(tail)
    match = _EXCEPTION.search(text)
    if match:
        # Keep the exception and message, not the volatile test-count footer.
        message = match.group(2).strip()
        message = re.sub(r"\b0x[0-9a-fA-F]+\b", "<HEX>", message)
        return match.group(1) + ": " + message
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not (re.match(r"^(Ran \d+ tests?|FAILED \(|OK$|=+)", line)):
            return line[:500]
    return "verification failure"


def failure_record(verification, stage_id=None):
    """Fingerprint the stable root error while retaining a compact explanation."""
    checks = verification.get("checks", []) if isinstance(verification, dict) else []
    failed = []
    for check in checks:
        if not isinstance(check, dict) or check.get("exit_code") in (None, 0):
            continue
        command = check.get("command", [])
        command_text = " ".join(redact(item) for item in command) if isinstance(command, list) else redact(command)
        tail = redact(check.get("tail", ""))[-6000:]
        exception = _EXCEPTION.search(tail)
        frames = _FRAME.findall(tail)
        normalized_frames = []
        for path, function in frames[-5:]:
            path = path.replace('\\', '/')
            if '/site-packages/' in path:
                path = '<LIB>/' + path.split('/site-packages/', 1)[1]
            elif '/Library/Developer/' in path:
                path = '<PYTHON>/' + path.rsplit('/', 2)[-2] + '/' + path.rsplit('/', 1)[-1]
            else:
                path = '/'.join(path.split('/')[-2:])
            normalized_frames.append((path, function))
        frames = normalized_frames
        # Prefer the exception over changing test totals or temporary paths.
        root = _normalized_error(tail)
        test_names = sorted(set(re.findall(
            r"(?m)^(?:ERROR|FAIL):\s*([A-Za-z_][\w.]+)|^([A-Za-z_][\w.]+)\s+\([^\n]+\)\s*$", tail)))
        test_names = [name for a, b in test_names for name in [a or b]
                      if name.upper() not in ('FAILED', 'ERROR')][:32]
        count_match = _UNTEST_COUNTS.search(tail)
        failed_count = (int(count_match.group(1) or 0) + int(count_match.group(2) or count_match.group(3) or 0)
                        if count_match else len(test_names))
        payload = {'stage_id': stage_id, 'command': command_text,
                   'exit_code': check.get('exit_code'), 'root_error': root,
                   'exception': exception.group(1) if exception else None,
                   'frames': frames, 'failing_tests': test_names}
        failed.append({'signature': _sha(json.dumps(payload, sort_keys=True, separators=(',', ':'))),
                       'error_class': payload['exception'] or 'VerificationFailure',
                       'normalized_error': root[:500], 'command': command_text[:500],
                       'failing_tests': test_names, 'failing_count': failed_count, 'frames': frames,
                       'exit_code': check.get('exit_code')})
    if not failed:
        return None
    # Multiple failed checks are order-independent.
    signatures = sorted(item['signature'] for item in failed)
    return {'signature': _sha('|'.join(signatures)), 'failures': failed[:32]}


def review_failure(issue, stage_id=None):
    """Normalize a repeated PlanContract review violation as a stage failure."""
    if not isinstance(issue, str) or not issue.strip():
        return None
    text = redact(issue)
    text = re.sub(r"\battempt(?:s)?\s*[:=]?\s*\d+\b", "attempt <N>", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()[:1200]
    signature = _sha(json.dumps({'kind': 'review_violation', 'stage_id': stage_id,
                                 'issue': text}, sort_keys=True, separators=(',', ':')))
    return {'signature': signature, 'failures': [{'signature': signature,
        'error_class': 'PlanContractReviewViolation', 'normalized_error': text[:500],
        'command': None, 'failing_tests': [], 'failing_count': 1, 'frames': []}]}


def approach_record(paths, diff_text, stage_id, hypothesis=None, commands=None, working_hash=None):
    safe_paths = sorted(set(redact(path).replace('\\', '/') for path in paths if isinstance(path, str)))[:128]
    safe_diff = redact(diff_text)[:128 * 1024]
    data = {'stage_id': stage_id, 'changed_paths': safe_paths,
            'diff_hash': working_hash if isinstance(working_hash, str) and re.fullmatch(r'[0-9a-f]{64}', working_hash)
                         else _sha(safe_diff), 'hypothesis': redact(hypothesis)[:300] if hypothesis else None,
            'commands': [redact(' '.join(x) if isinstance(x, list) else x)[:300]
                         for x in (commands or [])[:32]]}
    data['approach_id'] = _sha(json.dumps(data, sort_keys=True, separators=(',', ':')))
    return data


def progress_signals(previous, current):
    """Only report measurable verification changes as progress."""
    signals = []
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return signals
    before, after = previous.get('failures', []), current.get('failures', [])
    before_total = sum(item.get('failing_count', len(item.get('failing_tests', [])) or 1) for item in before)
    after_total = sum(item.get('failing_count', len(item.get('failing_tests', [])) or 1) for item in after)
    if after_total < before_total:
        signals.append('fewer_failing_checks_or_tests')
    before_tests = {name for item in before for name in item.get('failing_tests', [])}
    after_tests = {name for item in after for name in item.get('failing_tests', [])}
    if before_tests - after_tests and after_total <= before_total:
        signals.append('previous_failing_test_disappeared')
    if current.get('signature') != previous.get('signature'):
        if after_total <= before_total:
            signals.append('failure_signature_changed')
        else:
            signals.append('regression_detected')
    return signals


def should_stop(previous, current, previous_approach, current_approach, signals,
                attempt_number, post_rescue=False):
    if attempt_number < 2:
        return {'stop': False, 'reason': 'first_failure_gets_one_correction'}
    same_failure = bool(previous and current and previous.get('signature') == current.get('signature'))
    meaningful = any(signal != 'regression_detected' for signal in signals)
    same_approach = bool(previous_approach and current_approach and
                         previous_approach.get('approach_id') == current_approach.get('approach_id'))
    if post_rescue and not meaningful:
        return {'stop': True, 'reason': 'post_rescue_no_progress'}
    if same_failure and not meaningful:
        return {'stop': True, 'reason': 'same_failure_without_measurable_progress'}
    if same_approach and not meaningful:
        return {'stop': True, 'reason': 'same_approach_without_measurable_progress'}
    return {'stop': False, 'reason': 'new_evidence_or_measurable_change'}


def classify_rescue(failure, trigger=None):
    text = json.dumps(failure or {}, ensure_ascii=False).lower()
    if any(term in text for term in ('modulenotfounderror', 'no module named', 'command not found',
                                      'permission denied', 'network is unreachable')):
        return {'automatic_model': False, 'reason': 'infrastructure_or_dependency_failure', 'model': None,
                'reasoning': None}
    high_risk = {'PUBLIC_API_CHANGED', 'SCHEMA_CHANGED', 'PERSISTENCE_CHANGED', 'NEW_CONCURRENCY',
                 'SECURITY_BOUNDARY_CHANGED', 'PLATFORM_CONTRACT_CHANGED', 'MIGRATION_REQUIRED',
                 'PLAN_DEVIATION'}
    if trigger in high_risk:
        return {'automatic_model': True, 'reason': 'high_risk_architecture_trigger:' + trigger,
                'model': 'gpt-6-astra', 'reasoning': 'medium'}
    return {'automatic_model': True, 'reason': 'engineering_ambiguity_after_no_progress',
            'model': 'gpt-6-sol', 'reasoning': 'medium'}


def build_rescue_packet(state, stage, attempts, current_diff, current_paths,
                        new_evidence=None, known_facts=None, unknowns=None,
                        rescue_class=None):
    contract = state.get('plan_contract') or {}
    acceptance = stage.get('acceptance', [])
    if isinstance(acceptance, str):
        acceptance = [acceptance]
    if not isinstance(acceptance, list):
        acceptance = []
    packet = {'schema_version': 1, 'rescue_id': 'rescue-' + _sha(
        str(state.get('id')) + ':' + str(stage.get('id')) + ':' + str(len(attempts)))[:20],
        'task_id': state.get('id'), 'stage_id': stage.get('id'),
        'plan_contract_id': contract.get('contract_id'),
        'evidence_packet_id': (state.get('planning_evidence') or {}).get('packet_id'),
        'goal': str(stage.get('title') or state.get('prompt') or '')[:1000],
        'expected': [redact(item)[:500] for item in acceptance[:16]
                     if isinstance(item, str)],
        'attempts': [{key: item.get(key) for key in ('attempt_id', 'attempt', 'hypothesis',
            'approach_id', 'files_after', 'verification_after', 'failure_signature',
            'progress_signals', 'plan_deviation', 'new_evidence', 'result')}
            for item in attempts[-2:]],
        'unchanged_failure': (attempts[-1].get('failure_signature') if attempts else None),
        'current_diff': {'paths': [redact(x)[:500] for x in current_paths[:64]],
                         'bounded_fragments': redact(current_diff)[:8 * 1024]},
        'new_evidence': [redact(x)[:500] for x in (new_evidence or [])[:32]],
        'known_facts': [redact(x)[:500] for x in (known_facts or [])[:32]],
        'unknowns': [redact(x)[:500] for x in (unknowns or [])[:32]],
        'question_for_rescue': 'Назови одну проверяемую следующую стратегию; не меняй PlanContract. '
            'Если контракт неверен, верни PLAN_CONTRACT_REVISION_REQUIRED.',
        'references': [], 'rescue_routing': rescue_class or {}}
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    if len(encoded) > MAX_PACKET_BYTES:
        packet['current_diff']['bounded_fragments'] = packet['current_diff']['bounded_fragments'][:2048]
        packet['known_facts'] = packet['known_facts'][:8]
        packet['unknowns'] = packet['unknowns'][:8]
        packet['new_evidence'] = packet['new_evidence'][:8]
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    if len(encoded) > MAX_PACKET_BYTES:
        packet['current_diff']['bounded_fragments'] = ''
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError('Rescue Packet exceeds hard byte limit.')
    return packet, encoded
