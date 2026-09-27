"""Deterministic, bounded context view for a MegaProg coordinator.

The capsule is a generated view over Project Memory, Git and canonical task
state. It is never a source of truth and never includes raw model artifacts.
"""

import hashlib
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import project_memory
from .storage import read, write

DEFAULT_BUDGET = 8192
MIN_BUDGET = 3072
MAX_BUDGET = 16384
JOURNAL_TAIL_BYTES = 262144
MAX_JOURNAL_ROWS = 250
CHECKPOINT_DIR = Path('.ai-dev') / 'memory' / 'coordinator-checkpoints'
CHECKPOINT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _git(root, *args):
    try:
        return subprocess.check_output(['git'] + list(args), cwd=str(root),
                                       stderr=subprocess.DEVNULL, text=True,
                                       timeout=3).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_dirty(root):
    try:
        output = subprocess.check_output(['git', 'status', '--porcelain'], cwd=str(root),
                                         stderr=subprocess.DEVNULL, text=True, timeout=3)
        return bool(output.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def _clean(value, limit=500):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())[:limit]


def _tail_records(path):
    """Read a bounded journal tail; malformed records are reported, not fatal."""
    if not path.exists():
        return [], 0, None
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - JOURNAL_TAIL_BYTES))
            data = stream.read(JOURNAL_TAIL_BYTES)
        if size > JOURNAL_TAIL_BYTES:
            first_newline = data.find(b'\n')
            data = data[first_newline + 1:] if first_newline >= 0 else b''
        lines = data.splitlines()[-MAX_JOURNAL_ROWS:]
        rows = []
        malformed = None
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode('utf-8'))
                if isinstance(row, dict) and row.get('schema_version') == project_memory.SCHEMA_VERSION:
                    rows.append(row)
                else:
                    malformed = 'unsupported or malformed record in %s' % path.name
            except (UnicodeError, ValueError):
                malformed = 'malformed JSON in %s' % path.name
        return rows, len(lines), malformed
    except OSError:
        return [], 0, 'cannot read %s' % path.name


def _task_state(root, task_id):
    if not task_id:
        return None, None
    candidates = [Path('.ai-dev') / 'state.json',
                  Path('.ai-dev') / 'runs' / str(task_id) / 'state.json']
    for relative in candidates:
        path = root / relative
        try:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            state = read(path)
            if isinstance(state, dict) and str(state.get('id') or '') == str(task_id):
                return state, relative.as_posix()
        except (OSError, ValueError, TypeError):
            continue
    return None, None


def _stage_summary(state):
    if not isinstance(state, dict):
        return ''
    plan = state.get('plan')
    if not isinstance(plan, dict):
        return ''
    steps = plan.get('steps')
    index = state.get('checkpoint_index', 0)
    if not isinstance(steps, list) or type(index) is not int or not 0 <= index < len(steps):
        return ''
    step = steps[index]
    if not isinstance(step, dict):
        return ''
    return _clean(step.get('title') or step.get('goal') or step.get('description'), 700)


def _matches_component(row, component):
    if not component:
        return True
    needle = component.casefold()
    values = [row.get('summary'), row.get('decision'), row.get('rationale'),
              row.get('stage_id'), row.get('type'), row.get('error_class'),
              row.get('error_signature')]
    values.extend(row.get('affected_paths') or [])
    values.extend(row.get('artifact_refs') or [])
    values.extend(row.get('evidence_refs') or [])
    return any(isinstance(value, str) and needle in value.casefold() for value in values)


def _ref_list(values, limit=6):
    return [_clean(item, 240) for item in values[:limit] if isinstance(item, str) and item]


def _candidate_decision(row):
    return {'id': row.get('decision_id'), 'task_id': row.get('task_id'),
            'decision': _clean(row.get('decision'), 500),
            'rationale': _clean(row.get('rationale'), 240),
            'status': row.get('status'), 'commit': row.get('commit'),
            'evidence_refs': _ref_list(row.get('evidence_refs'))}


def _candidate_event(row):
    return {'id': row.get('event_id'), 'task_id': row.get('task_id'),
            'stage_id': row.get('stage_id'), 'type': row.get('type'),
            'summary': _clean(row.get('summary'), 300), 'commit': row.get('commit'),
            'artifact_refs': _ref_list(row.get('artifact_refs'))}


def _candidate_failure(row):
    return {'id': row.get('failure_id'), 'task_id': row.get('task_id'),
            'stage_id': row.get('stage_id'), 'error_class': _clean(row.get('error_class'), 120),
            'signature': _clean(row.get('error_signature'), 180),
            'resolution_status': _clean(row.get('resolution_status'), 80),
            'artifact_refs': _ref_list(row.get('artifact_refs'))}


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def _truncate_list(values, max_items):
    return values[:max_items]


def _latest_event_id(events):
    if not events:
        return None
    return events[-1].get('event_id')


def _checkpoint_path(root, checkpoint_id):
    if not isinstance(checkpoint_id, str) or not CHECKPOINT_ID.fullmatch(checkpoint_id) or checkpoint_id in ('.', '..'):
        raise ValueError('Invalid checkpoint id; use 1–80 letters, digits, dot, underscore or hyphen.')
    return root / CHECKPOINT_DIR / (checkpoint_id + '.json')


def read_checkpoint(root, checkpoint_id):
    path = _checkpoint_path(Path(root), checkpoint_id)
    if not path.exists():
        raise ValueError('Coordinator checkpoint not found: %s' % checkpoint_id)
    value = read(path)
    required = {'schema_version', 'coordinator_checkpoint_id', 'last_seen_event_id',
                'task_id', 'head', 'timestamp'}
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or
            required - set(value) or value.get('coordinator_checkpoint_id') != checkpoint_id):
        raise ValueError('Malformed coordinator checkpoint.')
    return value


def save_checkpoint(root, checkpoint_id, capsule):
    path = _checkpoint_path(Path(root), checkpoint_id)
    value = {'schema_version': 1, 'coordinator_checkpoint_id': checkpoint_id,
            'last_seen_event_id': capsule['context_meta'].get('last_included_event_id'),
             'task_id': capsule['current'].get('active_task'),
             'head': capsule['project'].get('head'), 'timestamp': _now()}
    write(path, value)
    return value


def is_stale(root, capsule):
    """Compare the capsule identity with current canonical Git/task/event state."""
    reasons = []
    head = _git(root, 'rev-parse', 'HEAD')
    if head is None:
        reasons.append('HEAD_UNAVAILABLE')
    if capsule.get('project', {}).get('head') != head:
        reasons.append('HEAD_CHANGED')
    try:
        current = project_memory.read_current(root)
        task_id = current.get('active_task')
        memory_schema = current.get('schema_version')
    except (OSError, ValueError, TypeError):
        task_id = None
        memory_schema = None
        reasons.append('MEMORY_UNAVAILABLE')
    canonical_path = Path(root) / '.ai-dev' / 'state.json'
    try:
        if canonical_path.is_file() and canonical_path.stat().st_size <= 2 * 1024 * 1024:
            canonical = read(canonical_path)
            if isinstance(canonical, dict) and canonical.get('status') not in ('COMPLETED', 'CANCELLED', 'IDLE'):
                task_id = canonical.get('id') or task_id
            elif isinstance(canonical, dict):
                task_id = None
    except (OSError, ValueError, TypeError):
        reasons.append('TASK_STATE_UNAVAILABLE')
    if capsule.get('current', {}).get('active_task') != task_id:
        reasons.append('ACTIVE_TASK_CHANGED')
    events, _, _ = _tail_records(Path(root) / project_memory.MEMORY_REL / 'events.jsonl')
    latest = _latest_event_id(events)
    if capsule.get('context_meta', {}).get('latest_event_id') != latest:
        reasons.append('EVENTS_CHANGED')
    if capsule.get('context_meta', {}).get('memory_schema_version') != memory_schema:
        reasons.append('MEMORY_SCHEMA_CHANGED')
    return {'stale': bool(reasons), 'reasons': reasons,
            'snapshot_id': capsule.get('context_meta', {}).get('snapshot_id'),
            'current_head': head, 'current_task_id': task_id, 'latest_event_id': latest}


def build_capsule(root, task_id=None, component=None, budget=DEFAULT_BUDGET,
                  since_event_id=None, checkpoint_id=None):
    """Build a deterministic bounded capsule without model calls."""
    started = time.monotonic()
    root = Path(root).resolve()
    if type(budget) is not int or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise ValueError('budget must be between %d and %d bytes.' % (MIN_BUDGET, MAX_BUDGET))
    if since_event_id and checkpoint_id:
        raise ValueError('Use either since_event_id or checkpoint_id, not both.')
    checkpoint = read_checkpoint(root, checkpoint_id) if checkpoint_id else None
    if checkpoint:
        since_event_id = checkpoint.get('last_seen_event_id')
    memory_status = 'OK'
    try:
        current = project_memory.read_current(root)
    except (OSError, ValueError, TypeError) as exc:
        current = project_memory._empty_current(root)
        memory_status = 'DEGRADED'
        memory_warning = _clean(str(exc), 180)
    else:
        memory_warning = None

    canonical_path = root / '.ai-dev' / 'state.json'
    canonical_state = None
    try:
        if canonical_path.is_file() and canonical_path.stat().st_size <= 2 * 1024 * 1024:
            value = read(canonical_path)
            if isinstance(value, dict):
                canonical_state = value
    except (OSError, ValueError, TypeError):
        memory_status = 'DEGRADED'
        memory_warning = 'canonical task state unavailable or malformed'
    active_id = current.get('active_task')
    if canonical_state and canonical_state.get('status') not in ('COMPLETED', 'CANCELLED', 'IDLE'):
        active_id = str(canonical_state.get('id') or active_id or '') or None
    checkpoint_task = checkpoint.get('task_id') if checkpoint else None
    selected_task_value = task_id or active_id or checkpoint_task
    selected_task = str(selected_task_value) if selected_task_value else None
    selected_state, selected_state_ref = _task_state(root, selected_task)
    if selected_task and selected_task == active_id and canonical_state:
        selected_state = canonical_state
        selected_state_ref = '.ai-dev/state.json'

    journal = {}
    considered = 1
    for kind in ('events', 'decisions', 'failures'):
        rows, count, warning = _tail_records(root / project_memory.MEMORY_REL / (kind + '.jsonl'))
        journal[kind] = rows
        considered += count
        if warning:
            memory_status = 'DEGRADED'
            memory_warning = warning
    all_events = journal['events']
    all_decisions = journal['decisions']
    all_failures = journal['failures']
    latest_id = _latest_event_id(all_events)
    event_cursor_found = since_event_id is None
    if since_event_id:
        for item in all_events:
            if item.get('event_id') == since_event_id:
                event_cursor_found = True
                break

    decisions_ids = set(current.get('recent_decisions') or [])
    if selected_state and isinstance(selected_state.get('decisions'), list):
        decisions_ids.update(item.get('decision_id') for item in selected_state['decisions']
                             if isinstance(item, dict) and item.get('decision_id'))
    decisions = []
    for item in all_decisions:
        related_task = selected_task and item.get('task_id') == selected_task
        related_id = item.get('decision_id') in decisions_ids
        if (related_task or related_id) and item.get('status') == 'accepted' and _matches_component(item, component):
            decisions.append(_candidate_decision(item))
    decisions = decisions[-12:]
    if since_event_id:
        cursor_index = next((i for i, row in enumerate(all_events)
                             if row.get('event_id') == since_event_id), None)
        event_rows = all_events[cursor_index + 1:] if cursor_index is not None else []
    else:
        event_rows = all_events[-5:]
    event_rows = [row for row in event_rows
                  if (row.get('task_id') == selected_task if selected_task
                      else row.get('task_id') is None) and _matches_component(row, component)]
    events = [_candidate_event(row) for row in reversed(event_rows)]
    decisions.reverse()
    failures = [_candidate_failure(item) for item in all_failures
                if selected_task and item.get('task_id') == selected_task and _matches_component(item, component)][-3:]
    failures.reverse()

    head = _git(root, 'rev-parse', 'HEAD')
    branch = _git(root, 'branch', '--show-current')
    dirty = _git_dirty(root)
    task_goal = _clean((selected_state or {}).get('prompt') or (selected_state or {}).get('task'), 1400)
    task_plan = (selected_state or {}).get('plan')
    plan_id = current.get('plan_id')
    if isinstance(task_plan, dict):
        plan_id = hashlib.sha256(json.dumps(task_plan, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode('utf-8')).hexdigest()
    canonical_status = (canonical_state or {}).get('status') or current.get('status')
    canonical_stage = project_memory._active_stage(canonical_state) if canonical_state else None
    blockers = list(current.get('blockers', []))
    if canonical_status in ('BLOCKED', 'PENDING_APPROVAL') and canonical_state:
        reason = _clean(canonical_state.get('reason') or canonical_state.get('last_failure'), 500)
        if reason and reason not in blockers:
            blockers.insert(0, reason)
    progress = (canonical_state or {}).get('progress')
    canonical_actions = progress.get('next_step') if isinstance(progress, dict) else None
    if canonical_actions:
        canonical_actions = canonical_actions if isinstance(canonical_actions, list) else [canonical_actions]
    else:
        canonical_actions = current.get('next_allowed_actions', [])
    canonical_invariants = (canonical_state or {}).get('active_invariants')
    if not isinstance(canonical_invariants, list):
        canonical_invariants = current.get('active_invariants', [])
    current_value = {
        'active_task': active_id,
        'active_stage': canonical_stage or current.get('active_stage'),
        'status': canonical_status,
        'blockers': [_clean(item, 350) for item in blockers[:6]
                     if isinstance(item, str)],
        'active_invariants': [_clean(item, 240) for item in canonical_invariants[:8]
                              if isinstance(item, str)],
        'next_allowed_actions': [_clean(item, 240) for item in canonical_actions[:5]
                                 if isinstance(item, str)]}
    task_value = {'task_id': selected_task, 'goal': task_goal, 'plan_id': plan_id,
                  'stage_summary': _stage_summary(selected_state)}
    evidence_refs = ['.ai-dev/memory/current.json']
    for document in ('docs/PRODUCT_ROADMAP.md', 'docs/CONTINUITY.md'):
        if (root / document).is_file():
            evidence_refs.append(document)
    if selected_state_ref:
        evidence_refs.append(selected_state_ref)
    for item in decisions:
        evidence_refs.extend(['decision:' + str(item['id'])] if item.get('id') else [])
        evidence_refs.extend(item.get('evidence_refs', []))
    for item in events:
        evidence_refs.extend(['event:' + str(item['id'])] if item.get('id') else [])
        evidence_refs.extend(item.get('artifact_refs', []))
    evidence_refs = list(dict.fromkeys(evidence_refs))[:12]
    project_id = current.get('project_id') or project_memory._project_id(root)
    snapshot_seed = '|'.join(str(value or '') for value in
                             (project_id, head, active_id, latest_id, current.get('schema_version')))
    snapshot_id = hashlib.sha256(snapshot_seed.encode('utf-8')).hexdigest()[:20]
    meta = {'budget_bytes': budget, 'bytes_estimated': 0, 'tokens_estimated': 0,
            'items_considered': considered, 'items_selected': 0, 'items_included': 0,
            'items_dropped': 0, 'dropped_reasons': {}, 'truncated': False,
            'generation_ms': 0, 'memory_status': memory_status,
            'memory_warning': memory_warning, 'memory_schema_version': current.get('schema_version'),
            'latest_event_id': latest_id, 'last_included_event_id': None,
            'event_cursor': since_event_id,
            'event_cursor_found': event_cursor_found, 'delta': bool(since_event_id),
            'snapshot_id': snapshot_id, 'stale': False,
            'generated_at': _now()}
    capsule = {'schema_version': 1, 'generated_at': meta['generated_at'],
               'project': {'project_id': project_id, 'name': root.name,
                           'branch': branch, 'head': head, 'dirty': dirty},
               'current': current_value, 'task': task_value,
               'decisions': [], 'recent_events': [], 'relevant_failures': [],
               'evidence_refs': evidence_refs, 'context_meta': meta}
    candidates = [('decision', item) for item in decisions]
    candidates += [('event', item) for item in events]
    candidates += [('failure', item) for item in failures]
    field_for = {'decision': 'decisions', 'event': 'recent_events', 'failure': 'relevant_failures'}
    # Required current/project/task capsule is never silently evicted.
    base_size = _json_bytes(capsule)
    base_compacted = False
    if base_size > budget:
        # Reduce only verbose canonical strings, with references retained.
        before_core = json.dumps({'task': task_value, 'current': current_value,
                                  'refs': evidence_refs}, ensure_ascii=False, sort_keys=True)
        task_value['goal'] = _clean(task_value['goal'], 200)
        task_value['stage_summary'] = _clean(task_value['stage_summary'], 120)
        current_value['blockers'] = [_clean(item, 100) for item in current_value['blockers']]
        current_value['active_invariants'] = [_clean(item, 100)
                                              for item in current_value['active_invariants'][:2]]
        current_value['next_allowed_actions'] = [_clean(item, 100)
                                                 for item in current_value['next_allowed_actions'][:2]]
        evidence_refs = [_clean(item, 140) for item in evidence_refs[:3]]
        capsule['evidence_refs'] = evidence_refs
        after_core = json.dumps({'task': task_value, 'current': current_value,
                                 'refs': evidence_refs}, ensure_ascii=False, sort_keys=True)
        base_compacted = before_core != after_core
        base_size = _json_bytes(capsule)
    if base_size > budget:
        raise ValueError('The required coordinator capsule exceeds the selected budget.')
    dropped = {}
    for kind, item in candidates:
        field = field_for[kind]
        capsule[field].append(item)
        if _json_bytes(capsule) > budget:
            capsule[field].pop()
            dropped[kind] = dropped.get(kind, 0) + 1
    meta['last_included_event_id'] = (
        capsule['recent_events'][0].get('id') if capsule['recent_events'] else
        (since_event_id if since_event_id else latest_id))
    if not event_cursor_found and since_event_id:
        meta['dropped_reasons']['cursor_not_found'] = 1
    if base_compacted:
        meta['dropped_reasons']['core_budget_compaction'] = 1
    meta['dropped_reasons'].update({'budget_' + key: value for key, value in dropped.items()})
    meta['items_selected'] = sum(len(capsule[field_for[kind]]) for kind in field_for)
    meta['items_included'] = meta['items_selected']
    meta['items_dropped'] = (sum(dropped.values()) +
                             (1 if since_event_id and not event_cursor_found else 0) +
                             (1 if base_compacted else 0))
    meta['truncated'] = bool(meta['items_dropped'] or not event_cursor_found)
    meta['bytes_estimated'] = _json_bytes(capsule)
    meta['tokens_estimated'] = int(math.ceil(meta['bytes_estimated'] / 4.0))
    meta['generation_ms'] = int((time.monotonic() - started) * 1000)
    # Metrics are part of the bounded representation; enforce once more after filling them.
    while _json_bytes(capsule) > budget:
        removed = False
        for kind in ('failure', 'event', 'decision'):
            field = field_for[kind]
            if capsule[field]:
                capsule[field].pop()
                meta['items_selected'] -= 1
                meta['items_included'] -= 1
                meta['items_dropped'] += 1
                meta['truncated'] = True
                removed = True
                break
        if not removed:
            raise ValueError('Coordinator context metadata exceeds the selected budget.')
        meta['dropped_reasons']['budget_post_metric_eviction'] = (
            meta['dropped_reasons'].get('budget_post_metric_eviction', 0) + 1)
        meta['bytes_estimated'] = _json_bytes(capsule)
        meta['tokens_estimated'] = int(math.ceil(meta['bytes_estimated'] / 4.0))
    # Resolve the self-referential byte counter to a stable exact value.
    for _ in range(5):
        actual_bytes = _json_bytes(capsule)
        actual_tokens = int(math.ceil(actual_bytes / 4.0))
        if actual_bytes == meta['bytes_estimated'] and actual_tokens == meta['tokens_estimated']:
            break
        meta['bytes_estimated'] = actual_bytes
        meta['tokens_estimated'] = actual_tokens
    if _json_bytes(capsule) > budget:
        raise ValueError('Coordinator context exceeds the selected byte budget.')
    return capsule


def render_capsule(capsule):
    """Stable compact text rendering of the same capsule fields as JSON."""
    project = capsule['project']
    current = capsule['current']
    task = capsule['task']
    meta = capsule['context_meta']
    lines = ['PROJECT', '%s (%s)' % (project.get('name'), project.get('project_id')),
             'GIT', 'branch: %s' % (project.get('branch') or 'unknown'),
             'head: %s' % (project.get('head') or 'unknown'),
             'dirty: %s' % ('yes' if project.get('dirty') else 'no'),
             'ACTIVE TASK', '%s' % (current.get('active_task') or 'none')]
    if task.get('goal'):
        lines.append(task['goal'])
    lines.extend(['STAGE', '%s — %s' % (current.get('active_stage') or 'none',
                                        task.get('stage_summary') or '(no stage summary)'),
                  'STATUS', str(current.get('status') or 'unknown'), 'BLOCKERS'])
    lines.extend(['- ' + item for item in current.get('blockers', [])] or ['- none'])
    lines.append('ACTIVE INVARIANTS')
    lines.extend(['- ' + item for item in current.get('active_invariants', [])] or ['- none'])
    lines.append('ACTIVE DECISIONS')
    lines.extend(['- %s: %s' % (item.get('id'), item.get('decision')) for item in capsule['decisions']] or ['- none'])
    lines.append('RECENT CHANGES')
    lines.extend(['- %s: %s' % (item.get('id'), item.get('summary')) for item in capsule['recent_events']] or ['- none'])
    lines.append('RELEVANT FAILURES')
    lines.extend(['- %s %s: %s' % (item.get('id'), item.get('error_class'), item.get('resolution_status'))
                  for item in capsule['relevant_failures']] or ['- none'])
    lines.append('NEXT ALLOWED ACTIONS')
    lines.extend(['- ' + item for item in current.get('next_allowed_actions', [])] or ['- none'])
    lines.append('MORE CONTEXT')
    lines.extend(['- ' + item for item in capsule.get('evidence_refs', [])] or ['- none'])
    lines.append('CONTEXT')
    lines.append('bytes: %d/%d; ~%d tokens; selected: %d; dropped: %d; truncated: %s; memory: %s; stale-check: %s' %
                 (meta['bytes_estimated'], meta['budget_bytes'], meta['tokens_estimated'],
                  meta['items_selected'], meta['items_dropped'], meta['truncated'],
                  meta['memory_status'], meta['snapshot_id']))
    return '\n'.join(lines)
