"""Small, local Project Memory built from canonical state and artifact references.

The Git checkout, MegaProg task state, reports, logs and commits remain the
source of truth. This module stores a compact projection and append-only
references; it never copies raw model output or logs into memory.
"""

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .storage import read, write

SCHEMA_VERSION = 1
MEMORY_REL = Path('.ai-dev') / 'memory'
LOGS = ('decisions.jsonl', 'events.jsonl', 'failures.jsonl')
CURRENT_FIELDS = {'active_invariants', 'next_allowed_actions'}
DELTA_FIELDS = {'schema_version', 'new_events', 'new_decisions', 'new_failures',
                'resolved_blockers', 'new_blockers', 'current_state_patch'}
RECORD_REQUIRED = {
    'events': {'event_id', 'timestamp', 'project_id', 'task_id', 'stage_id',
               'session_id', 'actor_role', 'type', 'commit', 'artifact_refs', 'summary'},
    'decisions': {'decision_id', 'timestamp', 'project_id', 'task_id', 'stage_id',
                  'session_id', 'commit', 'decision', 'rationale', 'evidence_refs',
                  'alternatives_considered', 'status', 'supersedes'},
    'failures': {'failure_id', 'timestamp', 'project_id', 'task_id', 'stage_id',
                 'session_id', 'commit', 'error_class', 'error_signature', 'command',
                 'affected_paths', 'attempt', 'resolution_status', 'artifact_refs'},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _project_id(root):
    """A local stable ID; deliberately contains no path or repository content."""
    canonical = str(Path(root).resolve())
    return hashlib.sha256(('megaprog-project-v1:' + canonical).encode('utf-8')).hexdigest()


def _git(root, *args):
    try:
        value = subprocess.check_output(['git'] + list(args), cwd=str(root),
                                        stderr=subprocess.DEVNULL, text=True).strip()
        return value or None
    except (OSError, subprocess.SubprocessError):
        return None


def _safe_text(value, limit=500):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())[:limit]


def _ref(root, value):
    if not value:
        return None
    path = Path(str(value))
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return str(value)[:1000]


def _read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    with path.open('r', encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line.encode('utf-8')) > 65536:
                raise ValueError('%s:%d exceeds the memory record size limit.' % (path.name, number))
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ValueError('%s:%d contains malformed JSON.' % (path.name, number)) from exc
            kind = path.stem
            _validate_record(kind, item)
            rows.append(item)
    return rows


def _validate_record(kind, record):
    if kind not in RECORD_REQUIRED or not isinstance(record, dict):
        raise ValueError('Malformed Project Memory record.')
    if type(record.get('schema_version')) is not int:
        raise ValueError('Memory record has no supported schema_version.')
    if record['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported Project Memory schema_version: %s.' % record['schema_version'])
    missing = RECORD_REQUIRED[kind] - set(record)
    if missing:
        raise ValueError('Memory %s entry is missing fields: %s.' % (kind, ', '.join(sorted(missing))))
    id_key = {'events': 'event_id', 'decisions': 'decision_id', 'failures': 'failure_id'}[kind]
    for field in (id_key, 'timestamp', 'project_id'):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError('Memory %s entry has invalid %s.' % (kind, field))
    for field in ('task_id', 'stage_id', 'session_id', 'commit'):
        value = record[field]
        if value is not None and (not isinstance(value, str) or len(value) > 1000):
            raise ValueError('Memory %s entry has invalid %s.' % (kind, field))
    if kind in ('events', 'failures') and not isinstance(record.get('artifact_refs'), list):
        raise ValueError('Memory artifact_refs must be an array.')
    ref_field = 'artifact_refs' if kind in ('events', 'failures') else 'evidence_refs'
    if kind in ('events', 'failures', 'decisions'):
        refs = record.get(ref_field)
        if (not isinstance(refs, list) or len(refs) > 64 or
                any(not isinstance(item, str) or len(item) > 1000 for item in refs)):
            raise ValueError('Memory %s must contain at most 64 short artifact references.' % ref_field)
    if kind == 'events' and not isinstance(record.get('summary'), (str, type(None))):
        raise ValueError('Memory event summary must be text or null.')
    if kind == 'events' and isinstance(record.get('summary'), str) and len(record['summary']) > 1000:
        raise ValueError('Memory event summary is too long.')
    if kind == 'decisions':
        for field in ('evidence_refs', 'alternatives_considered'):
            values = record.get(field)
            if (not isinstance(values, list) or len(values) > 64 or
                    any(not isinstance(item, str) or len(item) > 1000 for item in values)):
                raise ValueError('Memory decision %s must be an array.' % field)
        for field in ('decision', 'rationale', 'status'):
            if not isinstance(record.get(field), str) or not record[field].strip() or len(record[field]) > 4000:
                raise ValueError('Memory decision %s is invalid.' % field)
    if kind == 'failures':
        for field in ('error_class', 'error_signature', 'resolution_status'):
            if not isinstance(record.get(field), str) or not record[field].strip() or len(record[field]) > 1000:
                raise ValueError('Memory failure %s is invalid.' % field)
        paths = record.get('affected_paths')
        if not isinstance(paths, list) or len(paths) > 64 or any(
                not isinstance(item, str) or len(item) > 1000 for item in paths):
            raise ValueError('Memory failure affected_paths is invalid.')
        command = record.get('command')
        if command is not None and not isinstance(command, (str, list)):
            raise ValueError('Memory failure command is invalid.')
        if isinstance(command, list) and (len(command) > 100 or any(
                not isinstance(item, str) or len(item) > 1000 for item in command)):
            raise ValueError('Memory failure command is invalid.')
    if kind == 'failures':
        if type(record.get('attempt')) is not int or record['attempt'] < 0:
            raise ValueError('Memory failure attempt must be a non-negative integer.')
    if len(json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 65535:
        raise ValueError('Project Memory record exceeds 65535 bytes.')


def _same_record(left, right):
    comparable_left = dict(left)
    comparable_right = dict(right)
    comparable_left.pop('timestamp', None)
    comparable_right.pop('timestamp', None)
    return comparable_left == comparable_right


def _empty_current(root):
    return {'schema_version': SCHEMA_VERSION, 'project_id': _project_id(root),
            'repo_head': _git(root, 'rev-parse', 'HEAD'),
            'branch': _git(root, 'branch', '--show-current'), 'active_task': None,
            'active_stage': None, 'plan_id': None, 'status': 'IDLE', 'blockers': [],
            'user_blockers': [],
            'active_invariants': [], 'recent_decisions': [],
            'next_allowed_actions': ['Запустить или продолжить разрешённую задачу'],
            'updated_at': _now()}


def initialize(root):
    """Create empty v1 memory files without overwriting existing history."""
    root = Path(root).resolve()
    directory = root / MEMORY_REL
    directory.mkdir(parents=True, exist_ok=True)
    current_path = directory / 'current.json'
    if not current_path.exists():
        write(current_path, _empty_current(root))
    for name in LOGS:
        path = directory / name
        if not path.exists():
            with path.open('x', encoding='utf-8'):
                pass
    return directory


def read_current(root):
    path = Path(root) / MEMORY_REL / 'current.json'
    if not path.exists():
        return _empty_current(root)
    data = read(path)
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int:
        raise ValueError('Malformed Project Memory current state.')
    if data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported Project Memory current schema_version: %s.' % data['schema_version'])
    required = {'project_id', 'repo_head', 'branch', 'active_task', 'active_stage',
                'plan_id', 'status', 'blockers', 'active_invariants', 'recent_decisions',
                'next_allowed_actions', 'updated_at'}
    if required - set(data):
        raise ValueError('Project Memory current state is missing required fields.')
    for field in ('blockers', 'active_invariants', 'recent_decisions', 'next_allowed_actions'):
        if not isinstance(data[field], list):
            raise ValueError('Project Memory current %s must be an array.' % field)
    if data['project_id'] != _project_id(root):
        raise ValueError('Project Memory belongs to a different local project.')
    return data


def _active_stage(state):
    plan = state.get('plan')
    if isinstance(plan, dict) and plan.get('schema') == 2:
        steps = plan.get('steps') or []
        index = state.get('checkpoint_index', 0)
        if type(index) is int and 0 <= index < len(steps) and isinstance(steps[index], dict):
            return steps[index].get('id')
    if state.get('planning_attempts') and not plan:
        return 'planning'
    if plan:
        return 'legacy-stage-1'
    return None


def project_current(root, state=None, previous=None):
    """Build a compact projection; task state and Git remain canonical."""
    root = Path(root).resolve()
    previous = previous or _empty_current(root)
    state = state if isinstance(state, dict) else {}
    status = _safe_text(state.get('status'), 40) or 'IDLE'
    task_id = state.get('id')
    is_active = status not in ('COMPLETED', 'CANCELLED', 'IDLE')
    plan = state.get('plan')
    plan_id = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode('utf-8')).hexdigest() if plan else None
    user_blockers = previous.get('user_blockers', [])
    if not isinstance(user_blockers, list):
        user_blockers = []
    user_blockers = [_safe_text(item, 500) for item in user_blockers[:30]
                     if isinstance(item, str) and _safe_text(item, 500)]
    blockers = list(user_blockers)
    if status in ('BLOCKED', 'PENDING_APPROVAL'):
        blocker = _safe_text(state.get('reason') or state.get('last_failure'), 500)
        if blocker:
            blockers.append(blocker)
    progress = state.get('progress') if isinstance(state.get('progress'), dict) else {}
    next_actions = progress.get('next_step') or ('Просмотреть отчёт' if status == 'COMPLETED'
                                                  else 'Продолжить разрешённую задачу')
    if not isinstance(next_actions, list):
        next_actions = [next_actions]
    invariants = state.get('active_invariants', previous.get('active_invariants', []))
    if not isinstance(invariants, list):
        invariants = []
    invariants = [_safe_text(item, 500) for item in invariants[:30]
                  if isinstance(item, str) and _safe_text(item, 500)]
    task_id = _safe_text(task_id, 200) if task_id is not None else None
    recent_decisions = previous.get('recent_decisions', [])
    if not isinstance(recent_decisions, list):
        recent_decisions = []
    return {'schema_version': SCHEMA_VERSION, 'project_id': _project_id(root),
            'repo_head': _git(root, 'rev-parse', 'HEAD'),
            'branch': _git(root, 'branch', '--show-current'),
            'active_task': task_id if is_active else None,
            'active_stage': _active_stage(state) if is_active else None,
            'plan_id': plan_id if is_active else None, 'status': status,
            'blockers': blockers, 'user_blockers': user_blockers,
            'active_invariants': invariants,
            'recent_decisions': [_safe_text(item, 200) for item in recent_decisions[-10:]
                                 if isinstance(item, str)],
            'next_allowed_actions': [_safe_text(item, 500) for item in next_actions[:10]
                                    if _safe_text(item, 500)],
            'updated_at': _now()}


def _append(root, kind, record):
    _validate_record(kind, record)
    path = Path(root) / MEMORY_REL / (kind + '.jsonl')
    existing = _read_jsonl(path)
    id_key = {'events': 'event_id', 'decisions': 'decision_id', 'failures': 'failure_id'}[kind]
    for item in existing:
        if item[id_key] == record[id_key]:
            if _same_record(item, record):
                return False
            raise ValueError('Conflicting duplicate Project Memory id: ' + record[id_key])
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(payload.encode('utf-8')) > 65535:
        raise ValueError('Project Memory record exceeds 65535 bytes.')
    with path.open('a', encoding='utf-8') as stream:
        stream.write(payload + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return True


def append_event(root, **values):
    initialize(root)
    record = {'schema_version': SCHEMA_VERSION, 'event_id': uuid.uuid4().hex,
              'timestamp': _now(), **values}
    _append(root, 'events', record)
    return record


def append_decision(root, **values):
    initialize(root)
    record = {'schema_version': SCHEMA_VERSION, 'decision_id': uuid.uuid4().hex,
              'timestamp': _now(), **values}
    _append(root, 'decisions', record)
    return record


def append_failure(root, **values):
    initialize(root)
    record = {'schema_version': SCHEMA_VERSION, 'failure_id': uuid.uuid4().hex,
              'timestamp': _now(), **values}
    _append(root, 'failures', record)
    return record


def _validate_delta(root, delta):
    if not isinstance(delta, dict) or set(delta) != DELTA_FIELDS:
        raise ValueError('MemoryDelta must contain exactly the versioned contract fields.')
    if type(delta.get('schema_version')) is not int or delta['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported MemoryDelta schema_version.')
    for field in ('new_events', 'new_decisions', 'new_failures', 'resolved_blockers', 'new_blockers'):
        if not isinstance(delta[field], list):
            raise ValueError('MemoryDelta %s must be an array.' % field)
    patch = delta['current_state_patch']
    if not isinstance(patch, dict) or set(patch) - CURRENT_FIELDS:
        raise ValueError('MemoryDelta current_state_patch contains non-editable fields.')
    for field in ('active_invariants', 'next_allowed_actions'):
        if field in patch and (not isinstance(patch[field], list) or
                               any(not isinstance(item, str) or len(item) > 500 for item in patch[field])):
            raise ValueError('MemoryDelta %s must be an array of short strings.' % field)
    for field in ('resolved_blockers', 'new_blockers'):
        if any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in delta[field]):
            raise ValueError('MemoryDelta %s must contain short non-empty strings.' % field)
    for kind, field in (('events', 'new_events'), ('decisions', 'new_decisions'),
                        ('failures', 'new_failures')):
        seen = {}
        id_key = {'events': 'event_id', 'decisions': 'decision_id',
                  'failures': 'failure_id'}[kind]
        for item in delta[field]:
            _validate_record(kind, item)
            if item.get('project_id') != _project_id(root):
                raise ValueError('MemoryDelta record belongs to a different project.')
            record_id = item[id_key]
            if record_id in seen and not _same_record(seen[record_id], item):
                raise ValueError('MemoryDelta contains conflicting duplicate ids.')
            seen[record_id] = item


def apply_delta(root, delta):
    """Validate a complete proposal before deterministic, idempotent application."""
    _validate_delta(root, delta)
    initialize(root)
    # Preflight all existing journals before writing any; malformed history fails closed.
    for name in LOGS:
        _read_jsonl(Path(root) / MEMORY_REL / name)
    records_by_kind = [('events', 'new_events'), ('decisions', 'new_decisions'),
                       ('failures', 'new_failures')]
    # Detect conflicts against all existing rows before any append, so a bad
    # later record cannot leave an otherwise invalid delta partially applied.
    for kind, field in records_by_kind:
        id_key = {'events': 'event_id', 'decisions': 'decision_id',
                  'failures': 'failure_id'}[kind]
        existing = {item[id_key]: item for item in _read_jsonl(Path(root) / MEMORY_REL / (kind + '.jsonl'))}
        for record in delta[field]:
            prior = existing.get(record[id_key])
            if prior is not None and not _same_record(prior, record):
                raise ValueError('Conflicting duplicate Project Memory id: ' + record[id_key])
    for kind, field in records_by_kind:
        for record in delta[field]:
            _append(root, kind, record)
    current = read_current(root)
    blockers = [item for item in current.get('user_blockers', [])
                if item not in delta['resolved_blockers']]
    for item in delta['new_blockers']:
        if item not in blockers:
            blockers.append(item)
    current['user_blockers'] = blockers
    task_blockers = [item for item in current.get('blockers', [])
                     if item not in current.get('user_blockers', [])]
    current['blockers'] = blockers + task_blockers
    current.update(delta['current_state_patch'])
    current['updated_at'] = _now()
    write(Path(root) / MEMORY_REL / 'current.json', current)
    return current


def bootstrap_optimization_roadmap(root):
    """Seed only this canonical MegaProg checkout with accepted milestone refs."""
    root = Path(root).resolve()
    directory = initialize(root)
    project_id = _project_id(root)
    milestones = [
        ('milestone-1-planner-pin', 'Milestone 1: honor the user-pinned model and reasoning for planning; preserve explicit handoff.',
         'c3f570f', ['ai_dev/supervisor.py', 'ai_dev/plan_handoff.py', 'tests/test_plan_first.py']),
        ('milestone-1-role-scope', 'Milestone 1: scope explicit model pins by role so planner selection does not silently pin workers.',
         'b50951c', ['ai_dev/supervisor.py', 'ai_dev/cli.py']),
        ('milestone-2-turn-telemetry', 'Milestone 2: persist available per-turn Codex telemetry and keep unavailable observed fields unknown.',
         'db08461', ['ai_dev/telemetry.py', 'tests/test_turn_telemetry.py']),
        ('milestone-3-plan-format', 'Milestone 3: normalize only deterministic planner serialization defects and distinguish format from semantic rejection.',
         '8b2e6a3', ['ai_dev/planning.py', 'tests/test_plan_normalization.py']),
    ]
    for decision_id, decision, commit, refs in milestones:
        _append(root, 'decisions', {
            'schema_version': SCHEMA_VERSION, 'decision_id': decision_id,
            'timestamp': _now(), 'project_id': project_id, 'task_id': None,
            'stage_id': decision_id.split('-')[1] + '-' + decision_id.split('-')[2],
            'session_id': None, 'commit': commit, 'decision': decision,
            'rationale': 'Пользователь принял milestone как завершённый; запись ссылается на Git и исходные проверки.',
            'evidence_refs': ['git:' + commit] + refs,
            'alternatives_considered': [], 'status': 'accepted', 'supersedes': None})
        _append(root, 'events', {
            'schema_version': SCHEMA_VERSION, 'event_id': 'bootstrap-' + decision_id,
            'timestamp': _now(), 'project_id': project_id, 'task_id': None,
            'stage_id': decision_id.split('-')[1] + '-' + decision_id.split('-')[2],
            'session_id': None, 'actor_role': 'coordinator', 'type': 'milestone_completed',
            'commit': commit, 'artifact_refs': ['git:' + commit] + refs,
            'summary': decision[:240]})
    debt_id = 'debt-contract-validation-error-class'
    _append(root, 'decisions', {
        'schema_version': SCHEMA_VERSION, 'decision_id': debt_id,
        'timestamp': _now(), 'project_id': project_id, 'task_id': None,
        'stage_id': 'milestone-3', 'session_id': None, 'commit': '8b2e6a3',
        'decision': 'Отложить отдельный класс CONTRACT_VALIDATION_ERROR для валидных полей, превышающих локальные ограничения.',
        'rationale': 'Принято как low-priority technical debt; Milestone 3 не перестраивать.',
        'evidence_refs': ['git:8b2e6a3', 'ai_dev/planning.py', 'tests/test_plan_normalization.py'],
        'alternatives_considered': ['Оставить текущий FORMAT_ERROR до отдельного изменения классификации.'],
        'status': 'deferred_low_priority', 'supersedes': None})
    _append(root, 'events', {
        'schema_version': SCHEMA_VERSION, 'event_id': 'bootstrap-' + debt_id,
        'timestamp': _now(), 'project_id': project_id, 'task_id': None,
        'stage_id': 'milestone-3', 'session_id': None, 'actor_role': 'coordinator',
        'type': 'technical_debt_recorded', 'commit': '8b2e6a3',
        'artifact_refs': ['git:8b2e6a3', 'ai_dev/planning.py'],
        'summary': 'Отдельный CONTRACT_VALIDATION_ERROR — низкоприоритетный долг на будущее.'})
    _append(root, 'events', {
        'schema_version': SCHEMA_VERSION, 'event_id': 'bootstrap-milestone-4-started',
        'timestamp': _now(), 'project_id': project_id, 'task_id': None,
        'stage_id': 'milestone-4', 'session_id': None, 'actor_role': 'coordinator',
        'type': 'milestone_started', 'commit': _git(root, 'rev-parse', 'HEAD'),
        'artifact_refs': ['git:8b2e6a3'],
        'summary': 'Создание компактной локальной Project Memory foundation.'})
    current = read_current(root)
    current.update(repo_head=_git(root, 'rev-parse', 'HEAD'),
                   branch=_git(root, 'branch', '--show-current'),
                   active_stage='milestone-4', status='IN_PROGRESS',
                   recent_decisions=[item[0] for item in milestones] + [debt_id],
                   next_allowed_actions=['Завершить Project Memory foundation и проверить его тестами'],
                   updated_at=_now())
    write(directory / 'current.json', current)
    return current


def complete_milestone_4(root):
    """Record M4 only after its verified implementation commit exists."""
    root = Path(root).resolve()
    directory = initialize(root)
    current = read_current(root)
    project_id = current['project_id']
    commit = _git(root, 'rev-parse', 'HEAD')
    decision_base = 'milestone-4-project-memory'
    prior_decisions = [item for item in _read_jsonl(directory / 'decisions.jsonl')
                       if item['decision_id'] == decision_base or
                       item['decision_id'].startswith(decision_base + '-')]
    prior = prior_decisions[-1] if prior_decisions else None
    if prior and prior.get('commit') == commit:
        decision_id = prior['decision_id']
    elif prior and commit:
        decision_id = decision_base + '-' + commit[:12]
    else:
        decision_id = decision_base
    event_id = 'bootstrap-milestone-4-completed-' + (commit[:12] if commit else 'unknown')
    refs = ['ai_dev/project_memory.py', 'docs/PROJECT_MEMORY.md',
            'tests/test_project_memory.py']
    _append(root, 'decisions', {
        'schema_version': SCHEMA_VERSION, 'decision_id': decision_id,
        'timestamp': _now(), 'project_id': project_id, 'task_id': None,
        'stage_id': 'milestone-4', 'session_id': None, 'commit': commit,
        'decision': 'Project Memory v1 хранит компактное текущее состояние и append-only ссылки на события, решения и сбои.',
        'rationale': 'Реализовано и проверено; Git, task state, reports и raw artifacts остаются источниками истины.',
        'evidence_refs': (['git:' + commit] if commit else []) + refs,
        'alternatives_considered': ['Отложить память до semantic retrieval; это было вне цели milestone.'],
        'status': 'accepted',
        'supersedes': prior['decision_id'] if prior and prior['decision_id'] != decision_id else None})
    _append(root, 'events', {
        'schema_version': SCHEMA_VERSION, 'event_id': event_id,
        'timestamp': _now(), 'project_id': project_id, 'task_id': None,
        'stage_id': 'milestone-4', 'session_id': None, 'actor_role': 'coordinator',
        'type': 'milestone_completed', 'commit': commit,
        'artifact_refs': (['git:' + commit] if commit else []) + refs,
        'summary': 'Project Memory foundation завершён и проверен.'})
    current.update(repo_head=commit, branch=_git(root, 'branch', '--show-current'),
                   active_task=None, active_stage=None, plan_id=None,
                   status='COMPLETED', blockers=[],
                   recent_decisions=([item for item in current.get('recent_decisions', [])
                                      if not str(item).startswith(decision_base)] +
                                     [decision_id])[-10:],
                   next_allowed_actions=['Просмотреть результат Milestone 4'], updated_at=_now())
    write(directory / 'current.json', current)
    return current


def record_commit(root, state):
    """Link a user-approved commit to its task and refresh the current projection."""
    root = Path(root).resolve()
    directory = initialize(root)
    current = read_current(root)
    commit = _git(root, 'rev-parse', 'HEAD')
    task_id = _safe_text(state.get('id'), 200)
    event_id = 'commit-' + (commit or hashlib.sha256(str(task_id).encode('utf-8')).hexdigest())
    _append(root, 'events', {
        'schema_version': SCHEMA_VERSION, 'event_id': event_id, 'timestamp': _now(),
        'project_id': current['project_id'], 'task_id': task_id,
        'stage_id': _active_stage(state), 'session_id': state.get('session_id'),
        'actor_role': 'coordinator', 'type': 'commit_created', 'commit': commit,
        'artifact_refs': ['runs/%s/state.json' % task_id, 'runs/%s/report.json' % task_id],
        'summary': 'Пользовательски разрешённый commit задачи MegaProg.'})
    updated = project_current(root, state, current)
    updated['repo_head'] = commit
    write(directory / 'current.json', updated)
    return updated


def record_task_state(root, state, status, fields=None):
    """Persist a compact status event/current projection after supervisor saves."""
    root = Path(root).resolve()
    directory = initialize(root)
    previous = read_current(root)
    current = project_current(root, state, previous)
    status_map = {'PREFLIGHT': 'task_state_updated', 'RUNNING': 'stage_started',
                  'VERIFY': 'stage_verification_started', 'RETRY': 'stage_progressed',
                  'COMPLETED': 'task_completed', 'BLOCKED': 'task_blocked',
                  'PENDING_APPROVAL': 'approval_requested', 'CANCELLED': 'task_cancelled'}
    task_id = str(state.get('id') or '')
    history = state.get('checkpoint_history') or []
    completed_stage = history[-1] if history and isinstance(history[-1], dict) else {}
    artifacts = []
    if task_id:
        artifacts.append('runs/%s/state.json' % task_id)
        if status in ('COMPLETED', 'BLOCKED'):
            artifacts.append('runs/%s/report.json' % task_id)
        attempt = state.get('attempts', 0)
        if isinstance(attempt, int) and attempt > 0:
            artifacts.append('runs/%s/%s/codex.jsonl' % (task_id, attempt))
    session_id = state.get('session_id')
    event_type = status_map.get(status, 'task_state_updated')
    if status == 'PREFLIGHT' and not previous.get('active_task'):
        event_type = 'task_created'
    plan_id = current.get('plan_id')
    if plan_id and plan_id != previous.get('plan_id'):
        event_type = 'plan_accepted'
    reason = _safe_text((fields or {}).get('reason'), 500)
    stage_finished = bool(completed_stage) and (
        status == 'COMPLETED' or (status == 'RETRY' and reason.startswith('Этап проверен')))
    event_stage_id = (completed_stage.get('id') if stage_finished else _active_stage(state))
    event_session_id = (completed_stage.get('session_id') if stage_finished else session_id)
    if stage_finished and status == 'RETRY':
        event_type = 'stage_completed'
    append_event(root, project_id=current['project_id'], task_id=task_id or None,
                 stage_id=event_stage_id, session_id=event_session_id,
                 actor_role='planner' if event_stage_id == 'planning' else 'worker',
                 type=event_type, commit=current['repo_head'], artifact_refs=artifacts,
                 summary=_safe_text((fields or {}).get('reason') or status, 240) or None)
    if plan_id and plan_id != previous.get('plan_id'):
        plan_index = len(state.get('planning_attempts') or [])
        plan_ref = 'runs/%s/plan-%d/codex.jsonl' % (task_id, plan_index) if plan_index else None
        refs = [item for item in (plan_ref, 'runs/%s/state.json' % task_id,
                                  'runs/%s/report.json' % task_id) if item]
        plan_hash = plan_id[:16]
        _append(root, 'decisions', {
            'schema_version': SCHEMA_VERSION,
            'decision_id': 'accepted-plan-%s-%s' % (task_id, plan_hash),
            'timestamp': _now(), 'project_id': current['project_id'],
            'task_id': task_id or None, 'stage_id': 'planning',
            'session_id': ((state.get('planning_attempts') or [{}])[-1].get('codex') or {}).get('session_id'),
            'commit': current['repo_head'], 'decision': 'Принят план %s.' % plan_hash,
            'rationale': 'План прошёл детерминированные проверки формата и контрактной структуры.',
            'evidence_refs': refs, 'alternatives_considered': [], 'status': 'accepted',
            'supersedes': None})
        current['recent_decisions'] = (previous.get('recent_decisions', []) +
                                       ['accepted-plan-%s-%s' % (task_id, plan_hash)])[-10:]
    if ((status == 'BLOCKED' and not state.get('anti_loop_failure_recorded')) or
            ((state.get('verification') or {}).get('ok') is False and status == 'RETRY'
             and not state.get('anti_loop_enabled'))):
        verification = state.get('verification') or {}
        failed_check = next((item for item in verification.get('checks', [])
                             if isinstance(item, dict) and item.get('exit_code') not in (None, 0)), {})
        error_class = _safe_text(state.get('last_failure') or ('verification' if failed_check else 'blocked'), 100)
        signature_source = '|'.join((error_class, _safe_text(failed_check.get('command'), 300),
                                     _safe_text(failed_check.get('tail'), 300),
                                     str(state.get('checkpoint_index', 0))))
        failure_signature = hashlib.sha256(signature_source.encode('utf-8')).hexdigest()
        failure_id = hashlib.sha256(('%s|%s|%s|%s|%s' % (
            current['project_id'], task_id, _active_stage(state), state.get('attempts', 0),
            failure_signature)).encode('utf-8')).hexdigest()[:32]
        attempt_no = state.get('attempts', 0)
        if type(attempt_no) is not int or attempt_no < 0:
            attempt_no = 0
        paths = []
        diff_names = _git(root, 'diff', '--name-only') or ''
        for name in diff_names.splitlines():
            paths.append(name[:500])
        append_failure(root, failure_id=failure_id, project_id=current['project_id'],
                       task_id=task_id or None, stage_id=_active_stage(state), session_id=session_id,
                       commit=current['repo_head'], error_class=error_class,
                       error_signature=failure_signature,
                       command=failed_check.get('command'), affected_paths=paths[:40],
                       attempt=attempt_no, resolution_status='unresolved',
                       artifact_refs=artifacts + ['runs/%s/progress.jsonl' % task_id])
    write(directory / 'current.json', current)
    return current
