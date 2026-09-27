"""Execute an owner-approved feature plan without a new planner turn per task.

The immutable plan is the requirement register.  State and complete model/test
logs are kept separately so a shorter worker context cannot erase obligations.
This first implementation is intentionally sequential.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import unicodedata
from pathlib import Path

from . import codex, routing, supervisor
from .account_guard import AccountGuardError, model_turn
from .discovery import discover
from .process import run as run_command
from .storage import lock, read, write

SCHEMA = 1
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')
MAX_PLAN_BYTES = 128 * 1024
MAX_CONTEXT_BYTES = 12 * 1024


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _name(value, label):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(label + ' must be a short safe ID')
    return value


def _text(value, label, limit=8192):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
        raise ValueError(label + ' must be non-empty and bounded')
    return value.strip()


def _path(value, label):
    if not isinstance(value, str) or not value.strip() or '\\' in value:
        raise ValueError(label + ' must be a repository-relative file path')
    path = Path(value)
    if (path.is_absolute() or any(part in ('', '.', '..') for part in path.parts) or
            path.parts[0] in ('.ai-dev', '.git')):
        raise ValueError(label + ' is outside the source allowlist')
    return path.as_posix()


def _commands(value, label):
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError(label + ' must contain 1–20 commands')
    result = []
    for command in value:
        if (not isinstance(command, list) or not 1 <= len(command) <= 32 or
                any(not isinstance(arg, str) or not arg or len(arg) > 2048 for arg in command)):
            raise ValueError(label + ' must contain bounded argv arrays')
        result.append(command)
    return result


def validate_plan(plan):
    """Validate the complete requirement register before accepting any work."""
    if not isinstance(plan, dict) or plan.get('schema_version') != SCHEMA:
        raise ValueError('approved plan schema_version must be 1')
    if len(_canonical(plan)) > MAX_PLAN_BYTES:
        raise ValueError('approved plan exceeds 128 KiB')
    _name(plan.get('plan_id'), 'plan_id')
    _text(plan.get('objective'), 'objective')
    budget = plan.get('budget')
    if not isinstance(budget, dict) or type(budget.get('max_model_turns')) is not int or not 1 <= budget['max_model_turns'] <= 100:
        raise ValueError('budget.max_model_turns must be 1–100')
    features, tasks = plan.get('features'), plan.get('tasks')
    if not isinstance(features, list) or not 1 <= len(features) <= 50:
        raise ValueError('features must contain 1–50 entries')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
        raise ValueError('tasks must contain 1–100 entries')
    feature_ids, task_ids = set(), set()
    for feature in features:
        if not isinstance(feature, dict):
            raise ValueError('feature must be an object')
        fid = _name(feature.get('id'), 'feature.id')
        if fid in feature_ids:
            raise ValueError('duplicate feature ID: ' + fid)
        feature_ids.add(fid)
        _text(feature.get('title'), 'feature.title', 512)
        criteria = feature.get('acceptance')
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
            raise ValueError('feature.acceptance must contain 1–20 criteria')
        for item in criteria:
            _text(item, 'feature.acceptance item', 2048)
        _commands(feature.get('checks'), 'feature.checks')
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError('task must be an object')
        tid = _name(task.get('id'), 'task.id')
        if tid in task_ids:
            raise ValueError('duplicate task ID: ' + tid)
        task_ids.add(tid)
        if task.get('feature_id') not in feature_ids:
            raise ValueError('task has unknown feature: ' + tid)
        _text(task.get('title'), 'task.title', 512)
        _text(task.get('instructions'), 'task.instructions')
        model = task.get('model', 'gpt-6-luna')
        if model not in ('gpt-6-luna', 'gpt-6-sol'):
            raise ValueError('first approved-plan mode supports Luna and Sol tasks only')
        routing.validate_model_turn(model, task.get('reasoning', 'medium'))
        paths = task.get('allowed_paths')
        if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
            raise ValueError('task.allowed_paths must contain 1–32 files')
        for path in paths:
            _path(path, 'task.allowed_paths')
        if len(set(paths)) != len(paths):
            raise ValueError('duplicate allowed path')
        refs = task.get('context_paths', [])
        if not isinstance(refs, list) or len(refs) > 8:
            raise ValueError('task.context_paths must contain at most 8 paths')
        for path in refs:
            _path(path, 'task.context_paths')
        _commands(task.get('checks'), 'task.checks')
    dependencies = {}
    for task in tasks:
        deps = task.get('depends_on', [])
        if (not isinstance(deps, list) or len(deps) > 32 or len(set(deps)) != len(deps) or
                any(dep not in task_ids or dep == task['id'] for dep in deps)):
            raise ValueError('task.depends_on contains a duplicate, self, or unknown ID')
        dependencies[task['id']] = deps
    visiting, visited = set(), set()
    def visit(tid):
        if tid in visiting:
            raise ValueError('task dependencies contain a cycle')
        if tid not in visited:
            visiting.add(tid)
            for dep in dependencies[tid]:
                visit(dep)
            visiting.remove(tid)
            visited.add(tid)
    for tid in task_ids:
        visit(tid)
    if any(not any(task['feature_id'] == fid for task in tasks) for fid in feature_ids):
        raise ValueError('every feature needs at least one task')
    return plan


def _base(root, plan_id):
    return Path(root) / '.ai-dev' / 'approved-plans' / _name(plan_id, 'plan_id')


def _save(base, state):
    state['updated_at'] = time.time()
    write(base / 'state.json', state)


def _event(base, kind, **data):
    line = _canonical({'at': time.time(), 'kind': kind, **data}) + b'\n'
    with (base / 'events.jsonl').open('ab') as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def _git(root, *args):
    return subprocess.check_output(['git'] + list(args), cwd=str(root), stderr=subprocess.DEVNULL)


def _is_git_root(root):
    """Compare resolved macOS paths independent of Unicode normalization."""
    reported = Path(os.fsdecode(_git(root, 'rev-parse', '--show-toplevel').strip())).resolve()
    normalize = lambda path: unicodedata.normalize('NFC', os.fspath(path))
    return normalize(reported) == normalize(root)


def _source_signature(root):
    """Bind a pause to the integrated worktree, including untracked source."""
    digest = hashlib.sha256()
    try:
        head = _git(root, 'rev-parse', 'HEAD')
        diff = _git(root, 'diff', '--binary', 'HEAD')
    except subprocess.CalledProcessError:
        # A small new project can have an unborn branch.  Importing a plan
        # must not force a commit merely to obtain a checkpoint signature.
        head = b'<unborn>'
        diff = _git(root, 'diff', '--binary') + _git(root, 'diff', '--binary', '--cached')
    digest.update(head)
    if len(diff) > 64 * 1024 * 1024:
        raise ValueError('source diff exceeds 64 MiB')
    digest.update(diff)
    names = sorted(name for name in _git(root, 'ls-files', '-z', '--others', '--exclude-standard').split(b'\0') if name and not name.startswith(b'.ai-dev/'))
    total = 0
    for name in names:
        path = Path(root) / os.fsdecode(name)
        if path.is_symlink():
            content = os.readlink(str(path)).encode('utf-8')
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = b'<missing>'
        total += len(content)
        if total > 64 * 1024 * 1024:
            raise ValueError('untracked source exceeds 64 MiB')
        digest.update(name + b'\0' + _sha(content).encode())
    return digest.hexdigest()


def _status_paths(root):
    raw = _git(root, 'status', '--porcelain', '-z', '--untracked-files=all').split(b'\0')
    paths, index = set(), 0
    while index < len(raw) and raw[index]:
        row = raw[index]
        paths.add(os.fsdecode(row[3:]))
        if row[:2].find(b'R') >= 0 or row[:2].find(b'C') >= 0:
            index += 1
            if index < len(raw) and raw[index]:
                paths.add(os.fsdecode(raw[index]))
        index += 1
    return {path for path in paths if path != '.ai-dev' and not path.startswith('.ai-dev/')}


def _file_digest(path):
    if path.is_symlink():
        return _sha(os.readlink(str(path)).encode())
    if path.is_file():
        return _sha(path.read_bytes())
    return None


def _outside_changes(root, entry, allowed):
    """Changes since the worker started, excluding its approved source files."""
    prior = set(entry.get('before_paths', []))
    added = _status_paths(root) - prior - allowed
    mutated = {path for path, digest in entry.get('before_other_hashes', {}).items()
               if _file_digest(root / path) != digest}
    return added | mutated


def import_plan(root, source):
    root = Path(root).resolve()
    source = Path(source).expanduser().resolve()
    if source.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError('approved plan exceeds 128 KiB')
    plan = validate_plan(json.loads(source.read_text(encoding='utf-8')))
    if not _is_git_root(root):
        raise ValueError('project root must be the Git root')
    base = _base(root, plan['plan_id'])
    digest = _sha(_canonical(plan))
    with lock(root):
        if base.exists():
            existing = read(base / 'plan.json')
            if _sha(_canonical(existing)) != digest:
                raise ValueError('plan_id already exists with different requirements')
            return read(base / 'state.json')
        # Reject an unreadable or oversized source tree before creating a plan
        # record. Otherwise an import failure leaves an incomplete plan_id that
        # cannot be imported again.
        source_signature = _source_signature(root)
        base.mkdir(parents=True)
        write(base / 'plan.json', plan)
        state = {'schema_version': SCHEMA, 'plan_id': plan['plan_id'], 'plan_digest': digest,
                 'status': 'READY', 'reason': None, 'created_at': time.time(),
                 'updated_at': time.time(), 'model_turns': 0, 'usage': [],
                 'owned_paths': [],
                 'source_signature': source_signature,
                 'tasks': {task['id']: {'status': 'PENDING', 'attempts': 0, 'session_id': None,
                                      'checks': [], 'changed_paths': []} for task in plan['tasks']},
                 'features': {feature['id']: {'status': 'PENDING', 'proof': None}
                              for feature in plan['features']}}
        _save(base, state)
        _event(base, 'PLAN_IMPORTED', plan_digest=digest)
        return state


def load(root, plan_id):
    base = _base(root, plan_id)
    plan, state = read(base / 'plan.json'), read(base / 'state.json')
    validate_plan(plan)
    if state.get('plan_digest') != _sha(_canonical(plan)):
        raise ValueError('approved plan changed after import')
    return plan, state


def _context(root, task):
    sections, size = [], 0
    for name in task.get('context_paths', []):
        path = Path(root) / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(Path(root).resolve()):
            raise ValueError('task context unavailable: ' + name)
        content = path.read_bytes()
        size += len(content)
        if size > MAX_CONTEXT_BYTES:
            raise ValueError('task context exceeds 12 KiB; choose narrower sources')
        sections.append('## ' + name + '\n' + content.decode('utf-8'))
    return '\n\n'.join(sections)


def _prompt(plan, task, feature, state, root, recovering=False):
    completed = [tid for tid, value in state['tasks'].items() if value['status'] == 'COMPLETED']
    previous = state['tasks'][task['id']]
    parts = [
        'Execute one task from an already approved product plan. Do not globally replan.',
        'Plan ID: ' + plan['plan_id'] + '; task ID: ' + task['id'] + '; feature ID: ' + feature['id'],
        'Product objective: ' + plan['objective'],
        'Feature: ' + feature['title'],
        'Feature acceptance:\n' + '\n'.join('- ' + item for item in feature['acceptance']),
        'Task: ' + task['title'] + '\n' + task['instructions'],
        'Completed task IDs: ' + (', '.join(completed) or '(none)'),
        'Allowed source paths: ' + ', '.join(task['allowed_paths']),
        'Task checks: ' + json.dumps(task['checks'], ensure_ascii=False),
        'Feature checks: ' + json.dumps(feature['checks'], ensure_ascii=False),
        'Preserve existing user changes. Do not edit outside the allowed paths, .ai-dev, Git metadata, global settings, or other projects. No commit, install, push, or paid API. Keep sandbox and approvals. Read source and tests as needed. The supervisor saves full verification logs after your turn.',
    ]
    if previous.get('failure'):
        parts.append('Known last failure: ' + str(previous['failure'])[:2000])
    if recovering:
        parts.append('Resume this exact task session. Do not redo already completed tasks.')
    context = _context(root, task)
    if context:
        parts.append('Relevant project decisions and context:\n' + context)
    return '\n\n'.join(parts)


def _log_result(path):
    session, completed, message, usage = None, False, None, None
    if not path.exists():
        return session, completed, message, usage
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'thread.started':
            session = event.get('thread_id')
        elif event.get('type') == 'item.completed' and (event.get('item') or {}).get('type') == 'agent_message':
            message = event['item'].get('text')
        elif event.get('type') == 'turn.completed':
            completed, usage = True, event.get('usage')
    return session, completed, message, usage


def _check(root, base, task_id, label, commands, state, timeout):
    for index, command in enumerate(commands):
        serial = len(state['tasks'][task_id]['checks']) + 1
        path = base / 'logs' / ('%s-%s-%d-%d.log' % (task_id, label, serial, index + 1))
        path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            code, _ = run_command(command, root, timeout=timeout, env=codex.environment(), output_path=path)
            error = None
        except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            code, error = None, str(exc)
        row = {'argv': command, 'exit_code': code, 'log': str(path.relative_to(root)),
               'duration_seconds': round(time.monotonic() - started, 3), 'error': error}
        state['tasks'][task_id]['checks'].append(row)
        _save(base, state)
        print('  CHECK %s exit=%s log=%s' % (label, code, row['log']), flush=True)
        if code != 0:
            return False
    return True


def _block(base, state, task_id, reason):
    state['status'] = 'BLOCKED'
    state['reason'] = reason
    if task_id:
        state['tasks'][task_id]['status'] = 'BLOCKED'
        state['tasks'][task_id]['failure'] = reason
    _save(base, state)
    _event(base, 'BLOCKED', task_id=task_id, reason=reason)
    print('BLOCKED: ' + reason, flush=True)
    return state


def _block_external_drift(root, base, state, task_id, allowed, stage):
    entry = state['tasks'][task_id]
    changed = sorted(_outside_changes(root, entry, allowed))
    if not changed:
        return _block(base, state, task_id,
                      'Approved source changed during %s; inspect before continuing' % stage)
    entry['external_drift'] = {
        'paths': changed,
        'source_signature': _source_signature(root),
        'allowed_hashes': {path: _file_digest(root / path) for path in allowed},
    }
    return _block(base, state, task_id,
                  'Workspace changed outside task allowlist during %s (author unknown): %s' %
                  (stage, ', '.join(changed)))


def run_plan(root, plan_id, max_tasks=None, resume=False, verify_only=False):
    """Run at most max_tasks new tasks. A new process can continue the queue."""
    root = Path(root).resolve()
    if max_tasks is not None and (type(max_tasks) is not int or max_tasks < 1):
        raise ValueError('max_tasks must be positive')
    base = _base(root, plan_id)
    with lock(root):
        plan, state = load(root, plan_id)
        if state['status'] == 'COMPLETED':
            return state
        if state['status'] == 'BLOCKED' and not resume:
            return state
        legacy_path = root / '.ai-dev' / 'state.json'
        if legacy_path.exists():
            legacy_status = read(legacy_path).get('status')
            if legacy_status not in ('IDLE', 'COMPLETED', 'CANCELLED', 'BLOCKED'):
                return _block(base, state, None, 'Existing MegaProg task state is active: ' + str(legacy_status))
        current_signature = _source_signature(root)
        if current_signature != state['source_signature']:
            active = [task for task in plan['tasks'] if state['tasks'][task['id']]['status'] in ('RUNNING', 'VERIFYING')]
            if len(active) != 1:
                return _block(base, state, None, 'Source changed since the saved plan checkpoint; inspect it before resuming.')
            entry = state['tasks'][active[0]['id']]
            allowed = set(active[0]['allowed_paths'])
            outside = _outside_changes(root, entry, allowed)
            if outside:
                return _block(base, state, None, 'Source changed outside the interrupted task: ' + ', '.join(sorted(outside)))
            state['source_signature'] = current_signature
            _save(base, state)
        config_path = root / '.ai-dev' / 'config.json'
        config = read(config_path) if config_path.exists() else dict(supervisor.DEFAULT)
        if type(config.get('max_attempts')) is not int or config['max_attempts'] < 1:
            raise ValueError('invalid max_attempts configuration')
        if type(config.get('verify_timeout_seconds')) is not int or config['verify_timeout_seconds'] < 1:
            raise ValueError('invalid verify timeout configuration')
        configured_turn_cap = config.get('max_model_turns')
        if configured_turn_cap is not None and (type(configured_turn_cap) is not int or configured_turn_cap < 1):
            raise ValueError('invalid max_model_turns configuration')
        turn_cap = min(plan['budget']['max_model_turns'], configured_turn_cap or plan['budget']['max_model_turns'])
        feature_map = {feature['id']: feature for feature in plan['features']}
        ran = 0
        while True:
            pending = [task for task in plan['tasks'] if state['tasks'][task['id']]['status'] in ('PENDING', 'RUNNING', 'VERIFYING', 'BLOCKED')]
            if not pending:
                state['status'], state['reason'] = 'COMPLETED', None
                _save(base, state)
                _event(base, 'PLAN_COMPLETED')
                print('COMPLETED: %s features verified' % len(plan['features']), flush=True)
                return state
            if max_tasks is not None and ran >= max_tasks:
                state['status'], state['reason'] = 'PAUSED', 'Reached --max-tasks limit'
                _save(base, state)
                print('PAUSED: %d completed this run; %d remaining' % (ran, len(pending)), flush=True)
                return state
            pending.sort(key=lambda task: 0 if state['tasks'][task['id']]['status'] in ('RUNNING', 'VERIFYING', 'BLOCKED') else 1)
            ready = [task for task in pending if all(state['tasks'][dep]['status'] == 'COMPLETED' for dep in task.get('depends_on', []))]
            if not ready:
                return _block(base, state, None, 'No ready task; inspect dependency and blocked statuses.')
            task = ready[0]
            tid, feature = task['id'], feature_map[task['feature_id']]
            entry = state['tasks'][tid]
            if entry['status'] == 'BLOCKED' and (not resume or entry['attempts'] >= config['max_attempts']):
                return _block(base, state, tid, 'Task is blocked or max_attempts reached: ' + tid)
            before = _status_paths(root)
            allowed = set(task['allowed_paths'])
            overlap = (before & allowed) - set(state.get('owned_paths', []))
            if overlap and entry['status'] == 'PENDING':
                return _block(base, state, tid, 'Allowed source path has pre-existing edits: ' + ', '.join(sorted(overlap)))
            before_hashes = {path: _file_digest(root / path) for path in before - allowed}
            allowed_hashes = (entry.get('before_allowed_hashes')
                              if entry['status'] in ('RUNNING', 'VERIFYING') and entry.get('before_allowed_hashes')
                              else {path: _file_digest(root / path) for path in allowed})
            if entry['status'] == 'PENDING':
                entry['before_paths'] = sorted(before)
                entry['before_other_hashes'] = before_hashes
                entry['before_allowed_hashes'] = allowed_hashes
                _save(base, state)
            done_count = sum(item['status'] == 'COMPLETED' for item in state['tasks'].values())
            phase = 'VERIFYING' if entry['status'] == 'VERIFYING' else 'PREPARING'
            print('%s %s (%d/%d done): %s' % (phase, tid, done_count, len(plan['tasks']), task['title']), flush=True)
            if entry['status'] != 'VERIFYING':
                recovering = entry['status'] in ('RUNNING', 'BLOCKED') and bool(entry.get('model_log'))
                session = entry.get('session_id')
                if recovering and entry.get('model_log'):
                    observed_session, complete, _, usage = _log_result(root / entry['model_log'])
                    session = session or observed_session
                    if complete and entry['status'] == 'RUNNING':
                        entry['status'] = 'VERIFYING'
                        if usage and not entry.get('usage'):
                            entry['usage'] = usage
                            state['usage'].append({'task_id': tid, 'usage': usage})
                        _save(base, state)
                if entry['status'] != 'VERIFYING':
                    if verify_only:
                        raise ValueError('Verification-only continuation requires a reconciled task')
                    if state['model_turns'] >= turn_cap:
                        return _block(base, state, tid, 'Approved plan model-turn budget exhausted')
                    if recovering and not session:
                        return _block(base, state, tid, 'Interrupted turn has no recoverable Codex session ID')
                    if entry['attempts'] >= config['max_attempts']:
                        return _block(base, state, tid, 'Task max_attempts reached: ' + tid)
                    doctor = codex.doctor(root)
                    if not doctor['ready']:
                        return _block(base, state, tid, 'Codex ChatGPT authorization unavailable: ' + str(doctor.get('login')))
                    catalog = discover(root, doctor['executable'])
                    model = task.get('model', 'gpt-6-luna')
                    reasoning = task.get('reasoning', 'medium')
                    try:
                        choice = routing.choose(catalog, task['title'] + ' ' + task['instructions'],
                                                model=model, reasoning=reasoning)
                    except ValueError as exc:
                        return _block(base, state, tid, str(exc))
                    try:
                        prepared_prompt = _prompt(plan, task, feature, state, root, recovering)
                    except (OSError, UnicodeError, ValueError) as exc:
                        return _block(base, state, tid, 'Task preflight failed before model turn: ' + str(exc))
                    entry['attempts'] += 1
                    state['model_turns'] += 1
                    entry['status'], state['status'], state['reason'] = 'RUNNING', 'RUNNING', None
                    log = base / 'logs' / ('%s-turn-%d.jsonl' % (tid, entry['attempts']))
                    log.parent.mkdir(parents=True, exist_ok=True)
                    entry['model_log'] = str(log.relative_to(root))
                    entry['model'] = {'model': choice['model'], 'reasoning': choice['reasoning']}
                    _save(base, state)
                    _event(base, 'MODEL_TURN_STARTED', task_id=tid, attempt=entry['attempts'], session_id=session)
                    print('MODEL TURN %s attempt=%d/%d' % (tid, entry['attempts'], config['max_attempts']), flush=True)
                    try:
                        with model_turn(config, choice['model'], choice['reasoning'],
                                        owner={'project': root.name, 'task_id': tid}):
                            result = codex.execute(root, dict(config, model=choice['model'], reasoning=choice['reasoning']),
                                                   doctor, prepared_prompt, log,
                                                   session=session)
                    except AccountGuardError as exc:
                        entry['attempts'] -= 1
                        state['model_turns'] -= 1
                        entry['model_log'] = None
                        return _block(base, state, tid, 'Account guard prevented a model turn: ' + str(exc))
                    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                        return _block(base, state, tid, 'Worker launch failed: ' + str(exc))
                    entry['session_id'] = result.get('session_id')
                    entry['usage'] = result.get('usage')
                    entry['duration_seconds'] = result.get('telemetry', {}).get('duration_seconds')
                    if result.get('usage'):
                        state['usage'].append({'task_id': tid, 'usage': result['usage']})
                    _save(base, state)
                    if not result['ok']:
                        return _block(base, state, tid, 'Worker turn failed; see ' + entry['model_log'])
                    entry['status'], state['status'] = 'VERIFYING', 'VERIFYING'
                    _save(base, state)
            after = _status_paths(root)
            outside = sorted((after - before) - allowed)
            mutated = sorted(path for path, digest in before_hashes.items()
                             if _file_digest(root / path) != digest)
            if outside or mutated:
                return _block_external_drift(root, base, state, tid, allowed, 'worker turn')
            changed_allowed = {path for path in allowed if _file_digest(root / path) != allowed_hashes[path]}
            entry['changed_paths'] = sorted(set(entry.get('changed_paths', [])) | changed_allowed)
            state['source_signature'] = _source_signature(root)
            state['owned_paths'] = sorted(set(state.get('owned_paths', [])) | set(entry['changed_paths']))
            _save(base, state)
            checks = list(task['checks']) + [['git', 'diff', '--check']]
            if not _check(root, base, tid, 'task', checks, state, config['verify_timeout_seconds']):
                return _block(base, state, tid, 'Task verification failed; inspect saved check log')
            if _source_signature(root) != state['source_signature']:
                return _block_external_drift(root, base, state, tid, allowed, 'task verification')
            # A feature is complete only after every task and its own checks pass.
            entry['status'] = 'COMPLETED'
            entry['completed_at'] = time.time()
            for item in plan['features']:
                fid = item['id']
                if all(state['tasks'][t['id']]['status'] == 'COMPLETED' for t in plan['tasks'] if t['feature_id'] == fid):
                    if not _check(root, base, tid, 'feature-' + fid, item['checks'], state, config['verify_timeout_seconds']):
                        state['features'][fid]['status'] = 'REGRESSION'
                        return _block(base, state, tid, 'Feature acceptance failed: ' + fid)
                    if _source_signature(root) != state['source_signature']:
                        return _block_external_drift(root, base, state, tid, allowed, 'feature verification')
                    state['features'][fid] = {'status': 'VERIFIED', 'proof': {
                        'task_ids': [t['id'] for t in plan['tasks'] if t['feature_id'] == fid],
                        'checks': [row for row in entry['checks'] if 'feature-' + fid in row['log']],
                        'source_signature': _source_signature(root), 'verified_at': time.time()}}
            state['source_signature'] = _source_signature(root)
            _save(base, state)
            _event(base, 'TASK_COMPLETED', task_id=tid, changed_paths=entry['changed_paths'],
                   source_signature=state['source_signature'])
            print('VERIFIED %s; %d/%d tasks complete' % (tid, sum(x['status'] == 'COMPLETED' for x in state['tasks'].values()), len(plan['tasks'])), flush=True)
            ran += 1


def reconcile_plan(root, plan_id, accepted_external_paths, accept_current_state=False):
    """Explicitly review external drift, then verify an already completed turn.

    This never launches a worker.  The operator must name every changed path
    outside the task allowlist; the resulting decision and hashes are durable.
    """
    root = Path(root).resolve()
    accepted = set(accepted_external_paths)
    if not accepted or len(accepted) != len(accepted_external_paths):
        raise ValueError('Name every external path exactly once')
    for path in accepted:
        _path(path, 'accepted external path')
    base = _base(root, plan_id)
    with lock(root):
        plan, state = load(root, plan_id)
        blocked = [task for task in plan['tasks']
                   if state['tasks'][task['id']]['status'] == 'BLOCKED']
        verifying = [task for task in plan['tasks']
                     if state['tasks'][task['id']]['status'] == 'VERIFYING']
        if state['status'] == 'VERIFYING' and len(verifying) == 1:
            entry = state['tasks'][verifying[0]['id']]
            prior = entry.get('reconciliation') or {}
            if set(prior.get('accepted_external_paths', [])) != accepted:
                raise ValueError('Reconciliation was already recorded with different paths')
        else:
            if (state['status'] != 'BLOCKED' or len(blocked) != 1 or
                    not str(state.get('reason') or '').startswith((
                        'Workspace changed outside task allowlist',
                        'Worker changed files outside allowed paths'))):
                raise ValueError('Only an external-drift block can be reconciled')
            task = blocked[0]
            entry = state['tasks'][task['id']]
            allowed = set(task['allowed_paths'])
            if accepted & allowed:
                raise ValueError('Accepted external paths overlap the task allowlist')
            current = _outside_changes(root, entry, allowed)
            if current != accepted:
                raise ValueError('External paths differ from current workspace: ' + ', '.join(sorted(current)))
            log_name = entry.get('model_log')
            if not log_name:
                raise ValueError('Completed worker log is missing')
            log = root / log_name
            session, complete, _, usage = _log_result(log)
            if not complete or not session or (entry.get('session_id') and entry['session_id'] != session):
                raise ValueError('Worker turn completion and session are not proven')
            if not log.is_file():
                raise ValueError('Completed worker log is missing')
            allowed_hashes = {path: _file_digest(root / path) for path in allowed}
            observed = entry.get('external_drift')
            if observed:
                if observed.get('allowed_hashes') != allowed_hashes:
                    raise ValueError('Approved source patch changed after the blocked turn')
            elif not accept_current_state:
                raise ValueError('Legacy block has no saved patch hashes; explicitly accept the reviewed current state')
            changed_allowed = {path for path in allowed
                               if allowed_hashes[path] != entry.get('before_allowed_hashes', {}).get(path)}
            if not changed_allowed:
                raise ValueError('No approved source changes to verify')
            signature = _source_signature(root)
            entry['session_id'] = session
            if usage and not entry.get('usage'):
                entry['usage'] = usage
                state['usage'].append({'task_id': task['id'], 'usage': usage})
            entry['reconciliation'] = {
                'accepted_external_paths': sorted(accepted),
                'external_hashes': {path: _file_digest(root / path) for path in accepted},
                'allowed_hashes': allowed_hashes,
                'model_log_sha256': _file_digest(log),
                'source_signature': signature,
                'at': time.time(),
            }
            entry['status'], state['status'], state['reason'] = 'VERIFYING', 'VERIFYING', None
            state['source_signature'] = signature
            _save(base, state)
            _event(base, 'EXTERNAL_DRIFT_RECONCILED', task_id=task['id'],
                   accepted_external_paths=sorted(accepted), source_signature=signature)
            print('RECONCILED %s; verifying saved worker turn without a new model call' % task['id'], flush=True)
    return run_plan(root, plan_id, max_tasks=1, resume=True, verify_only=True)


def summary(root, plan_id):
    plan, state = load(root, plan_id)
    active = next((task['id'] for task in plan['tasks']
                   if state['tasks'][task['id']]['status'] in ('RUNNING', 'VERIFYING', 'BLOCKED')), None)
    current_task = active or next((task['id'] for task in plan['tasks']
                                   if state['tasks'][task['id']]['status'] == 'PENDING' and
                                   all(state['tasks'][dep]['status'] == 'COMPLETED'
                                       for dep in task.get('depends_on', []))), None)
    remaining = sum(item['status'] != 'COMPLETED' for item in state['tasks'].values())
    config_path = Path(root) / '.ai-dev' / 'config.json'
    config = read(config_path) if config_path.exists() else {}
    cap = config.get('max_model_turns')
    effective_cap = min(plan['budget']['max_model_turns'], cap) if type(cap) is int and cap > 0 else plan['budget']['max_model_turns']
    totals = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0,
              'reasoning_output_tokens': 0}
    missing_usage = max(0, state['model_turns'] - len(state['usage']))
    for item in state['usage']:
        usage = item.get('usage') if isinstance(item, dict) else None
        if not isinstance(usage, dict):
            missing_usage += 1
            continue
        for name in totals:
            value = usage.get(name)
            if type(value) is int and value >= 0:
                totals[name] += value
    return {'plan_id': plan_id, 'status': state['status'], 'reason': state.get('reason'),
            'current_task': current_task, 'remaining_tasks': remaining,
            'features': [{'id': item['id'], 'title': item['title'],
                          'status': state['features'][item['id']]['status'],
                          'proof': state['features'][item['id']].get('proof')}
                         for item in plan['features']],
            'tasks': [{'id': item['id'], 'feature_id': item['feature_id'],
                       'status': state['tasks'][item['id']]['status'],
                       'attempts': state['tasks'][item['id']]['attempts'],
                       'session_id': state['tasks'][item['id']].get('session_id'),
                       'changed_paths': state['tasks'][item['id']].get('changed_paths', []),
                       'checks': state['tasks'][item['id']].get('checks', [])}
                      for item in plan['tasks']],
            'model_turns': state['model_turns'], 'model_turn_budget': effective_cap,
            'usage': state['usage'], 'usage_totals': totals, 'missing_usage_records': missing_usage,
            'elapsed_seconds': round(max(0, state['updated_at'] - state['created_at']), 3),
            'updated_at': state['updated_at']}
