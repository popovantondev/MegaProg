"""PlanContract-driven deterministic and risk-triggered review cascade.

This module is deliberately local-first. It creates bounded evidence packets,
never copies raw worker logs, and does not infer a model verdict from missing
evidence.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

SCHEMA_VERSION = 1
REVIEW_PACKET_BUDGET = 32 * 1024
ASTRA_PACKET_BUDGET = 12 * 1024
DIFF_BUDGET = 20 * 1024
FRAGMENT_LIMIT = 5000

TRIGGERS = {
    'PUBLIC_API_CHANGED', 'SCHEMA_CHANGED', 'PERSISTENCE_CHANGED',
    'NEW_CONCURRENCY', 'SECURITY_BOUNDARY_CHANGED',
    'FILES_OUTSIDE_ALLOWED_SCOPE', 'FORBIDDEN_PATH_CHANGED', 'TEST_GAP',
    'TEST_FAILURE', 'PLAN_DEVIATION', 'PLATFORM_CONTRACT_CHANGED',
    'UNEXPECTED_DEPENDENCY', 'MIGRATION_REQUIRED', 'UNKNOWN_CRITICAL_STATE',
}
HIGH_COST_TRIGGERS = {
    'PUBLIC_API_CHANGED', 'SCHEMA_CHANGED', 'PERSISTENCE_CHANGED',
    'NEW_CONCURRENCY', 'SECURITY_BOUNDARY_CHANGED',
    'PLATFORM_CONTRACT_CHANGED', 'MIGRATION_REQUIRED', 'PLAN_DEVIATION',
}
_HIGH_PATH = re.compile(r'(^|/)(api|public|schema|schemas|migration|migrations|security|auth|persistence|database|db)(/|\.|$)|(^|/)(lock|mutex|thread|process|concurrency)[^/]*$', re.I)
_DEPENDENCY = re.compile(r'(^|/)(requirements[^/]*\.txt|pyproject\.toml|package\.json|package-lock\.json|poetry\.lock|uv\.lock|cargo\.toml|go\.mod)$', re.I)
_TEST_COMMAND = re.compile(r'(^|[/ ])(pytest|unittest|tox|nox)([/ ]|$)|(^|[/ ])test([/_-]|$)', re.I)
_AUTOPASS = re.compile(r'^(configured (verification|checks|tests) pass|all configured (verification|checks|tests) pass)\.?$', re.I)
_SECRET = re.compile(r'(?i)(\b(?:api[_-]?key|token|password|secret|authorization)\b\s*[:=]\s*)([^\s,;]+)')
SEMANTIC_OUTPUT_SCHEMA = ('{"contract_compliance":"PASS|FAIL|UNKNOWN","acceptance_results":[{"criterion":"exact text",'
    '"status":"PASS|FAIL|UNKNOWN","evidence_refs":["exact supplied ref"]}],"invariant_violations":[],'
    '"acceptance_gaps":[],"plan_deviations":[],"new_risks":[],"astra_required":false,'
    '"astra_trigger":null,"astra_reason":null}')


def _git(root, *args):
    proc = subprocess.run(['git', *args], cwd=str(root), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding='utf-8',
                          errors='replace')
    if proc.returncode:
        raise ValueError((proc.stderr or proc.stdout or 'git command failed')[:500])
    return proc.stdout


def _sha(value):
    return hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()


def _working_hash(root):
    digest = hashlib.sha256()
    digest.update(_git(root, 'diff', '--binary', 'HEAD').encode('utf-8'))
    digest.update(_git(root, 'status', '--porcelain', '-z').encode('utf-8'))
    names = [name for name in _git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0') if name]
    for name in sorted(names):
        path = Path(root) / name
        digest.update(name.encode('utf-8'))
        try:
            if path.is_symlink():
                digest.update(os.readlink(str(path)).encode('utf-8'))
            elif path.is_file():
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(65536), b''):
                        digest.update(chunk)
        except OSError:
            return None
    return digest.hexdigest()


def _under(path, scope):
    path = path.replace('\\', '/')
    return any(path == item or path.startswith(item.rstrip('/') + '/') for item in scope)


def _changed_paths(root):
    # Use NUL-delimited status to preserve spaces and Unicode in repository paths.
    raw = _git(root, 'status', '--porcelain', '-z')
    paths = []
    rows = raw.split('\0')
    index = 0
    while index < len(rows):
        row = rows[index]
        index += 1
        if len(row) < 4:
            continue
        code, name = row[:2], row[3:]
        if 'R' in code or 'C' in code:
            if index < len(rows):
                name = rows[index]
                index += 1
        if name:
            paths.append({'path': name.replace('\\', '/'), 'status': code.strip() or '??'})
    untracked = None
    expanded = []
    for item in paths:
        if item['status'] == '??' and item['path'].endswith('/'):
            if untracked is None:
                untracked = [name for name in _git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0') if name]
            prefix = item['path'].rstrip('/') + '/'
            expanded.extend({'path': name.replace('\\', '/'), 'status': '??'}
                            for name in untracked if name.replace('\\', '/').startswith(prefix))
        else:
            expanded.append(item)
    # MegaProg runtime state is generated outside the project change scope.
    # Repositories predating the current .gitignore may expose it as untracked.
    expanded = [item for item in expanded if not item['path'].replace('\\', '/').startswith('.ai-dev/')]
    return sorted(expanded, key=lambda item: item['path'])


def _diff_fragments(root, paths):
    selected, considered, considered_bytes = [], 0, 0
    remaining = DIFF_BUDGET
    for item in paths:
        name = item['path']
        considered += 1
        if name.startswith('.ai-dev/') or item['status'] == 'D':
            continue
        if item['status'] == '??':
            path = Path(root) / name
            try:
                if not path.is_file() or path.stat().st_size > FRAGMENT_LIMIT:
                    continue
                content = path.read_text(encoding='utf-8', errors='replace')[:FRAGMENT_LIMIT]
                content = _SECRET.sub(r'\1[REDACTED]', content)
                fragment = '+++ %s (untracked excerpt) +++\n%s' % (name, content)
            except OSError:
                continue
        else:
            try:
                fragment = _git(root, 'diff', '--no-ext-diff', '--unified=3', 'HEAD', '--', name)
            except ValueError:
                continue
            if not fragment:
                continue
            considered_bytes += len(fragment.encode('utf-8', 'replace'))
            fragment = fragment[:FRAGMENT_LIMIT]
        if item['status'] == '??':
            considered_bytes += len(fragment.encode('utf-8', 'replace'))
        encoded = fragment.encode('utf-8', 'replace')
        if len(encoded) > remaining:
            if remaining < 300:
                break
            fragment = encoded[:remaining].decode('utf-8', 'ignore')
            encoded = fragment.encode('utf-8')
        fragment = _SECRET.sub(r'\1[REDACTED]', fragment)
        encoded = fragment.encode('utf-8')
        selected.append({'path': name, 'status': item['status'],
                         'bytes': len(encoded), 'sha256': _sha(fragment),
                         'excerpt': fragment})
        remaining -= len(encoded)
        if remaining <= 0:
            break
    return selected, considered, considered_bytes


def _acceptance_matrix(stage, verification):
    checks = verification.get('checks') if isinstance(verification, dict) else None
    check_pass = bool(isinstance(checks, list) and checks and
                      verification.get('ok') is True and
                      all(isinstance(row, dict) and row.get('exit_code') == 0 for row in checks))
    command_ids = []
    for row in checks or []:
        if isinstance(row, dict):
            command = row.get('command')
            if isinstance(command, list):
                command_ids.append(_sha(json.dumps(command, ensure_ascii=False, separators=(',', ':')))[:16])
    verification_text = {str(value).strip().casefold() for value in stage.get('verification', [])
                         if isinstance(value, str)}
    matrix = []
    for criterion in stage.get('acceptance', []):
        value = str(criterion).strip()
        if check_pass and (_AUTOPASS.fullmatch(value) or value.casefold() in verification_text):
            status = 'PASS'
            refs = ['verification:%s' % item for item in command_ids]
        elif isinstance(checks, list) and checks and verification.get('ok') is False:
            status, refs = 'FAIL', ['verification:%s' % item for item in command_ids]
        else:
            status, refs = 'UNKNOWN', []
        matrix.append({'criterion': value, 'evidence': refs, 'status': status})
    return matrix


def _worker_evidence(root, entries, acceptance):
    """Expose only bounded worker evidence that resolves to a real local artifact.

    A worker's PASS label is retained as an untrusted assertion; artifact
    existence alone never turns an acceptance criterion into PASS.
    """
    if not isinstance(entries, list):
        return []
    root = Path(root).resolve()
    accepted = {str(item).strip() for item in acceptance}
    seen = set()
    output = []
    for row in entries[:12]:
        if not isinstance(row, dict):
            continue
        ident, kind = row.get('evidence_id'), row.get('kind')
        claim, source_ref, artifact_ref = (row.get(key) for key in
            ('claim', 'source_ref', 'artifact_ref'))
        criterion = row.get('criterion')
        if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', ident) or
                ident in seen or not isinstance(kind, str) or not kind.strip() or len(kind) > 40 or
                not isinstance(claim, str) or not claim.strip() or len(claim) > 500 or
                not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > 200 or
                not isinstance(artifact_ref, str) or not artifact_ref.strip() or len(artifact_ref) > 500 or
                not isinstance(criterion, str) or criterion not in accepted):
            continue
        ref_path = Path(artifact_ref)
        if ref_path.is_absolute() or '..' in ref_path.parts:
            continue
        candidate = root / ref_path
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root) or resolved.stat().st_size > 8 * 1024 * 1024:
                continue
            hasher = hashlib.sha256()
            with resolved.open('rb') as handle:
                for block in iter(lambda: handle.read(65536), b''):
                    hasher.update(block)
            digest = hasher.hexdigest()
        except (OSError, ValueError):
            continue
        expected_hash = row.get('artifact_sha256')
        if expected_hash is not None and expected_hash != digest:
            continue
        seen.add(ident)
        output.append({'evidence_id': ident, 'kind': kind, 'criterion': criterion,
            'claim': claim.strip(), 'source_ref': source_ref.strip(),
            'artifact_ref': artifact_ref.replace('\\', '/'), 'artifact_sha256': digest,
            'worker_verification_claim': row.get('verification') if row.get('verification') in
                ('PASS', 'FAIL', 'UNKNOWN') else 'UNKNOWN', 'status': 'UNVERIFIED'})
    return output


def _trigger_from_evidence(name, paths, contract, verification, platform, diff=None):
    path_names = [item['path'] for item in paths]
    normalized = [name.replace('\\', '/') for name in path_names]
    diff_text = '\n'.join(item.get('excerpt', '') for item in (diff or []))
    changed_tests = any(re.search(r'(^|/)(tests?|spec)(/|_)|(^|/)test_[^/]+\.py$', p, re.I) for p in normalized)
    if name in ('FILES_OUTSIDE_ALLOWED_SCOPE', 'FORBIDDEN_PATH_CHANGED'):
        stage = contract.get('stages', [{}])[0]
        stage = stage.get('contract') if isinstance(stage.get('contract'), dict) else stage
        forbidden = stage.get('forbidden_paths', [])
        allowed = stage.get('allowed_paths', []) + stage.get('expected_paths', [])
        if name == 'FORBIDDEN_PATH_CHANGED' and any(_under(p, forbidden) for p in normalized):
            return True
        if name == 'FILES_OUTSIDE_ALLOWED_SCOPE' and any(not _under(p, allowed) for p in normalized):
            return True
        return False
    if name == 'TEST_GAP':
        checks = verification.get('checks') if isinstance(verification, dict) else None
        commands = [' '.join(row.get('command', [])) for row in (checks or []) if isinstance(row, dict)]
        # A successful configured command is not automatically a test command.
        return not changed_tests and not any(_TEST_COMMAND.search(command) for command in commands)
    if name == 'TEST_FAILURE':
        return isinstance(verification, dict) and verification.get('ok') is False
    if name == 'PLATFORM_CONTRACT_CHANGED':
        deferred = set((contract.get('platform_contract') or {}).get('deferred_platforms') or [])
        if 'windows' not in deferred:
            return False
        return any(('windows' in p.casefold() or _is_windows_specific(p)) and not _is_shared_contract(p)
                   for p in normalized)
    if name == 'UNEXPECTED_DEPENDENCY':
        return any(_DEPENDENCY.search('/' + p) for p in normalized)
    if name == 'MIGRATION_REQUIRED':
        return any(re.search(r'(^|/)(migrations?|schema)(/|$)', p, re.I) for p in normalized)
    if name in ('SCHEMA_CHANGED', 'PERSISTENCE_CHANGED', 'PUBLIC_API_CHANGED',
                'NEW_CONCURRENCY', 'SECURITY_BOUNDARY_CHANGED'):
        if name == 'PUBLIC_API_CHANGED':
            return any(re.search(r'(^|/)(api|public)(/|\.|$)', '/' + p, re.I) for p in normalized)
        if name == 'SCHEMA_CHANGED':
            return any(re.search(r'(^|/)(schema|schemas)(/|\.|$)', '/' + p, re.I) for p in normalized)
        if name == 'PERSISTENCE_CHANGED':
            return any(re.search(r'(^|/)(persistence|database|db)(/|\.|$)', '/' + p, re.I) for p in normalized)
        if name == 'NEW_CONCURRENCY':
            return (any(re.search(r'(^|/)(lock|mutex|thread|process|concurrency)[^/]*$', '/' + p, re.I)
                        for p in normalized) or
                    bool(re.search(r'(?m)^\+.*(?:async def|threading\.|concurrent\.futures|multiprocessing\.|Lock\()', diff_text)))
        if name == 'SECURITY_BOUNDARY_CHANGED':
            return (any(re.search(r'(^|/)(security|auth|permissions?)(/|\.|$)', '/' + p, re.I)
                        for p in normalized) or
                    bool(re.search(r'(?im)^\+.*(?:authorize|authenticate|permission|credential|token validation)', diff_text)))
    if name == 'PLAN_DEVIATION':
        return bool(contract.get('_deviation_reported'))
    if name == 'UNKNOWN_CRITICAL_STATE':
        return any(item.get('status') == '??' and _HIGH_PATH.search('/' + item['path']) for item in paths)
    return False


def _is_windows_specific(path):
    lowered = path.casefold()
    return bool(re.search(r'(^|/)(windows?|win32|win64|powershell|cmd)(/|\.|$)', lowered))


def _is_shared_contract(path):
    lowered = path.casefold()
    return bool(re.search(r'(^|/)(contracts?|schemas?)/|platform_contract|shared_contract', lowered))


def build_review_packet(root, state, stage, verification, *, packet=None, worker_evidence=None):
    root = Path(root)
    contract = state.get('plan_contract')
    if not isinstance(contract, dict):
        raise ValueError('Review Cascade requires a validated PlanContract.')
    stage_contract = stage.get('contract') if isinstance(stage.get('contract'), dict) else stage
    changed = _changed_paths(root)
    diff, considered, diff_bytes_considered = _diff_fragments(root, changed)
    current_head = _git(root, 'rev-parse', 'HEAD').strip()
    current_dirty_hash = _working_hash(root)
    planned = contract.get('snapshot') or {}
    packet_snapshot = packet.get('snapshot') if isinstance(packet, dict) else {}
    planned_head = planned.get('head')
    stale = (current_head != planned_head or not isinstance(packet, dict) or
             packet.get('packet_id') != contract.get('packet_id') or
             packet_snapshot.get('head') != planned_head or
             packet_snapshot.get('dirty_hash') != planned.get('dirty_hash'))
    unexpected = [item['path'] for item in changed
                  if not _under(item['path'], stage_contract.get('allowed_paths', []) +
                                stage_contract.get('expected_paths', []))]
    missing_expected = [item for item in stage_contract.get('expected_paths', [])
                        if not (root / item).exists()]
    forbidden = [item['path'] for item in changed
                 if _under(item['path'], stage_contract.get('forbidden_paths', []))]
    matrix = _acceptance_matrix(stage_contract, verification)
    typed_evidence = _worker_evidence(root, worker_evidence or [], stage_contract.get('acceptance', []))
    for row in matrix:
        refs = ['worker-evidence:' + item['evidence_id'] for item in typed_evidence
                if item['criterion'] == row['criterion']]
        row['evidence'] = list(dict.fromkeys(row['evidence'] + refs))
    verification_rows = []
    checks = verification.get('checks', []) if isinstance(verification, dict) else []
    for index, row in enumerate(checks[:32] if isinstance(checks, list) else []):
        if not isinstance(row, dict):
            continue
        command = row.get('command') if isinstance(row.get('command'), list) else []
        cid = _sha(json.dumps(command, ensure_ascii=False, separators=(',', ':')))[:16]
        verification_rows.append({'evidence_id': cid, 'command': command[:32],
                                  'exit_code': row.get('exit_code'),
                                  'status': 'PASS' if row.get('exit_code') == 0 else
                                            ('FAIL' if isinstance(row.get('exit_code'), int) else 'UNKNOWN'),
                                  'tail': str(row.get('tail', ''))[-1000:]})
    deviation = state.get('stage_deviation') if isinstance(state.get('stage_deviation'), dict) else None
    trigger_contract = dict(contract)
    trigger_contract['_deviation_reported'] = bool(deviation)
    trigger_names = set(stage_contract.get('review_triggers', []))
    review = contract.get('review_contract') or {}
    for key in ('architectural_triggers', 'security_triggers', 'scope_triggers'):
        trigger_names.update(review.get(key, []))
    trigger_names.update({'FILES_OUTSIDE_ALLOWED_SCOPE', 'FORBIDDEN_PATH_CHANGED', 'TEST_GAP',
                          'TEST_FAILURE', 'PUBLIC_API_CHANGED', 'SCHEMA_CHANGED',
                          'PERSISTENCE_CHANGED', 'NEW_CONCURRENCY', 'SECURITY_BOUNDARY_CHANGED',
                          'PLAN_DEVIATION', 'PLATFORM_CONTRACT_CHANGED', 'UNEXPECTED_DEPENDENCY',
                          'MIGRATION_REQUIRED', 'UNKNOWN_CRITICAL_STATE'})
    trigger_names.intersection_update(TRIGGERS)
    triggers = sorted(name for name in trigger_names
                      if _trigger_from_evidence(name, changed, trigger_contract, verification,
                                                contract.get('platform_contract') or {}, diff))
    unknowns = []
    if stale:
        unknowns.append('PlanContract or Evidence Packet snapshot is stale.')
    if not isinstance(checks, list) or not checks:
        unknowns.append('No verification command result is available.')
    if any(row.get('status') == 'UNKNOWN' for row in matrix):
        unknowns.append('One or more acceptance criteria lack deterministic evidence.')
    # Prevent an oversized evidence object; retain metadata and references while
    # dropping lowest-priority diff excerpts at whole-file boundaries.
    snapshot = {'planned_head': planned_head, 'current_head': current_head,
                'planned_dirty_hash': planned.get('dirty_hash'),
                'current_dirty_hash': current_dirty_hash}
    value = {
        'schema_version': SCHEMA_VERSION,
        'review_id': 'review-' + _sha(state.get('id', '') + str(stage.get('id', '')) + current_head)[:20],
        'task_id': state.get('id'), 'stage_id': stage.get('id'),
        'plan_contract_id': contract.get('contract_id'),
        'evidence_packet_id': (state.get('planning_evidence') or {}).get('packet_id'),
        'snapshot': snapshot,
        'contract': {'objective': stage.get('title') or stage.get('objective'),
            'decision': (contract.get('decision') or {}).get('summary'),
            'invariants': contract.get('invariants', []),
            'allowed_paths': stage_contract.get('allowed_paths', []),
            'expected_paths': stage_contract.get('expected_paths', []),
            'forbidden_paths': stage_contract.get('forbidden_paths', []),
            'acceptance': stage_contract.get('acceptance', []),
            'review_triggers': sorted(trigger_names),
            'platform_contract': contract.get('platform_contract', {}),
            'rollback': stage_contract.get('rollback')},
        'implementation': {'changed_paths': changed, 'files_changed': len(changed),
            'reported_plan_deviation': deviation,
            'diff_bytes_considered': diff_bytes_considered,
            'diff_bytes_selected': sum(item['bytes'] for item in diff),
            'files_considered': considered, 'files_selected_for_semantic_review': [item['path'] for item in diff],
            'diff_summary': [{'path': item['path'], 'status': item['status'],
                              'bytes': item['bytes'], 'sha256': item['sha256']} for item in diff],
            'diff_fragments': diff, 'unexpected_paths': unexpected,
            'forbidden_paths_changed': forbidden,
            'missing_expected_paths': missing_expected,
            'verification_results': verification_rows},
        'deterministic_checks': {'snapshot_fresh': not stale,
            'contract_validated': bool(contract.get('contract_id')),
            'scope_within_allowed': not unexpected,
            'expected_paths_present': not missing_expected,
            'forbidden_paths_unchanged': not forbidden,
            'verification_complete': bool(isinstance(checks, list) and checks),
            'verification_pass': bool(isinstance(verification, dict) and verification.get('ok') is True),
            'acceptance_matrix': matrix},
        'triggered_reviews': [{'trigger': name, 'source': 'deterministic-evidence'} for name in triggers],
        'unknowns': unknowns, 'conflicts': [],
        'references': [{'kind': 'plan_contract', 'id': contract.get('contract_id')}],
        'worker_evidence': typed_evidence,
        'available_evidence_refs': (['contract:objective'] +
            ['contract:invariant:%d' % i for i in range(len(contract.get('invariants', [])))] +
            ['contract:acceptance:%d' % i for i in range(len(stage_contract.get('acceptance', [])))] +
            ['diff:' + item['path'] for item in diff] +
            ['verification:' + item['evidence_id'] for item in verification_rows] +
            ['worker-evidence:' + item['evidence_id'] for item in typed_evidence] +
            ['trigger:' + name for name in triggers]),
        'metrics': {'review_packet_bytes': 0, 'diff_bytes_considered': diff_bytes_considered,
                    'diff_bytes_selected': sum(i['bytes'] for i in diff),
                    'files_changed': len(changed), 'files_selected_for_semantic_review': len(diff)}}
    while len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > REVIEW_PACKET_BUDGET:
        fragments = value['implementation']['diff_fragments']
        if not fragments:
            raise ValueError('Review Packet metadata exceeds its bounded context budget.')
        removed = fragments.pop()
        value['implementation']['diff_summary'] = [item for item in value['implementation']['diff_summary']
                                                    if item['path'] != removed['path']]
        value['implementation']['files_selected_for_semantic_review'].remove(removed['path'])
        value['implementation']['diff_bytes_selected'] -= removed['bytes']
        value['metrics']['diff_bytes_selected'] -= removed['bytes']
        unknowns.append('Diff excerpt omitted by review packet budget: %s.' % removed['path'])
        value['unknowns'] = list(dict.fromkeys(unknowns))
    value['metrics']['review_packet_bytes'] = len(json.dumps(value, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8'))
    value['deterministic_checks']['mechanical_status'] = deterministic_status(value)
    for _ in range(4):
        size = len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if value['metrics']['review_packet_bytes'] == size:
            break
        value['metrics']['review_packet_bytes'] = size
    return value


def deterministic_status(packet):
    checks = packet.get('deterministic_checks', {})
    if not checks.get('snapshot_fresh') or not checks.get('contract_validated'):
        return 'BLOCKED'
    if (not checks.get('scope_within_allowed') or not checks.get('expected_paths_present') or
            not checks.get('forbidden_paths_unchanged')):
        return 'FIX_REQUIRED'
    hard_local_failures = {item.get('trigger') for item in packet.get('triggered_reviews', [])} & {
        'FILES_OUTSIDE_ALLOWED_SCOPE', 'FORBIDDEN_PATH_CHANGED', 'TEST_GAP', 'TEST_FAILURE'}
    if hard_local_failures:
        return 'FIX_REQUIRED'
    if not checks.get('verification_complete'):
        return 'UNKNOWN'
    if not checks.get('verification_pass'):
        return 'FIX_REQUIRED'
    if any(row.get('status') == 'FAIL' for row in checks.get('acceptance_matrix', [])):
        return 'FIX_REQUIRED'
    if any(row.get('status') in ('NOT_TESTED', 'UNKNOWN') for row in checks.get('acceptance_matrix', [])):
        return 'SEMANTIC_REVIEW_REQUIRED'
    if any('omitted by review packet budget' in str(item).casefold()
           for item in packet.get('unknowns', [])):
        return 'UNKNOWN'
    if packet.get('triggered_reviews'):
        return 'SEMANTIC_REVIEW_REQUIRED'
    return 'PASS_NO_MODEL_REVIEW'


def required_tier(packet):
    """Return none, sol, or astra based only on proven triggers and task evidence."""
    status = deterministic_status(packet)
    if status != 'SEMANTIC_REVIEW_REQUIRED':
        return None
    triggers = {item.get('trigger') for item in packet.get('triggered_reviews', [])}
    deviation = packet.get('implementation', {}).get('reported_plan_deviation')
    if not (isinstance(deviation, dict) and deviation.get('architectural') is True):
        triggers.discard('PLAN_DEVIATION')
    return 'astra' if triggers & HIGH_COST_TRIGGERS else 'sol'


def required_tier(packet):
    """Return none, sol, or astra based only on proven triggers and task evidence."""
    status = deterministic_status(packet)
    if status != 'SEMANTIC_REVIEW_REQUIRED':
        return None
    triggers = {item.get('trigger') for item in packet.get('triggered_reviews', [])}
    deviation = packet.get('implementation', {}).get('reported_plan_deviation')
    if not (isinstance(deviation, dict) and deviation.get('architectural') is True):
        triggers.discard('PLAN_DEVIATION')
    return 'astra' if triggers & HIGH_COST_TRIGGERS else 'sol'


def semantic_prompt(packet, *, tier='sol'):
    payload = json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
    return ('Review only whether the implementation complies with the approved PlanContract. '
            'Do not redesign the task. Report only evidence-backed violations and uncertainties. '
            'Do not infer missing facts. An unproved violation is not a violation. '
            'Use only the supplied packet and excerpts. Return ONLY this JSON shape: %s. ' % SEMANTIC_OUTPUT_SCHEMA +
            'acceptance_results must contain exactly one entry per contract acceptance criterion: '
            '{"criterion":"exact criterion text","status":"PASS|FAIL|UNKNOWN","evidence_refs":["exact supplied ref"]}. '
            'PASS/FAIL require at least one exact evidence ref; use UNKNOWN when evidence is insufficient. '
            'Each finding in invariant_violations, acceptance_gaps, plan_deviations and new_risks must be '
            '{"explanation":"...","evidence_refs":["exact supplied ref"]}. '
            'Set astra_required true only for a concrete high-cost architectural/security/platform uncertainty. '
            'Review tier: %s.\nREVIEW PACKET:\n%s' % (tier, payload))


def parse_semantic_result(message):
    if not isinstance(message, str) or len(message.encode('utf-8')) > 12000:
        raise ValueError('Semantic review response is absent or exceeds limit.')
    text = message.strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    value = json.loads(text)
    keys = {'contract_compliance', 'acceptance_results', 'invariant_violations', 'acceptance_gaps',
            'plan_deviations', 'new_risks', 'astra_required', 'astra_trigger', 'astra_reason'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('Semantic review schema mismatch.')
    if value.get('contract_compliance') not in ('PASS', 'FAIL', 'UNKNOWN'):
        raise ValueError('Invalid contract_compliance.')
    if type(value.get('astra_required')) is not bool:
        raise ValueError('Invalid astra_required.')
    if value['astra_required'] and (not isinstance(value.get('astra_reason'), str) or not value['astra_reason'].strip()):
        raise ValueError('Astra escalation requires a concrete reason.')
    if value['astra_required'] and value.get('astra_trigger') not in HIGH_COST_TRIGGERS:
        raise ValueError('Astra escalation requires a recognized high-cost trigger.')
    if not value['astra_required'] and value.get('astra_trigger') is not None:
        raise ValueError('astra_trigger must be null when no Astra escalation is requested.')
    acceptance = value.get('acceptance_results')
    if not isinstance(acceptance, list) or len(acceptance) > 32:
        raise ValueError('Invalid acceptance_results.')
    for row in acceptance:
        if (not isinstance(row, dict) or set(row) != {'criterion', 'status', 'evidence_refs'} or
                not isinstance(row['criterion'], str) or not row['criterion'].strip() or
                row['status'] not in ('PASS', 'FAIL', 'UNKNOWN') or
                not isinstance(row['evidence_refs'], list) or
                (row['status'] in ('PASS', 'FAIL') and not row['evidence_refs']) or
                not all(isinstance(ref, str) and ref.strip() for ref in row['evidence_refs'])):
            raise ValueError('Invalid acceptance result; statuses need evidence.')
    for key in ('invariant_violations', 'acceptance_gaps', 'plan_deviations', 'new_risks'):
        rows = value.get(key)
        if not isinstance(rows, list) or len(rows) > 16:
            raise ValueError('Invalid review list: ' + key)
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'explanation', 'evidence_refs'} or
                    not isinstance(row['explanation'], str) or not row['explanation'].strip() or
                    not isinstance(row['evidence_refs'], list) or not row['evidence_refs'] or
                    not all(isinstance(ref, str) and ref.strip() for ref in row['evidence_refs'])):
                raise ValueError('Review findings require evidence references.')
    if value['contract_compliance'] == 'FAIL' and not (value['invariant_violations'] or
       value['acceptance_gaps'] or value['plan_deviations']):
        raise ValueError('FAIL must contain a specific contract finding.')
    return value


def validate_acceptance_results(value, packet, *, allowed_refs=None):
    """Require exact coverage and references that exist in the Review Packet."""
    expected = [item.get('criterion') for item in
                packet.get('deterministic_checks', {}).get('acceptance_matrix', [])]
    actual = [item.get('criterion') for item in value.get('acceptance_results', [])]
    if actual != expected:
        raise ValueError('Review response does not cover acceptance criteria in contract order.')
    if allowed_refs is None:
        allowed = set()
        for row in packet.get('implementation', {}).get('verification_results', []):
            allowed.add('verification:' + row['evidence_id'])
        for item in packet.get('implementation', {}).get('diff_summary', []):
            allowed.add('diff:' + item['path'])
        for item in packet.get('worker_evidence', []):
            allowed.add('worker-evidence:' + item['evidence_id'])
        for index in range(len(packet.get('contract', {}).get('invariants', []))):
            allowed.add('contract:invariant:%d' % index)
        for index in range(len(expected)):
            allowed.add('contract:acceptance:%d' % index)
        allowed.add('contract:objective')
        allowed.update('trigger:' + item.get('trigger', '') for item in packet.get('triggered_reviews', []))
    else:
        allowed = set(allowed_refs)
    for item in value['acceptance_results']:
        if any(ref not in allowed for ref in item['evidence_refs']):
            raise ValueError('Review response cites evidence outside its packet.')
    for key in ('invariant_violations', 'acceptance_gaps', 'plan_deviations', 'new_risks'):
        for finding in value.get(key, []):
            if any(ref not in allowed for ref in finding['evidence_refs']):
                raise ValueError('Review finding cites evidence outside its packet.')
    if (value.get('contract_compliance') == 'PASS' and
            (value.get('invariant_violations') or value.get('acceptance_gaps') or
             value.get('plan_deviations') or
             any(row['status'] == 'FAIL' for row in value.get('acceptance_results', [])))):
        raise ValueError('PASS contradicts one or more reported contract failures.')


def build_astra_packet(packet, *, trigger, objection=None):
    """Return only the decision, trigger, matching excerpts and test evidence."""
    triggers = [trigger] if isinstance(trigger, str) else list(trigger)
    triggers = sorted(set(triggers))
    if not triggers or any(item not in HIGH_COST_TRIGGERS for item in triggers):
        raise ValueError('Astra review requires an explicit high-cost trigger.')
    source = packet.get('implementation', {})
    if ('PLAN_DEVIATION' in triggers and
            not (isinstance(source.get('reported_plan_deviation'), dict) and
                 source['reported_plan_deviation'].get('architectural') is True)):
        raise ValueError('Astra review for plan deviation requires an explicitly architectural deviation.')
    fragments = source.get('diff_fragments', [])
    relevant = []
    for item in fragments:
        path = item.get('path', '')
        if (('SECURITY_BOUNDARY_CHANGED' in triggers and re.search(r'auth|security|permission|session', path, re.I) or
            set(triggers) & {'SCHEMA_CHANGED', 'PERSISTENCE_CHANGED', 'MIGRATION_REQUIRED'} and
                re.search(r'schema|database|db|migration|model', path, re.I) or
            'PUBLIC_API_CHANGED' in triggers and re.search(r'api|public|interface', path, re.I) or
            'NEW_CONCURRENCY' in triggers and re.search(r'async|thread|process|lock|queue|worker', path, re.I) or
            'PLATFORM_CONTRACT_CHANGED' in triggers or 'PLAN_DEVIATION' in triggers)):
            relevant.append(item)
    if not relevant:
        # No path-specific evidence is not grounds to invent a finding.
        relevant = fragments[:1]
    refs = {'contract:objective'}
    refs.update('contract:invariant:%d' % i for i in range(
        len(packet.get('contract', {}).get('invariants', []))))
    refs.update('contract:acceptance:%d' % i for i in range(
        len(packet.get('contract', {}).get('acceptance', []))))
    refs.update('diff:' + item.get('path', '') for item in relevant)
    refs.update('verification:' + item.get('evidence_id', '')
                for item in source.get('verification_results', []))
    narrow = {'schema_version': 1, 'task_id': packet.get('task_id'),
        'stage_id': packet.get('stage_id'), 'plan_contract_id': packet.get('plan_contract_id'),
        'original_planner_decision': packet.get('contract', {}).get('decision'),
        'specific_contract_clauses': {'objective': packet.get('contract', {}).get('objective'),
            'invariants': packet.get('contract', {}).get('invariants', []),
            'acceptance': packet.get('contract', {}).get('acceptance', []),
            'platform_contract': packet.get('contract', {}).get('platform_contract')},
        'triggers': triggers, 'reviewer_objection': objection,
        'relevant_diff_fragments': relevant,
        'relevant_test_evidence': source.get('verification_results', []),
        'available_evidence_refs': sorted(refs)}
    # Keep Astra's context materially smaller than the general Review Packet.
    for row in narrow['relevant_test_evidence']:
        row['tail'] = str(row.get('tail', ''))[-600:]
    while len(json.dumps(narrow, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > ASTRA_PACKET_BUDGET:
        if narrow['relevant_diff_fragments']:
            removed = narrow['relevant_diff_fragments'].pop()
            narrow['available_evidence_refs'] = [ref for ref in narrow['available_evidence_refs']
                if ref != 'diff:' + removed.get('path', '')]
        elif narrow['relevant_test_evidence']:
            narrow['relevant_test_evidence'].pop()
        else:
            raise ValueError('Narrow Astra review packet metadata exceeds its 12 KiB budget.')
    narrow['packet_bytes'] = len(json.dumps(narrow, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8'))
    return narrow


def astra_prompt(narrow_packet):
    return ('One or more high-cost review triggers were detected. Address every listed trigger and answer only whether this specific change violates '
        'the supplied PlanContract clause. If yes, cite exact evidence and state the minimal required correction. '
        'Do not review unrelated files or redesign the task. Return ONLY JSON matching the semantic-review schema '
        'and provide acceptance_results for every listed criterion. Use exact supplied evidence references.\n'
        'Output schema: %s\nNARROW ASTRA PACKET:\n' % SEMANTIC_OUTPUT_SCHEMA +
        json.dumps(narrow_packet, ensure_ascii=False, separators=(',', ':')))


def result(packet, *, semantic=None, astra=None, turns=None):
    turns = turns if isinstance(turns, list) else []
    status = deterministic_status(packet)
    if status in ('BLOCKED', 'FIX_REQUIRED', 'UNKNOWN'):
        final = status
    elif status == 'PASS_NO_MODEL_REVIEW':
        final = 'PASS_NO_MODEL_REVIEW'
    elif astra is not None:
        compliance = astra.get('contract_compliance')
        final = {'PASS': 'PASS', 'FAIL': 'FIX_REQUIRED', 'UNKNOWN': 'UNKNOWN'}.get(compliance, 'UNKNOWN')
    elif semantic is not None:
        if semantic.get('astra_required'):
            final = 'ESCALATE'
        else:
            compliance = semantic.get('contract_compliance')
            final = {'PASS': 'PASS', 'FAIL': 'FIX_REQUIRED', 'UNKNOWN': 'UNKNOWN'}.get(compliance, 'UNKNOWN')
    elif status == 'SEMANTIC_REVIEW_REQUIRED':
        # When a caller deliberately runs offline, absence of a reviewer is
        # UNKNOWN evidence, not an escalation decision that could be mistaken
        # for completed review.
        final = 'UNKNOWN'
    else:
        final = 'UNKNOWN'
    chosen = astra if astra is not None else semantic
    acceptance = packet.get('deterministic_checks', {}).get('acceptance_matrix', [])
    if isinstance(chosen, dict) and len(chosen.get('acceptance_results', [])) == len(acceptance):
        acceptance = chosen['acceptance_results']
    if isinstance(chosen, dict) and (
            chosen.get('invariant_violations') or chosen.get('acceptance_gaps') or
            chosen.get('plan_deviations') or
            any(row.get('status') == 'FAIL' for row in chosen.get('acceptance_results', []))):
        if final == 'PASS':
            final = 'FIX_REQUIRED'
    if final in ('PASS', 'PASS_NO_MODEL_REVIEW') and any(
            row.get('status') in ('UNKNOWN', 'NOT_TESTED') for row in acceptance):
        final = 'UNKNOWN'
    if final == 'PASS_NO_MODEL_REVIEW' and any(row.get('status') != 'PASS' for row in acceptance):
        final = 'UNKNOWN'
    return {'schema_version': SCHEMA_VERSION, 'review_id': packet.get('review_id'),
        'task_id': packet.get('task_id'), 'stage_id': packet.get('stage_id'),
        'plan_contract_id': packet.get('plan_contract_id'), 'status': final,
        'deterministic_status': status, 'deterministic_triggers': packet.get('triggered_reviews', []),
        'semantic_review': semantic, 'astra_review': astra, 'acceptance_matrix': acceptance,
        'astra_trigger': next((x.get('trigger') for x in turns if x.get('role') == 'astra_review'), None),
        'telemetry': {'review_id': packet.get('review_id'),
            'review_packet_bytes': packet.get('metrics', {}).get('review_packet_bytes'),
            'deterministic_review_status': status,
            'deterministic_triggers': [item.get('trigger') for item in packet.get('triggered_reviews', [])],
            'semantic_review_used': any(x.get('role') == 'semantic_review' for x in turns),
            'semantic_review_model': next((x.get('model') for x in turns if x.get('role') == 'semantic_review'), None),
            'semantic_review_reasoning': next((x.get('reasoning') for x in turns if x.get('role') == 'semantic_review'), None),
            'semantic_review_turns': sum(x.get('role') == 'semantic_review' for x in turns),
            'astra_review_used': any(x.get('role') == 'astra_review' for x in turns),
            'astra_review_turns': sum(x.get('role') == 'astra_review' for x in turns),
            'astra_trigger': next((x.get('trigger') for x in turns if x.get('role') == 'astra_review'), None),
            'acceptance_pass': sum(x.get('status') == 'PASS' for x in acceptance),
            'acceptance_fail': sum(x.get('status') == 'FAIL' for x in acceptance),
            'acceptance_unknown': sum(x.get('status') in ('UNKNOWN', 'NOT_TESTED') for x in acceptance),
            'review_result': final}}
