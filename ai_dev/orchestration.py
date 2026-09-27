"""Durable, local-only orchestration envelopes.

The roles are protocol identities, not background agents.  Every transition is
explicit, serialized by the project lock, and verifier-gated.  This module does
not start Codex, create threads, use a network, or grant a worker access to
another project's files.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .storage import lock, read, write
from .process import run
from .version import __version__

SCHEMA = 1
ROLES = ('chief', 'assistant', 'worker', 'verifier')
TASK_STATES = ('PENDING', 'CLAIMED', 'CHECKPOINTED', 'VERIFYING', 'COMPLETED', 'BLOCKED')
MAX_TEXT = 8192
MAX_FILES = 64
MAX_CHECKS = 64
APPROVAL_SCOPES = ('model_turn', 'expensive_escalation', 'commit', 'release')


def _now():
    return time.time()


def _base(root):
    return Path(root) / '.ai-dev' / 'orchestration'


def _paths(root):
    base = _base(root)
    return {'base': base, 'plans': base / 'plans', 'tasks': base / 'tasks',
            'checkpoints': base / 'checkpoints', 'ownership': base / 'ownership',
            'reports': base / 'reports', 'registry': base / 'registry.json',
            'approvals': base / 'approvals.json'}


def _id(value, field):
    if value is None:
        return field + '-' + uuid.uuid4().hex[:20]
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', value):
        raise ValueError('%s is unsafe' % field)
    return value


def _text(value, field, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s must be non-empty' % field)
    if len(value.encode('utf-8')) > limit:
        raise ValueError('%s exceeds %d UTF-8 bytes' % (field, limit))
    return value.strip()


def _role(value):
    if value not in ROLES:
        raise ValueError('role must be one of: ' + ', '.join(ROLES))
    return value


def _files(values):
    if not isinstance(values, list) or not values or len(values) > MAX_FILES:
        raise ValueError('files_allowed must be a bounded non-empty list')
    result = []
    for value in values:
        path = Path(value) if isinstance(value, str) else None
        if path is None or not value.strip() or path.is_absolute() or '..' in path.parts:
            raise ValueError('files_allowed contains an unsafe path')
        result.append(value.strip())
    return result


def _checks(values):
    if not isinstance(values, list) or len(values) > MAX_CHECKS:
        raise ValueError('checks must be a bounded list')
    result = []
    for value in values:
        if isinstance(value, str):
            result.append({'name': _text(value, 'check', 512), 'status': 'PENDING'})
        elif isinstance(value, dict):
            name = _text(value.get('name'), 'check.name', 512)
            status = value.get('status', 'PENDING')
            if status not in ('PENDING', 'PASS', 'FAIL', 'UNKNOWN'):
                raise ValueError('check status is invalid')
            result.append({'name': name, 'status': status, 'detail': str(value.get('detail', ''))[:1024]})
        else:
            raise ValueError('check must be text or an object')
    return result


def _write_dirs(paths):
    for key, directory in paths.items():
        if key not in ('base', 'registry', 'approvals'):
            directory.mkdir(parents=True, exist_ok=True)


def _project_id(root):
    return hashlib.sha256(str(Path(root).resolve()).encode('utf-8')).hexdigest()[:16]


def create_plan(root, objective, files_allowed, verify_commands, plan_id=None):
    objective = _text(objective, 'objective')
    files_allowed = _files(files_allowed)
    if not isinstance(verify_commands, list) or not verify_commands:
        raise ValueError('verify_commands must be a non-empty list')
    commands = []
    for command in verify_commands:
        if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError('verify_commands must contain argument lists')
        commands.append(command[:32])
    plan = {'schema': SCHEMA, 'plan_id': _id(plan_id, 'plan'),
            'project_id': _project_id(root), 'objective': objective,
            'files_allowed': files_allowed, 'verify_commands': commands,
            'roles': {role: {'role': role, 'status': 'READY'} for role in ROLES},
            'status': 'READY', 'priority': 50, 'next_step': 'claim a PENDING task',
            'approval_gates': {scope: 'PENDING_APPROVAL' for scope in APPROVAL_SCOPES},
            'created_at': _now(), 'updated_at': _now()}
    paths = _paths(root)
    with lock(Path(root)):
        _write_dirs(paths)
        write(paths['plans'] / (plan['plan_id'] + '.json'), plan)
        _registry_update_locked(root, plan['plan_id'], None, plan['next_step'], plan['priority'], plan['status'])
    return plan


def _load_plan(root, plan_id):
    path = _paths(root)['plans'] / (_id(plan_id, 'plan') + '.json')
    if not path.exists():
        raise ValueError('plan not found: %s' % plan_id)
    plan = read(path)
    if plan.get('schema') != SCHEMA or plan.get('project_id') != _project_id(root):
        raise ValueError('plan envelope is invalid for this project')
    return plan


def create_task(root, plan_id, prompt, files_allowed=None, task_id=None, owner_role='worker'):
    plan = _load_plan(root, plan_id)
    owner_role = _role(owner_role)
    prompt = _text(prompt, 'prompt')
    allowed = _files(files_allowed or plan['files_allowed'])
    if not set(allowed).issubset(set(plan['files_allowed'])):
        raise ValueError('task files_allowed must be within plan allow-list')
    task = {'schema': SCHEMA, 'task_id': _id(task_id, 'task'), 'plan_id': plan['plan_id'],
            'project_id': plan['project_id'], 'prompt': prompt, 'files_allowed': allowed,
            'owner_role': owner_role, 'owner_id': None, 'status': 'PENDING',
            'checkpoint_id': None, 'verification': None, 'created_at': _now(), 'updated_at': _now()}
    paths = _paths(root)
    with lock(Path(root)):
        _write_dirs(paths)
        write(paths['tasks'] / (task['task_id'] + '.json'), task)
    return task


def _load_task(root, task_id):
    path = _paths(root)['tasks'] / (_id(task_id, 'task') + '.json')
    if not path.exists():
        raise ValueError('task not found: %s' % task_id)
    task = read(path)
    if task.get('schema') != SCHEMA or task.get('project_id') != _project_id(root):
        raise ValueError('task envelope is invalid for this project')
    return task


def claim_task(root, task_id, worker_id):
    worker_id = _id(worker_id, 'worker_id')
    paths = _paths(root)
    with lock(Path(root)):
        task = _load_task(root, task_id)
        if task['status'] == 'CLAIMED' and task.get('owner_id') != worker_id:
            raise ValueError('task is owned by another worker')
        if task['status'] not in ('PENDING', 'CLAIMED'):
            raise ValueError('task cannot be claimed from %s' % task['status'])
        ownership = paths['ownership'] / (task['plan_id'] + '.json')
        current = read(ownership) if ownership.exists() else None
        if current and current.get('worker_id') != worker_id and current.get('status') == 'ACTIVE':
            raise ValueError('project already has an active worker')
        owner = {'schema': SCHEMA, 'project_id': task['project_id'], 'plan_id': task['plan_id'],
                 'worker_id': worker_id, 'task_id': task['task_id'], 'status': 'ACTIVE', 'updated_at': _now()}
        write(ownership, owner)
        task.update(status='CLAIMED', owner_id=worker_id, updated_at=_now())
        write(paths['tasks'] / (task['task_id'] + '.json'), task)
    return task


def checkpoint_task(root, task_id, worker_id, summary, files_changed, handoff_role='verifier'):
    worker_id = _id(worker_id, 'worker_id')
    _role(handoff_role)
    if handoff_role != 'verifier':
        raise ValueError('checkpoint handoff must go to verifier')
    summary = _text(summary, 'summary')
    files_changed = [] if files_changed is None else _files(files_changed)
    paths = _paths(root)
    with lock(Path(root)):
        task = _load_task(root, task_id)
        if task.get('owner_id') != worker_id or task['status'] != 'CLAIMED':
            raise ValueError('only the claiming worker may checkpoint a CLAIMED task')
        checkpoint = {'schema': SCHEMA, 'checkpoint_id': 'checkpoint-' + uuid.uuid4().hex[:20],
                      'task_id': task['task_id'], 'project_id': task['project_id'],
                      'worker_id': worker_id, 'summary': summary, 'files_changed': files_changed,
                      'handoff_role': handoff_role, 'created_at': _now()}
        write(paths['checkpoints'] / (checkpoint['checkpoint_id'] + '.json'), checkpoint)
        task.update(status='CHECKPOINTED', checkpoint_id=checkpoint['checkpoint_id'], updated_at=_now())
        write(paths['tasks'] / (task['task_id'] + '.json'), task)
        return checkpoint


def verify_task(root, task_id, verifier_id, checks=None, report=''):
    verifier_id = _id(verifier_id, 'verifier_id')
    if checks is not None:
        raise ValueError('manual checks are not accepted; verifier must execute plan commands')
    report = _text(report, 'report', 4096) if report else ''
    paths = _paths(root)
    with lock(Path(root)):
        task = _load_task(root, task_id)
        if task['status'] != 'CHECKPOINTED' or not task.get('checkpoint_id'):
            raise ValueError('verifier requires a worker checkpoint')
        plan = _load_plan(root, task['plan_id'])
        checks = []
        for command in plan['verify_commands']:
            try:
                code, output = run(command, root, timeout=300)
                checks.append({'name': ' '.join(command), 'status': 'PASS' if code == 0 else 'FAIL',
                               'exit_code': code, 'output_tail': output[-4000:]})
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                checks.append({'name': ' '.join(command), 'status': 'UNKNOWN',
                               'exit_code': None, 'output_tail': str(exc)[:4000]})
        status = 'COMPLETED' if checks and all(item['status'] == 'PASS' for item in checks) else 'BLOCKED'
        verification = {'schema': SCHEMA, 'verifier_id': verifier_id, 'checks': checks,
                        'report': report, 'status': status, 'verified_at': _now()}
        task.update(status=status, verification=verification, updated_at=_now())
        write(paths['tasks'] / (task['task_id'] + '.json'), task)
        ownership = paths['ownership'] / (task['plan_id'] + '.json')
        if ownership.exists():
            owner = read(ownership)
            owner.update(status='RELEASED', released_at=_now())
            write(ownership, owner)
        _registry_update_locked(root, task['plan_id'], task['task_id'],
                                'review completed verification' if status == 'COMPLETED' else 'inspect failed verification',
                                None, status)
        return task


def handoff_task(root, task_id, from_role, to_role):
    _role(from_role)
    _role(to_role)
    if from_role == to_role:
        raise ValueError('handoff roles must differ')
    paths = _paths(root)
    with lock(Path(root)):
        task = _load_task(root, task_id)
        if task['status'] in ('COMPLETED', 'BLOCKED'):
            raise ValueError('finished task cannot be handed off')
        task['handoff'] = {'from': from_role, 'to': to_role, 'at': _now()}
        task['updated_at'] = _now()
        write(paths['tasks'] / (task['task_id'] + '.json'), task)
    return task


def status(root, plan_id=None):
    paths = _paths(root)
    tasks = []
    if paths['tasks'].exists():
        for path in sorted(paths['tasks'].glob('*.json')):
            task = read(path)
            if plan_id is None or task.get('plan_id') == plan_id:
                tasks.append(task)
    return {'schema': SCHEMA, 'project_id': _project_id(root), 'plan_id': plan_id,
            'tasks': tasks, 'counts': {state: sum(item.get('status') == state for item in tasks)
                                      for state in TASK_STATES}}


def _registry_update_locked(root, plan_id, task_id, next_step, priority=None, status_value=None):
    paths = _paths(root)
    registry = read(paths['registry']) if paths['registry'].exists() else {
        'schema': SCHEMA, 'project_id': _project_id(root), 'version': __version__, 'projects': {}}
    project = registry['projects'].setdefault(plan_id, {'plan_id': plan_id, 'tasks': {}})
    if priority is not None:
        project['priority'] = priority
    if status_value is not None:
        project['status'] = status_value
    project['next_step'] = next_step
    project['updated_at'] = _now()
    if task_id:
        project['tasks'].setdefault(task_id, {})['status'] = status_value or 'ACTIVE'
        project['tasks'][task_id]['next_step'] = next_step
        project['tasks'][task_id]['updated_at'] = _now()
    write(paths['registry'], registry)


def registry_update(root, plan_id, task_id, next_step, priority=50, status_value='ACTIVE'):
    _text(next_step, 'next_step', 1024)
    with lock(Path(root)):
        _load_plan(root, plan_id)
        _write_dirs(_paths(root))
        _registry_update_locked(root, plan_id, task_id, next_step, priority, status_value)
        return read(_paths(root)['registry'])


def registry(root):
    path = _paths(root)['registry']
    return read(path) if path.exists() else {'schema': SCHEMA, 'project_id': _project_id(root), 'version': __version__, 'projects': {}}


def approve(root, scope, actor, plan_id=None, task_id=None):
    if scope not in APPROVAL_SCOPES:
        raise ValueError('unknown approval scope')
    actor = _id(actor, 'actor')
    with lock(Path(root)):
        path = _paths(root)['approvals']
        approvals = read(path) if path.exists() else {'schema': SCHEMA, 'approvals': []}
        approvals['approvals'].append({'scope': scope, 'actor': actor, 'plan_id': plan_id,
                                       'task_id': task_id, 'approved_at': _now()})
        write(path, approvals)
        return approvals['approvals'][-1]


def approval_status(root, scope, plan_id=None, task_id=None):
    path = _paths(root)['approvals']
    if not path.exists():
        return {'scope': scope, 'status': 'PENDING_APPROVAL'}
    approvals = read(path).get('approvals', [])
    for item in reversed(approvals):
        if item.get('scope') == scope and item.get('plan_id') == plan_id and item.get('task_id') == task_id:
            return dict(item, status='APPROVED')
    return {'scope': scope, 'status': 'PENDING_APPROVAL'}


def parity_check(root, manifest_path):
    manifest = read(Path(manifest_path))
    expected = manifest.get('expected', {})
    actions = set(manifest.get('cli_actions', []))
    source = Path(root) / 'ai_dev' / 'cli.py'
    if not source.exists():
        source = Path(__file__).resolve().parent / 'cli.py'
    text = source.read_text(encoding='utf-8')
    actual_actions = {match for match in re.findall(r"choices=\(([^)]*)\)", text)
                      for match in re.findall(r"'([a-z-]+)'", match)}
    checks = {
        'version': (__version__ == expected.get('version'), __version__),
        'orchestration_schema': (SCHEMA == expected.get('orchestration_schema'), SCHEMA),
        'roles': (list(ROLES) == expected.get('roles'), list(ROLES)),
        'cli_actions': (actions.issubset(actual_actions), sorted(actual_actions & actions)),
        'required_checks': (all(isinstance(item, str) and item for item in manifest.get('required_checks', [])), manifest.get('required_checks', [])),
    }
    return {'schema': SCHEMA, 'status': 'PASS' if all(item[0] for item in checks.values()) else 'BLOCKED',
            'checks': {key: {'status': 'PASS' if value[0] else 'BLOCKED', 'actual': value[1]}
                       for key, value in checks.items()}}


def handoff_report(root, plan_id=None):
    data = status(root, plan_id)
    try:
        from .usage_report import aggregate
        usage = aggregate(root)
        usage_summary = {'recorded_tokens': usage['totals']['recorded_tokens'],
                         'tasks': usage['totals']['tasks'], 'attempts': usage['totals']['attempts'],
                         'missing_usage': usage['missing_usage']}
    except (OSError, ValueError, KeyError, TypeError):
        usage_summary = {'status': 'UNKNOWN'}
    return {'schema': SCHEMA, 'project_id': data['project_id'], 'plan_id': plan_id,
            'platform': 'windows' if __import__('os').name == 'nt' else 'macos',
            'roles': list(ROLES), 'tasks': data['tasks'], 'counts': data['counts'],
            'registry': registry(root), 'approval_scopes': list(APPROVAL_SCOPES),
            'usage_summary': usage_summary,
            'automatic_threads': False, 'network': False,
            'next_step': ('run verifier for CHECKPOINTED tasks' if data['counts']['CHECKPOINTED']
                          else 'claim a PENDING task or resume an existing task')}
