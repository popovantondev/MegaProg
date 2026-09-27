import subprocess
import json
import hashlib
import os
import time
import uuid
from contextlib import contextmanager, ExitStack
from pathlib import Path
from . import codex, routing, planning
from . import evidence_packet, planning_pipeline, review_cascade
from .telemetry import attach_context
from .discovery import discover
from .process import run
from .storage import read, write
from . import project_memory
from . import anti_loop
from . import phase_telemetry, test_recommendation
from .report import write_report
from .lessons import prompt_context
from .account_guard import (AccountGuardError, DEFAULT_MAX_CONCURRENT,
                            DEFAULT_STALE_SECONDS, DEFAULT_TIER_LIMITS,
                            model_turn, normalize_legacy_defaults)
from .self_improvement import SELF_IMPROVEMENT_PROMPT
from .context import assemble, DEFAULT_BUDGET_BYTES, project_scope_manifest
from .context_isolation import isolate_generated_context
from .context_compaction import (build_summary, inspect_jsonl, should_compact,
                                  summary_is_fresh)
from .review import codex_advisory_context, create_chatgpt_offer
from .plan_handoff import create_plan_offer
from . import orchestration
from .terminal_style import styled
from .version import __version__
from .self_health import record_safely, exception_frames

DEFAULT = {'model': 'auto', 'reasoning': 'low', 'max_attempts': 5,
           'strong_planning': True,
           'windows_worker_full_access': False,
           'timeout_seconds': 0, 'verify_timeout_seconds': 300, 'verify': [],
           'context_budget_bytes': DEFAULT_BUDGET_BYTES,
           'account_guard': {'enabled': True, 'max_concurrent': DEFAULT_MAX_CONCURRENT,
                             'stale_seconds': DEFAULT_STALE_SECONDS,
                             'tier_limits': dict(DEFAULT_TIER_LIMITS)}}


class PlanningFormatExhausted(ValueError):
    """Both bounded planner answers failed the local JSON contract."""


class PlanningGateBlocked(ValueError):
    """Planner was stopped by deterministic evidence or contract guardrails."""


def _owner_progress_text(value):
    """Format saved owner evidence without letting slot data expand a TTY line."""
    if not isinstance(value, str):
        return 'unknown'
    value = ''.join(c if c.isprintable() and c not in '\r\n' else ' ' for c in value).strip()
    return value[:120] or 'unknown'


@contextmanager
def waiting_turn(config, model, reasoning, notify, owner=None, state=None):
    """Retry only local capacity acquisition; never retry the model body."""
    timeout = config.get('account_guard', {}).get('wait_seconds', 300)
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    previous = None
    with ExitStack() as stack:
        wait_phase = phase_telemetry.measure(state, 'account_slot_wait', 'wait') if state is not None else None
        if wait_phase:
            wait_phase.__enter__()
        try:
            while True:
                try:
                    guard = stack.enter_context(model_turn(config, model, reasoning, owner))
                    guard.evidence['wait_seconds'] = max(0, int(time.monotonic() - started))
                    break
                except AccountGuardError as exc:
                    if exc.evidence.get('reason') not in ('no_free_global_slot', 'no_free_tier_slot'):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    evidence = dict(exc.evidence)
                    evidence['wait_seconds'] = max(0, int(time.monotonic() - started))
                    # A capacity poll is measured as waiting and never as a turn.
                    comparable = dict(evidence)
                    comparable.pop('wait_seconds', None)
                    comparable['owners'] = [{k: v for k, v in item.items() if k != 'held_seconds'}
                                            for item in evidence.get('owners', []) if isinstance(item, dict)]
                    marker = json.dumps(comparable, sort_keys=True, ensure_ascii=False)
                    if marker != previous:
                        notify(evidence)
                        previous = marker
                    time.sleep(min(2, remaining))
        except BaseException as exc:
            if wait_phase:
                wait_phase.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            if wait_phase:
                wait_phase.__exit__(None, None, None)
        yield guard


def progress_line(root, state, status, fields=None, now=None):
    """Build a cheap, human-readable live status line for the terminal."""
    fields = fields or {}
    phase = 'VERIFYING' if status == 'VERIFY' else status
    started = state.get('created_at')
    elapsed = max(0, int((now if now is not None else time.time()) - started)) if isinstance(started, (int, float)) else 0
    verification = state.get('verification') or {}
    checks = verification.get('checks') or []
    last_verify = 'UNKNOWN'
    if checks:
        check = next((item for item in checks if item.get('exit_code') not in (None, 0)), checks[-1])
        last_verify = '%s exit=%s' % (check.get('status') or ('PASS' if check.get('exit_code') == 0 else 'FAIL'), check.get('exit_code', '?'))
    elif status == 'VERIFY':
        last_verify = 'RUNNING'
    doing = fields.get('doing') or fields.get('reason') or {
        'PREFLIGHT': 'подготовка задачи и проверка возможностей',
        'PENDING_APPROVAL': 'ожидание подтверждения пользователя',
        'RUNNING': 'исполнение текущего model turn',
        'VERIFY': 'запуск configured verify-команд',
        'COMPLETED': 'проверка завершена',
        'BLOCKED': 'задача остановлена',
    }.get(status, 'обновление состояния задачи')
    next_step = fields.get('next_step') or {
        'PREFLIGHT': 'проверить Codex, каталог и approval',
        'PENDING_APPROVAL': 'получить требуемое подтверждение пользователя',
        'RUNNING': 'дождаться завершения model turn',
        'VERIFY': 'выполнить verify-команды',
        'COMPLETED': 'просмотреть отчёт и решить, нужен ли commit',
        'BLOCKED': 'разобрать причину и выполнить безопасный resume',
    }.get(status, 'просмотреть состояние задачи')
    report = ''
    waiting = ''
    if status == 'WAITING_FOR_SLOT':
        guard = fields.get('guard') if isinstance(fields.get('guard'), dict) else state.get('guard', {})
        wait_seconds = guard.get('wait_seconds') if isinstance(guard, dict) else None
        owners = guard.get('owners', []) if isinstance(guard, dict) else []
        owner_text = ', '.join('%s/%s/%s held=%ss' % (
            _owner_progress_text(item.get('project')), _owner_progress_text(item.get('task_id')),
            _owner_progress_text(item.get('model')), item.get('held_seconds', 'unknown'))
            for item in owners[:5] if isinstance(item, dict)) or 'unknown'
        waiting = ' wait=%ss owners=%s' % (wait_seconds if isinstance(wait_seconds, int) else 0, owner_text)
    if status in ('COMPLETED', 'BLOCKED'):
        report = ' report=%s' % (root / '.ai-dev' / 'runs' / state['id'] / 'report.json')
    return ('[MegaProg] project=%s task_id=%s phase=%s doing=%s attempt=%s/%s '
            'verify=%s access=%s elapsed=%ss next=%s%s' %
            (Path(root).name, state.get('id', '?'), phase, doing,
             state.get('attempts', 0), state.get('attempt_limit', state.get('config', {}).get('max_attempts', '?')),
             last_verify, 'FULL' if state.get('config', {}).get('windows_worker_full_access') else 'WORKSPACE',
             elapsed, next_step, report + waiting))


def _relevant_files(root):
    """Return names only; never read source or generated files for a summary."""
    code, output = run(['git', 'status', '--short'], root)
    if code:
        return []
    result = []
    for line in output.splitlines():
        name = line[3:].strip() if len(line) > 3 else ''
        if name and '.ai-dev' not in name and not name.startswith('/'):
            result.append(name)
    return result


def configure(root):
    path = root / '.ai-dev/config.json'
    stored = read(path)
    config = normalize_legacy_defaults(stored)
    # Persist only the complete, recognized legacy template.  This keeps the
    # on-disk configuration and a resumed frozen snapshot on one effective
    # policy without rewriting custom or partial limits.
    if config != stored:
        write(path, config)
    if type(config.get('strong_planning', True)) is not bool:
        raise ValueError('strong_planning должен быть boolean.')
    if type(config.get('windows_worker_full_access', False)) is not bool:
        raise ValueError('windows_worker_full_access должен быть boolean.')
    if config.get('windows_worker_full_access') and os.name != 'nt':
        raise ValueError('Полный доступ worker разрешён только для Windows.')
    turn_cap = config.get('max_model_turns')
    if turn_cap is not None and (type(turn_cap) is not int or not 1 <= turn_cap <= 100):
        raise ValueError('max_model_turns должен быть целым числом от 1 до 100.')
    if not isinstance(config.get('model'), str) or not config['model']:
        raise ValueError('Укажите model: auto или идентификатор модели.')
    if config.get('reasoning') not in ('low', 'medium', 'high'):
        raise ValueError('reasoning должен быть low, medium или high.')
    if type(config.get('timeout_seconds')) is not int or config['timeout_seconds'] < 0:
        raise ValueError('timeout_seconds: целое число >= 0; 0 — без ограничения.')
    for key, upper in [('max_attempts', 5), ('verify_timeout_seconds', 3600)]:
        if type(config.get(key)) is not int or not 1 <= config[key] <= upper:
            raise ValueError('Недопустимое значение: ' + key)
    checks = config.get('verify')
    if not isinstance(checks, list) or any(not isinstance(c, list) or not c or
            any(not isinstance(a, str) or not a for a in c) for c in checks):
        raise ValueError('verify должен содержать массивы аргументов команд.')
    budget = config.get('context_budget_bytes', DEFAULT_BUDGET_BYTES)
    if type(budget) is not int or not 8192 <= budget <= 131072:
        raise ValueError('context_budget_bytes должен быть целым числом от 8192 до 131072.')
    for key, lower, upper in [('context_compaction_bytes', 65536, 16 * 1024 * 1024),
                              ('context_compaction_tokens', 8192, 2_000_000)]:
        value = config.get(key, DEFAULT.get(key, 1_000_000 if key.endswith('bytes') else 180_000))
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError('Недопустимое значение: ' + key)
    guard = config.get('account_guard', {})
    if not isinstance(guard, dict):
        raise ValueError('account_guard должен быть объектом.')
    if type(guard.get('enabled', True)) is not bool:
        raise ValueError('account_guard.enabled должен быть boolean.')
    wait_seconds = guard.get('wait_seconds', 300)
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 86400:
        raise ValueError('account_guard.wait_seconds: целое число от 0 до 86400.')
    for key, upper in [('max_concurrent', 100), ('stale_seconds', 86400)]:
        value = guard.get(key, {'max_concurrent': DEFAULT_MAX_CONCURRENT,
                                'stale_seconds': DEFAULT_STALE_SECONDS}[key])
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError('Недопустимое значение account_guard.' + key)
    limits = guard.get('tier_limits', {})
    if not isinstance(limits, dict) or any(key not in ('cheap', 'terra', 'sol', 'expensive') or
                                           type(value) is not int or not 1 <= value <= 100
                                           for key, value in limits.items()):
        raise ValueError('account_guard.tier_limits должен содержать известные положительные целые limits.')
    if guard.get('directory') is not None and (not isinstance(guard['directory'], str) or
                                               not guard['directory'].strip()):
        raise ValueError('account_guard.directory должен быть непустым путём.')
    return config


def ensure_turn_budget(state, config):
    cap = config.get('max_model_turns')
    review_turns = sum(1 for item in state.get('review_turns', [])
                       if isinstance(item, dict) and item.get('consumed') is True)
    rescue_turns = sum(1 for item in state.get('rescue_turns', [])
                       if isinstance(item, dict) and item.get('consumed') is True)
    if cap is not None and len(state.get('planning_attempts', [])) + state.get('attempts', 0) + review_turns + rescue_turns >= cap:
        raise ValueError('Лимит model turns исчерпан (%d); задача остановлена без нового запуска.' % cap)


def git(root, *args):
    code, output = run(['git'] + list(args), root)
    if code:
        raise ValueError(output.strip())
    return output.strip()


def _anti_loop_snapshot(root, stage_id):
    paths = [item[3:] for item in git(root, 'status', '--short').splitlines() if len(item) > 3]
    diff = git(root, 'diff', '--no-ext-diff', '--unified=1', 'HEAD')
    return anti_loop.approach_record(paths, diff, stage_id,
        working_hash=review_cascade._working_hash(root)), paths, diff


def _record_attempt(state, decision, checkpoint, result, verification, before, after,
                    previous_verification, receipt=None, hypothesis=None, new_evidence=None, review_issue=None):
    from .verification_evidence import summarize
    failure = anti_loop.failure_record(verification, checkpoint.get('id'))
    if not failure and review_issue:
        failure = anti_loop.review_failure(review_issue, checkpoint.get('id'))
    before_approach, before_paths, _ = before
    after_approach, after_paths, _ = after
    history = state.setdefault('attempt_history', [])
    prior = next((item for item in reversed(history)
                  if item.get('stage_id') == checkpoint.get('id') and
                  item.get('generation', 0) == state.get('anti_loop_generation', 0)), None)
    previous_failure = prior.get('failure') if prior else None
    signals = anti_loop.progress_signals(previous_failure, failure)
    # Verification comparison is independent of textual output and diff size.
    from .verification_evidence import compare
    compared = compare(previous_verification, verification)
    if compared.get('outcome') == 'improved':
        signals.append(compared.get('reason', 'verification_improved'))
    if new_evidence:
        signals.append('new_relevant_evidence')
    safe_hypothesis = hypothesis[:300] if isinstance(hypothesis, str) and hypothesis else None
    commands = [item.get('command', []) for item in verification.get('checks', [])
                if isinstance(item, dict)]
    approach = anti_loop.approach_record(after_paths, after[2], checkpoint.get('id'),
        safe_hypothesis, commands, working_hash=after_approach.get('diff_hash'))
    row = {'attempt_id': '%s:%s:%s' % (state['id'], checkpoint.get('id'), state['attempts']),
        'task_id': state['id'], 'stage_id': checkpoint.get('id'),
        'role': decision.get('role', 'worker'), 'session_id': result.get('session_id'),
        'hypothesis': safe_hypothesis,
        'approach_id': approach.get('approach_id'), 'approach': approach,
        'files_before': before_paths[:128], 'files_after': after_paths[:128],
        'diff_hash_before': before_approach.get('diff_hash'),
        'diff_hash_after': approach.get('diff_hash'),
        'verification_before': summarize(previous_verification),
        'verification_after': summarize(verification),
        'failure': failure, 'failure_signature': failure.get('signature') if failure else None,
        'new_evidence': (new_evidence or [])[:16], 'plan_deviation': state.get('stage_deviation'),
        'progress_signals': sorted(set(signals)),
        'result': 'PASS' if verification.get('ok') else 'FAIL',
        'attempt': state['attempts'], 'generation': state.get('anti_loop_generation', 0),
        'model': decision.get('model'),
        'reasoning': decision.get('reasoning'), 'review_status':
            (state.get('attempts_detail') or [{}])[-1].get('review_cascade', {}).get('status')}
    history.append(row)
    state['attempt_history'] = history[-64:]
    return row, prior


def _save_rescue_packet(root, state, checkpoint, current_diff, paths, rescue_class):
    from .storage import write
    attempts = [item for item in state.get('attempt_history', [])
                if item.get('stage_id') == checkpoint.get('id')]
    packet, encoded = anti_loop.build_rescue_packet(state, checkpoint, attempts,
        current_diff, paths, known_facts=[item.get('normalized_error', '')
            for row in attempts[-2:] for item in ((row.get('failure') or {}).get('failures') or [])],
        unknowns=['Причина не считается устранённой до успешных настроенных проверок.'],
        rescue_class=rescue_class)
    evidence_ref = (state.get('planning_evidence') or {}).get('packet_path')
    if isinstance(evidence_ref, str) and evidence_ref.startswith('.ai-dev/evidence/'):
        packet['references'].append(evidence_ref[:1000])
    for row in attempts[-2:]:
        attempt_no = row.get('attempt')
        if type(attempt_no) is int and attempt_no > 0:
            ref = '.ai-dev/runs/%s/%d/verify-0.log' % (state['id'], attempt_no)
            if (Path(root) / ref).is_file():
                packet['references'].append(ref)
    packet['references'] = list(dict.fromkeys(packet['references']))[:16]
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    if len(encoded) > anti_loop.MAX_PACKET_BYTES:
        packet['references'] = packet['references'][:4]
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    if len(encoded) > anti_loop.MAX_PACKET_BYTES:
        raise ValueError('Rescue Packet exceeds hard byte limit after references.')
    relative = Path('.ai-dev/runs') / state['id'] / ('rescue-' + str(state['attempts'])) / 'rescue-packet.json'
    write(root / relative, packet)
    state['rescue_packet'] = {'path': str(relative), 'bytes': len(encoded),
                              'rescue_id': packet['rescue_id']}
    return packet


def _record_failure_resolution(root, state):
    matched_id = state.get('failure_memory_match')
    if not isinstance(matched_id, str) or not matched_id or state.get('failure_memory_resolved'):
        return
    from .project_memory import MEMORY_REL, _read_jsonl, append_failure
    prior = next((item for item in _read_jsonl(Path(root) / MEMORY_REL / 'failures.jsonl')
                  if item.get('failure_id') == matched_id), None)
    signature = prior.get('error_signature') if prior else None
    if not signature:
        return
    history = state.get('attempt_history', [])
    failed = next((item.get('failure') for item in reversed(history)
                   if isinstance(item, dict) and item.get('failure')), None)
    if not failed:
        return
    failure = (failed.get('failures') or [{}])[0]
    resolution_id = hashlib.sha256(('%s:%s:resolved' % (state.get('id'), signature)).encode()).hexdigest()[:28]
    record = append_failure(root, failure_id='m9-resolved-' + resolution_id,
        project_id=project_memory._project_id(root), task_id=state.get('id'),
        stage_id=history[-1].get('stage_id') if history else None,
        session_id=state.get('session_id'), commit=project_memory._git(root, 'rev-parse', 'HEAD'),
        error_class=failure.get('error_class') or 'VerificationFailure',
        error_signature=signature, command=failure.get('command'),
        affected_paths=sorted({p for item in history for p in item.get('files_after', [])})[:64],
        attempt=state.get('attempts', 0), resolution_status='resolved',
        artifact_refs=['runs/%s/state.json' % state.get('id'),
                       'runs/%s/report.json' % state.get('id')])
    state['failure_memory_resolution_id'] = record.get('failure_id')
    state['failure_memory_resolved'] = True


def _record_terminal_failure(root, state, checkpoint, failure, attempts):
    """Index only terminal/repeated M9 failures; never archive every retry."""
    if not failure or state.get('anti_loop_failure_recorded'):
        return
    from .project_memory import MEMORY_REL, _read_jsonl, append_failure
    existing = _read_jsonl(Path(root) / MEMORY_REL / 'failures.jsonl')
    signature = failure.get('signature')
    if any(item.get('error_signature') == signature for item in existing):
        state['failure_memory_match'] = next(item.get('failure_id') for item in existing
            if item.get('error_signature') == signature)
        state['anti_loop_failure_recorded'] = True
        return
    paths = sorted({path for item in attempts for path in item.get('files_after', [])})[:64]
    unique_id = hashlib.sha256(('%s:%s:%s' % (state.get('id'), checkpoint.get('id'), signature)).encode()).hexdigest()[:28]
    record = append_failure(root, failure_id='m9-' + unique_id,
        project_id=project_memory._project_id(root), task_id=state.get('id'),
        stage_id=checkpoint.get('id'), session_id=state.get('session_id'),
        commit=state.get('base_head'), error_class=(failure.get('failures') or [{}])[0].get('error_class', 'VerificationFailure'),
        error_signature=signature, command=(failure.get('failures') or [{}])[0].get('command'),
        affected_paths=paths, attempt=state.get('attempts', 0),
        resolution_status='unresolved', artifact_refs=[state.get('rescue_packet', {}).get('path', '')])
    state['failure_memory_match'] = record.get('failure_id')
    state['anti_loop_failure_recorded'] = True


def _run_rescue(root, state, checkpoint, packet, rescue_class, report, config, catalog, save):
    """One bounded, read-only rescue turn in a fresh session; never a worker continuation."""
    if not rescue_class.get('automatic_model'):
        return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 0,
                'reason': rescue_class.get('reason')}
    model, reasoning = rescue_class['model'], rescue_class['reasoning']
    try:
        ensure_turn_budget(state, config)
        routing.choose(catalog, state['prompt'], kind='normal',
                       model=model, reasoning=reasoning)
    except (ValueError, KeyError) as exc:
        return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 0,
                'reason': 'rescue route unavailable: ' + str(exc)[:200]}
    if state.get('orchestration_task_id'):
        gate = orchestration.approval_status(root, 'model_turn', task_id=state['orchestration_task_id'])
        if gate.get('status') != 'APPROVED':
            return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 0,
                    'reason': 'model_turn approval required'}
        if model == 'gpt-6-astra':
            gate = orchestration.approval_status(root, 'expensive_escalation',
                task_id=state['orchestration_task_id'])
            if gate.get('status') != 'APPROVED':
                return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 0,
                        'reason': 'expensive_escalation approval required'}
    fresh_worker_session(state, 'Rescue начинается в чистой сессии; история worker не продолжается.')
    save('RESCUE_IN_PROGRESS', reason='Read-only rescue анализирует компактный Rescue Packet в новой сессии.',
         next_step='Дождаться результата rescue; PlanContract остаётся неизменным.')
    directory = root / '.ai-dev/runs' / state['id'] / ('rescue-' + str(len(state.get('rescue_turns', [])) + 1))
    directory.mkdir(parents=True, exist_ok=True)
    rescue_config = dict(config, model=model, reasoning=reasoning, detect_stall=False)
    prompt = ('Ты выполняешь роль rescue-reviewer в свежей read-only сессии. Не меняй файлы. '
        'Не меняй PlanContract. Верни только JSON: {"status":"RETRY_WITH_NEW_APPROACH|NEEDS_EVIDENCE|NEEDS_HUMAN_DECISION|ABORT_STAGE|PLAN_CONTRACT_REVISION_REQUIRED",'
        '"root_cause_hypothesis":"...","new_evidence_required":[],"new_approach":{"summary":"...","allowed_scope_change":[]},'
        '"plan_contract_change_required":false,"reason":"..."}. Packet:\n' +
        json.dumps(packet, ensure_ascii=False, separators=(',', ':')))
    decision = {'model': model, 'reasoning': reasoning, 'role': 'rescue',
                'reason': rescue_class.get('reason'), 'checkpoint': checkpoint.get('id')}
    try:
        with waiting_turn(config, model, reasoning,
                lambda evidence: save('WAITING_FOR_SLOT', guard=evidence,
                    reason='Жду слот для read-only rescue turn.'),
                owner={'project': root.name, 'task_id': state['id'], 'role': 'rescue'}, state=state):
            with phase_telemetry.measure(state, 'rescue_model_turn', 'model',
                                         overlap_group='rescue'):
                result = codex.execute(root, rescue_config, report, prompt,
                                       directory / 'codex.jsonl', None, sandbox='read-only')
    except (AccountGuardError, ValueError, OSError) as exc:
        return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 0,
                'reason': 'rescue turn не запущен: ' + str(exc)[:250]}
    from .telemetry import attach_context
    attach_context(result, task_id=state['id'], stage_id=checkpoint.get('id'), role='rescue',
                   retry_number=1, attempt_number=state.get('attempts', 0),
                   escalation_reason=rescue_class.get('reason'),
                   configured_model=model, configured_reasoning=reasoning)
    row = {'consumed': True, 'decision': decision, 'codex': result,
           'telemetry': result.get('telemetry'), 'packet': state.get('rescue_packet')}
    state.setdefault('rescue_turns', []).append(row)
    if not result.get('ok'):
        return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 1,
                'reason': 'rescue turn завершился без валидного ответа'}
    try:
        answer = json.loads(result.get('message', ''))
    except (ValueError, TypeError):
        return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 1,
                'reason': 'rescue response не прошёл JSON-контракт'}
    allowed = {'RETRY_WITH_NEW_APPROACH', 'NEEDS_EVIDENCE', 'NEEDS_HUMAN_DECISION',
               'ABORT_STAGE', 'PLAN_CONTRACT_REVISION_REQUIRED'}
    if (not isinstance(answer, dict) or answer.get('status') not in allowed or
            type(answer.get('plan_contract_change_required')) is not bool or
            not isinstance(answer.get('new_approach'), dict) or
            not isinstance(answer.get('new_approach', {}).get('summary'), str) or
            not isinstance(answer.get('new_approach', {}).get('allowed_scope_change', []), list)):
        return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 1,
                'reason': 'rescue response нарушил контракт'}
    if answer['status'] == 'PLAN_CONTRACT_REVISION_REQUIRED' or answer['plan_contract_change_required']:
        answer['status'] = 'PLAN_CONTRACT_REVISION_REQUIRED'
        return dict(answer, turns=1)
    if answer.get('new_approach', {}).get('allowed_scope_change'):
        return {'status': 'NEEDS_HUMAN_DECISION', 'turns': 1,
                'reason': 'rescue предложил расширение scope; требуется отдельное решение по PlanContract'}
    return dict(answer, turns=1)


def _no_progress_stop(root, state, checkpoint, failure, stage_attempts,
                      attempt_row, after_attempt, report, config, catalog, save):
    """Build the bounded packet and either allow one rescue-guided retry or stop."""
    paths, diff_now = after_attempt[1], after_attempt[2]
    trigger = None
    review = attempt_row.get('review_result')
    if isinstance(review, dict):
        triggers = review.get('deterministic_triggers') or []
        trigger = next((item.get('trigger') for item in triggers
                        if isinstance(item, dict) and item.get('trigger')),
                       review.get('astra_trigger'))
    path_text = ' '.join(paths).lower()
    for marker, value in (('security', 'SECURITY_BOUNDARY_CHANGED'),
        ('auth', 'SECURITY_BOUNDARY_CHANGED'), ('schema', 'SCHEMA_CHANGED'),
        ('migration', 'MIGRATION_REQUIRED'), ('api/', 'PUBLIC_API_CHANGED'),
        ('platform', 'PLATFORM_CONTRACT_CHANGED'), ('thread', 'NEW_CONCURRENCY'),
        ('lock', 'NEW_CONCURRENCY'), ('database', 'PERSISTENCE_CHANGED')):
        if marker in path_text:
            trigger = value
            break
    rescue_class = anti_loop.classify_rescue(failure, trigger)
    packet = _save_rescue_packet(root, state, checkpoint, diff_now, paths, rescue_class)
    state['anti_loop_telemetry'] = {
        'attempt_count': len(stage_attempts), 'failure_fingerprint': failure.get('signature'),
        'approach_id': attempt_row.get('approach_id'),
        'progress_signals': attempt_row.get('progress_signals', []),
        'no_progress_detected': True, 'circuit_breaker_fired': True,
        'rescue_packet_bytes': state['rescue_packet']['bytes'],
        'rescue_model': rescue_class.get('model'), 'rescue_reasoning': rescue_class.get('reasoning'),
        'rescue_reason': rescue_class.get('reason'), 'rescue_turns': 0,
        'worker_turns_before_stop': len(stage_attempts),
        'repeated_failure_count': max(0, len(stage_attempts) - 1),
        'rescue_used': False, 'strong_rescue_used': False,
        'post_rescue_attempts': state.get('post_rescue_attempts', 0),
        'final_failure_status': None}
    state['rescue_status'] = 'RESCUE_REQUIRED'
    if not attempt_row.get('post_rescue_attempt') and rescue_class.get('automatic_model'):
        state['rescue_status'] = 'RESCUE_IN_PROGRESS'
        rescue_result = _run_rescue(root, state, checkpoint, packet, rescue_class,
                                    report, config, catalog, save)
        state['rescue_result'] = rescue_result
        state['anti_loop_telemetry']['rescue_turns'] = rescue_result.get('turns', 0)
        state['anti_loop_telemetry']['rescue_used'] = rescue_result.get('turns', 0) > 0
        state['anti_loop_telemetry']['strong_rescue_used'] = (
            rescue_result.get('turns', 0) > 0 and rescue_class.get('model') == 'gpt-6-astra')
        if rescue_result.get('status') == 'RETRY_WITH_NEW_APPROACH':
            state['rescue_status'] = 'POST_RESCUE_RETRY'
            state['rescue_instruction'] = rescue_result
            state['post_rescue_pending'] = True
            state['worker_failures'] = 1
            fresh_worker_session(state, 'Одна controlled попытка после rescue; worker получает только узкий совет.')
            save('RETRY', reason='Rescue предложил новую проверяемую стратегию; разрешена одна попытка.',
                 last_failure='verification')
            return 'RETRY'
        if rescue_result.get('status') == 'PLAN_CONTRACT_REVISION_REQUIRED':
            state['rescue_status'] = 'PLAN_REVISION_REQUIRED'
            state['anti_loop_telemetry']['final_failure_status'] = 'PLAN_CONTRACT_REVISION_REQUIRED'
            _record_terminal_failure(root, state, checkpoint, failure, stage_attempts)
            save('BLOCKED', reason='Rescue считает, что требуется новая версия PlanContract; тихое отклонение запрещено.',
                 last_failure='planning_contract',
                 next_step='Пересобрать Evidence Packet и заново утвердить PlanContract.')
            return 'STOP'
    if state.get('rescue_status') != 'POST_RESCUE_RETRY':
        state['rescue_status'] = 'NEEDS_HUMAN_DECISION'
    state['anti_loop_telemetry']['rescue_turns'] = len([
        item for item in state.get('rescue_turns', []) if item.get('consumed')])
    state['anti_loop_telemetry']['final_failure_status'] = state['rescue_status']
    _record_terminal_failure(root, state, checkpoint, failure, stage_attempts)
    offer_external_advisory(root, state)
    save('BLOCKED', reason='NO_PROGRESS_STOP: повторяется сбой без измеримого прогресса. Rescue Packet сохранён; следующая попытка автоматически не запускается.',
         last_failure='no_progress',
         next_step='Откройте Rescue Packet и предложенный запрос внешнему ChatGPT; после исправления причины начните новую задачу или выполните контролируемый resume.')
    return 'STOP'


def check_root(root):
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root.resolve():
        raise ValueError('Запускайте ai-dev в корне отдельного Git-репозитория.')



def snapshot(root):
    """Fingerprint tracked diff and untracked files before offering a commit."""
    digest = hashlib.sha256()
    for args in (['git', 'diff', '--binary', 'HEAD'], ['git', 'status', '--porcelain', '-z']):
        code, output = run(args, root)
        if code:
            raise ValueError(output)
        digest.update(output.encode('utf-8'))
    code, names = run(['git', 'ls-files', '--others', '--exclude-standard', '-z'], root)
    if code:
        raise ValueError(names)
    for name in sorted(filter(None, names.split('\0'))):
        path = root / name
        digest.update(name.encode('utf-8'))
        digest.update(str(path.lstat().st_mode).encode('ascii'))
        if path.is_symlink():
            digest.update(os.readlink(str(path)).encode('utf-8'))
        elif path.is_file():
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(65536), b''):
                    digest.update(chunk)
    return digest.hexdigest()


def _commit_impl(root, message):
    check_root(root)
    path = root / '.ai-dev/state.json'
    state = read(path)
    orchestration_task_id = state.get('orchestration_task_id')
    if orchestration_task_id:
        gate = orchestration.approval_status(root, 'commit', task_id=orchestration_task_id)
        if gate.get('status') != 'APPROVED':
            raise ValueError('PENDING_APPROVAL: commit approval required for orchestration task %s' % orchestration_task_id)
    if state['status'] != 'COMPLETED' or state.get('commit'):
        raise ValueError('Нужна завершённая задача без сохранённого коммита.')
    if git(root, 'rev-parse', 'HEAD') != state['base_head'] or snapshot(root) != state.get('snapshot'):
        raise ValueError('После проверки изменился код или HEAD; автоматическое сохранение отключено.')
    config = configure(root)
    if config != state['config']:
        raise ValueError('Конфигурация проверок изменилась после завершения задачи.')
    checked = verify(root, config, root / '.ai-dev', state=state)
    if not checked['ok'] or snapshot(root) != state['snapshot']:
        raise ValueError('Повторные проверки не прошли или изменили файлы.')
    if not git(root, 'status', '--porcelain'):
        raise ValueError('Нет изменений для коммита.')
    git(root, 'add', '-A')
    git(root, 'commit', '-m', message)
    state['commit'] = git(root, 'rev-parse', 'HEAD')
    write(path, state)
    write(root / '.ai-dev/runs' / state['id'] / 'state.json', state)
    try:
        project_memory.record_commit(root, state)
        state['project_memory_status'] = 'OK'
    except (OSError, ValueError) as memory_error:
        state['project_memory_status'] = 'DEGRADED'
        state['project_memory_error'] = str(memory_error)[:400]
        print('WARNING: Project Memory не обновлена после commit: ' + str(memory_error)[:300], flush=True)
    write(path, state)
    write(root / '.ai-dev/runs' / state['id'] / 'state.json', state)
    print('Сохранён коммит: ' + state['commit'])
    return 0


def _verification_phase(argv):
    tokens = [str(item).lower() for item in argv]
    text = ' '.join(tokens)
    unittest_index = tokens.index('unittest') if 'unittest' in tokens else -1
    unittest_targeted = unittest_index >= 0 and any(
        not item.startswith('-') for item in tokens[unittest_index + 1:])
    if ((unittest_index >= 0 and (not unittest_targeted or 'discover' in tokens)) or
            ('pytest' in text and not any(token.startswith('tests/') or token.startswith('test_')
                                          for token in tokens)) or
            text in ('npm test', 'npm run test', 'cargo test', 'go test ./...')):
        return 'full_test_suite', 'test'
    if any(token in text for token in ('test_', 'tests/', 'pytest')):
        return 'targeted_verification', 'test'
    return 'targeted_verification', 'local_tool'


def verify(root, config, directory, state=None):
    from .live import LiveSignal
    if not config['verify']:
        return {'ok': False, 'reason': 'Не настроены команды verify.', 'checks': []}
    results = []
    for index, argv in enumerate(config['verify']):
        phase_name, category = _verification_phase(argv)
        if state is None:
            try:
                code, output = run(argv, root, config['verify_timeout_seconds'],
                                   live=LiveSignal(root, phase='VERIFY', activity='Проверка %d из %d' % (index + 1, len(config['verify']))))
            except (OSError, subprocess.TimeoutExpired) as exc:
                code, output = -1, str(exc)
        else:
            timer = phase_telemetry.measure(state, phase_name, category)
            with timer:
                try:
                    code, output = run(argv, root, config['verify_timeout_seconds'],
                                       live=LiveSignal(root, phase='VERIFY', activity='Проверка %d из %d' % (index + 1, len(config['verify']))))
                except (OSError, subprocess.TimeoutExpired) as exc:
                    code, output = -1, str(exc)
            timer.row['command'] = argv[:32]
        (directory / ('verify-%s.log' % index)).write_text(output, encoding='utf-8')
        results.append({'command': argv, 'exit_code': code, 'tail': output[-6000:]})
    return {'ok': all(c['exit_code'] == 0 for c in results), 'checks': results}


def _review_model_turn(root, state, config, report, prompt, role, model, reasoning, log_path,
                       trigger=None):
    """Run one bounded, read-only review and account it as a real model turn."""
    ensure_turn_budget(state, config)
    if state.get('orchestration_task_id'):
        gate = orchestration.approval_status(root, 'model_turn', task_id=state['orchestration_task_id'])
        if gate.get('status') != 'APPROVED':
            raise ValueError('PENDING_APPROVAL: model_turn approval is required for review.')
        if model == 'gpt-6-astra':
            gate = orchestration.approval_status(root, 'expensive_escalation', task_id=state['orchestration_task_id'])
            if gate.get('status') != 'APPROVED':
                raise ValueError('PENDING_APPROVAL: expensive_escalation approval is required for Astra review.')
    decision = routing.choose(state.get('fresh_catalog') or discover(root, report['executable']),
        state['prompt'], kind='expert' if model == 'gpt-6-astra' else 'normal',
        model=model, reasoning=reasoning)
    decision.update(role=role, reason='PlanContract review cascade; read-only reviewer.')
    stages = state.get('plan_contract', {}).get('stages') or [{}]
    index = min(state.get('checkpoint_index', 0), len(stages) - 1)
    state.setdefault('review_turns', []).append({'role': role, 'model': model,
        'reasoning': reasoning, 'stage_id': stages[index].get('id'), 'trigger': trigger,
        'consumed': False})
    record = state['review_turns'][-1]
    review_config = dict(config, model=model, reasoning=reasoning, timeout_seconds=0,
                         detect_stall=False, _megaprog_task_planner=False)
    with waiting_turn(config, model, reasoning, lambda _: None,
                      owner={'project': root.name, 'task_id': state['id'], 'role': role}, state=state):
        record['consumed'] = True
        with phase_telemetry.measure(state, 'review_model_turn', 'model',
                                     overlap_group='review'):
            with isolate_generated_context(root) as isolation:
                result = codex.execute(root, review_config, report, prompt,
                                       isolation.relocate(log_path), None, sandbox='read-only')
        record['session_id'] = result.get('session_id')
        record['result'] = 'COMPLETED' if result.get('ok') else 'FAILED'
        record['usage'] = result.get('usage')
        attach_context(result, task_id=state['id'], stage_id=record.get('stage_id'),
            role=role, retry_number=state.get('checkpoint_attempts', 1),
            attempt_number=state.get('attempts', 0) + len(state['review_turns']),
            escalation_reason=decision.get('reason'), configured_model=model,
            configured_reasoning=reasoning)
        record['telemetry'] = result.get('telemetry')
    if not result.get('ok'):
        raise ValueError('Review model turn failed; see its raw Codex artifact.')
    parsed = review_cascade.parse_semantic_result(result.get('message'))
    return parsed, record


def run_review_cascade(root, state, config, report, directory, checkpoint, verification):
    """Run deterministic checks first; use Sol/Astra only on explicit gates."""
    contract = state.get('plan_contract')
    evidence_state = state.get('planning_evidence') or {}
    packet = None
    packet_path = evidence_state.get('packet_path')
    if isinstance(packet_path, str) and packet_path.startswith('.ai-dev/evidence/'):
        try:
            packet = evidence_packet.load_packet(root / packet_path)
        except (OSError, ValueError, TypeError):
            packet = None
    worker_result = state.get('stage_worker_result') if isinstance(state.get('stage_worker_result'), dict) else {}
    review_packet = review_cascade.build_review_packet(root, state, checkpoint, verification,
        packet=packet, worker_evidence=worker_result.get('evidence', []))
    review_dir = directory / 'review'
    review_dir.mkdir(parents=True, exist_ok=True)
    packet_path = review_dir / 'review-packet.json'
    packet_path.write_text(json.dumps(review_packet, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    deterministic = review_cascade.deterministic_status(review_packet)
    semantic = astra = None
    turns = []
    high_triggers = sorted({item.get('trigger') for item in review_packet.get('triggered_reviews', [])}
                           & review_cascade.HIGH_COST_TRIGGERS)
    if review_cascade.required_tier(review_packet) != 'astra':
        high_triggers = []
    deviation = review_packet.get('implementation', {}).get('reported_plan_deviation')
    if not (isinstance(deviation, dict) and deviation.get('architectural') is True):
        high_triggers = [item for item in high_triggers if item != 'PLAN_DEVIATION']
    try:
        if deterministic not in ('BLOCKED', 'FIX_REQUIRED', 'UNKNOWN', 'PASS_NO_MODEL_REVIEW'):
            if high_triggers:
                trigger = high_triggers[0] if len(high_triggers) == 1 else high_triggers
                narrow = review_cascade.build_astra_packet(review_packet, trigger=trigger)
                prompt = review_cascade.astra_prompt(narrow)
                astra, turn = _review_model_turn(root, state, config, report, prompt,
                    'astra_review', 'gpt-6-astra', 'medium', review_dir / 'astra-codex.jsonl',
                    trigger=','.join(high_triggers))
                turns.append(turn)
                review_cascade.validate_acceptance_results(astra, review_packet,
                    allowed_refs=narrow.get('available_evidence_refs', []))
            else:
                catalog = discover(root, report['executable'])
                state['fresh_catalog'] = catalog
                choice = routing.choose(catalog, state['prompt'], kind='normal',
                                        model='gpt-6-sol', reasoning='medium')
                if choice.get('model') != 'gpt-6-sol':
                    raise ValueError('Sol Medium is unavailable for semantic review; no fallback was selected.')
                prompt = review_cascade.semantic_prompt(review_packet, tier='sol-medium')
                semantic, turn = _review_model_turn(root, state, config, report, prompt,
                    'semantic_review', 'gpt-6-sol', 'medium', review_dir / 'sol-codex.jsonl')
                turns.append(turn)
                review_cascade.validate_acceptance_results(semantic, review_packet)
                if semantic.get('astra_required'):
                    trigger = semantic['astra_trigger']
                    narrow = review_cascade.build_astra_packet(review_packet, trigger=trigger,
                                                               objection=semantic['astra_reason'])
                    prompt = review_cascade.astra_prompt(narrow)
                    astra, turn = _review_model_turn(root, state, config, report, prompt,
                        'astra_review', 'gpt-6-astra', 'medium', review_dir / 'astra-codex.jsonl',
                        trigger=trigger)
                    turns.append(turn)
                    review_cascade.validate_acceptance_results(astra, review_packet,
                        allowed_refs=narrow.get('available_evidence_refs', []))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        final = review_cascade.result(review_packet, semantic=semantic, astra=astra, turns=turns)
        final.update(status='UNKNOWN', review_error=str(exc)[:500])
    else:
        final = review_cascade.result(review_packet, semantic=semantic, astra=astra, turns=turns)
    final['review_packet_path'] = str(packet_path.relative_to(root))
    final_path = review_dir / 'review-result.json'
    final_path.write_text(json.dumps(final, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    state.setdefault('review_cascade', []).append(final)
    state.pop('fresh_catalog', None)
    return final


def _planning_evidence_packet(root, state):
    """Load a fresh saved packet or rebuild it locally; never calls a model."""
    evidence_state = state.get('planning_evidence') or {}
    relative = evidence_state.get('packet_path')
    packet = None
    gate = None
    rebuild_reason = None
    if (not state.get('pending_evidence_requests') and isinstance(relative, str) and
            relative.startswith('.ai-dev/evidence/') and relative.endswith('.json') and
            Path(relative).name[:-5].isalnum()):
        try:
            candidate = evidence_packet.load_packet(root / relative)
            if candidate.get('packet_id') == Path(relative).stem:
                with phase_telemetry.measure(state, 'completeness_check', 'local_tool'):
                    candidate_gate = planning_pipeline.completeness_gate(root, candidate)
                if candidate_gate.get('planner_may_run'):
                    packet, gate = candidate, candidate_gate
                else:
                    rebuild_reason = 'saved Evidence Packet is stale or blocked'
        except (OSError, ValueError, TypeError):
            rebuild_reason = 'saved Evidence Packet is unavailable or invalid'
    requests = state.get('pending_evidence_requests') or []
    if packet is None:
        previous = None
        if isinstance(relative, str) and relative.startswith('.ai-dev/evidence/'):
            try:
                previous = evidence_packet.load_packet(root / relative)
            except (OSError, ValueError, TypeError):
                previous = None
        with phase_telemetry.measure(state, 'evidence_build', 'local_tool'):
            packet = evidence_packet.build_packet(root, task_id=state['id'], extra_requests=requests,
                initial_evidence_seeds=state.get('initial_evidence_seeds'))
        with phase_telemetry.measure(state, 'completeness_check', 'local_tool'):
            gate = planning_pipeline.completeness_gate(root, packet)
        if requests:
            added = bool(previous and planning_pipeline.added_evidence(previous, packet))
            state.setdefault('planning_telemetry', {})['evidence_reentry_added_evidence'] = added
            if not added:
                lookup_rows = packet.get('selection_hygiene', {}).get('evidence_requests', [])
                requested_paths = [row for row in lookup_rows if row.get('kind') == 'path']
                missing_paths = [row for row in requested_paths if row.get('found') is not True]
                lookup_class = 'EVIDENCE_NOT_FOUND' if requested_paths and len(missing_paths) == len(requested_paths) else 'NO_NEW_EVIDENCE'
                state['evidence_reentry'] = {'status': 'NO_NEW_EVIDENCE',
                    'error_class': lookup_class,
                    'requests': requests,
                    'rejected_requests': state.get('evidence_request_rejections', []),
                    'lookup_results': lookup_rows,
                    'packet_id': packet.get('packet_id')}
                raise PlanningGateBlocked('Planner requested evidence, but the bounded local lookup found no new evidence; stopped without another planner turn.')
            state['extra_evidence_rounds'] = 1
            state.pop('pending_evidence_requests', None)
            if state.get('pending_contract_path_closure'):
                state.setdefault('planning_telemetry', {})['contract_path_evidence_added'] = True
            state['evidence_reentry'] = {'status': 'NEW_EVIDENCE_FOUND',
                'requests': requests,
                'rejected_requests': state.get('evidence_request_rejections', []),
                'packet_id': packet.get('packet_id')}
        saved = evidence_packet.save_packet(root, packet)
        relative = saved.relative_to(root).as_posix()
        evidence_state = {'packet_id': packet['packet_id'], 'packet_path': relative,
            'evidence_bytes': packet['budget'].get('final_bytes'),
            'evidence_estimated_tokens': packet['budget'].get('estimated_tokens'),
            'evidence_status': gate.get('status'), 'gate': gate,
            'rebuild_reason': rebuild_reason}
        state['planning_evidence'] = evidence_state
    else:
        evidence_state = dict(evidence_state)
        evidence_state['gate'] = gate
        state['planning_evidence'] = evidence_state
    evidence_state = state['planning_evidence']
    evidence_state['evidence_status'] = gate.get('status')
    telemetry = state.setdefault('planning_telemetry', {})
    reference_fragments = packet.get('scope', {}).get('reference_fragments', [])
    telemetry.setdefault('contract_path_closure_attempted', False)
    telemetry.setdefault('contract_path_requests', 0)
    telemetry.setdefault('contract_path_valid', 0)
    telemetry.setdefault('contract_path_rejected', 0)
    telemetry.setdefault('contract_path_evidence_added', False)
    telemetry.setdefault('contract_path_reentry', 0)
    telemetry.setdefault('planner_semantic_correction_turns', 0)
    telemetry.setdefault('evidence_reentry_turns', 0)
    telemetry['explicit_task_reference_paths'] = list(dict.fromkeys(
        row.get('path') for row in reference_fragments
        if row.get('selection_reason') == 'EXPLICIT_TASK_REFERENCE' and row.get('path')))
    telemetry.update({'evidence_packet_id': packet.get('packet_id'),
        'evidence_bytes': packet.get('budget', {}).get('final_bytes'),
        'evidence_estimated_tokens': packet.get('budget', {}).get('estimated_tokens'),
        'evidence_status': gate.get('status'), 'completeness_gate_used': 'deterministic',
        'completeness_model_turns': 0,
        'planner_requested_more_evidence': bool(state.get('planner_requested_more_evidence')),
        'evidence_request_count': telemetry.get('evidence_request_count', 0),
        'evidence_requests_valid': telemetry.get('evidence_requests_valid', 0),
        'evidence_requests_rejected': telemetry.get('evidence_requests_rejected', 0),
        'evidence_request_error_class': telemetry.get('evidence_request_error_class', 'NONE'),
        'evidence_reentry_attempted': telemetry.get('evidence_reentry_attempted', False),
        'evidence_reentry_added_evidence': telemetry.get('evidence_reentry_added_evidence', False),
        'extra_evidence_rounds': state.get('extra_evidence_rounds', 0),
        'contract_path_closure_attempted': telemetry.get('contract_path_closure_attempted', False),
        'contract_path_requests': telemetry.get('contract_path_requests', 0),
        'contract_path_valid': telemetry.get('contract_path_valid', 0),
        'contract_path_rejected': telemetry.get('contract_path_rejected', 0),
        'contract_path_evidence_added': telemetry.get('contract_path_evidence_added', False),
        'contract_path_reentry': telemetry.get('contract_path_reentry', 0),
        'planner_semantic_correction_turns': telemetry.get('planner_semantic_correction_turns', 0),
        'evidence_reentry_turns': telemetry.get('evidence_reentry_turns', 0),
        'explicit_task_reference_paths': telemetry.get('explicit_task_reference_paths', []),
        'plan_contract_id': state.get('plan_contract', {}).get('contract_id')})
    return packet, gate


def _record_plan_validation_telemetry(state, validation, *, error=None):
    """Keep the planner result classification in the existing task telemetry."""
    diagnostics = validation or {}
    telemetry = state.setdefault('planning_telemetry', {})
    telemetry.update({
        'planner_response_status': diagnostics.get('parse_status', 'REJECTED'),
        'planner_parse_error_class': diagnostics.get('error_class', 'UNKNOWN'),
        'planner_normalization_actions': list(diagnostics.get('normalization_actions') or []),
        'normalization_repaired': diagnostics.get('parse_status') == 'REPAIRED',
        'plan_contract_schema_valid': diagnostics.get('schema_valid'),
        'plan_contract_semantic_valid': diagnostics.get('semantic_valid'),
        'plan_contract_validation_error': (str(error)[:500] if error else None),
        'planner_retry_reason': diagnostics.get('retry_reason', 'unknown'),
    })


def _recover_saved_plancontract(root, state):
    """Revalidate one saved schema-only planner response against its fresh packet."""
    if (not state.get('planner_format_error') or state.get('planner_error_class') !=
            'PLAN_CONTRACT_SCHEMA_ERROR'):
        return False
    attempts = state.get('planning_attempts') or []
    if len(attempts) != 1:
        return False
    attempt = attempts[0]
    codex_result = attempt.get('codex') or {}
    if codex_result.get('exit_code') != 0 or not codex_result.get('completed'):
        return False
    validation = attempt.get('planner_validation') or {}
    evidence = attempt.get('planning_evidence') or {}
    raw_ref = validation.get('raw_artifact')
    packet_ref = evidence.get('packet_path')
    if not isinstance(raw_ref, str) or not isinstance(packet_ref, str):
        return False
    root_path = Path(root).resolve()
    expected_run = (root_path / '.ai-dev' / 'runs' / state['id']).resolve()
    try:
        raw_path = (root_path / raw_ref).resolve(strict=True)
        packet_path = (root_path / packet_ref).resolve(strict=True)
        raw_path.relative_to(expected_run)
        packet_path.relative_to((root_path / '.ai-dev' / 'evidence').resolve())
    except (OSError, ValueError):
        return False
    try:
        packet = json.loads(packet_path.read_text(encoding='utf-8'))
        if packet.get('packet_id') != evidence.get('packet_id'):
            return False
        stale = evidence_packet.check_packet(root_path, packet, task_contract_only=True)
        if stale.get('status') != 'VALID':
            return False
        raw_messages = []
        for line in raw_path.read_text(encoding='utf-8').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get('item') or {}
            if (event.get('type') == 'item.completed' and
                    item.get('type') == 'agent_message' and isinstance(item.get('text'), str)):
                raw_messages.append(item['text'])
        saved_message = codex_result.get('message')
        if not raw_messages or raw_messages[-1] != saved_message:
            return False
        parsed = planning_pipeline.parse_response(saved_message, state['id'], packet, root=root_path)
    except (OSError, UnicodeError, ValueError, TypeError,
            planning.PlanParseError, planning_pipeline.ContractPathClosureRequired,
            planning_pipeline.ContractPathInvalid):
        return False
    if parsed.get('kind') != 'plan':
        return False
    state['plan_contract'] = parsed['contract']
    state['plan'] = parsed['plan']
    state.pop('planner_format_error', None)
    state.pop('planner_error_class', None)
    state.pop('plan_issue', None)
    diagnostics = {
        'parse_status': 'REPAIRED', 'error_class': 'NONE',
        'normalization_actions': list(parsed.get('normalization_actions', [])) +
            ['revalidated_saved_response_after_schema_compatibility_fix'],
        'schema_valid': True, 'semantic_valid': True, 'retry_required': False,
        'retry_reason': 'none', 'validation_error': None,
        'raw_artifact': raw_ref, 'packet_id': packet.get('packet_id'),
        'plan_contract_id': parsed['contract']['contract_id'],
    }
    attempt['planner_validation'] = diagnostics
    state.setdefault('planner_validation_history', []).append(dict(diagnostics))
    _record_plan_validation_telemetry(state, diagnostics)
    state.setdefault('planning_telemetry', {}).update({
        'planner_turns': len(attempts), 'plan_contract_id': parsed['contract']['contract_id']})
    return True


def prepare_plan(root, state, config, catalog, report, save):
    if state.get('plan'):
        return
    if state.get('planner_format_error'):
        if _recover_saved_plancontract(root, state):
            save('PREFLIGHT', reason='Сохранённый ответ PlanContract повторно проверен на свежем Evidence Packet после согласования schema; новый model turn не запускался.')
            return
        if state.get('chatgpt_plan_offer', {}).get('status') != 'OFFERED':
            try:
                state['chatgpt_plan_offer'] = create_plan_offer(root, state)
            except OSError:
                pass
        raise PlanningFormatExhausted(
            'Неустранимый FORMAT_ERROR уже сохранён; используйте plan-import вместо повтора планировщика.')
    if len(state.get('planning_attempts', [])) >= 2:
        if not state.get('plan_issue'):
            raise ValueError('Две попытки планирования исчерпаны; изучите журнал до продолжения.')
        if state.get('chatgpt_plan_offer', {}).get('status') != 'OFFERED':
            try:
                state['chatgpt_plan_offer'] = create_plan_offer(root, state)
            except OSError:
                pass
        raise PlanningFormatExhausted('Две попытки планирования исчерпаны; используйте готовый запрос ChatGPT и plan-import.')
    while len(state.get('planning_attempts', [])) < 2:
        ensure_turn_budget(state, config)
        new_pipeline = state.get('planning_pipeline_version') == 1
        packet = gate = None
        if new_pipeline:
            packet, gate = _planning_evidence_packet(root, state)
            seed_summary = packet.get('scope', {}).get('initial_evidence_seeds', [])
            telemetry = state.setdefault('planning_telemetry', {})
            telemetry.update({'evidence_seed_count': len(seed_summary),
                'required_seed_count': sum(row.get('priority') == 'required' for row in seed_summary),
                'optional_seed_count': sum(row.get('priority') == 'optional' for row in seed_summary),
                'valid_seed_count': len(seed_summary),
                'rejected_seed_count': 0,
                'missing_seed_count': sum(row.get('status') != 'SATISFIED' for row in seed_summary),
                'satisfied_seed_count': sum(row.get('status') == 'SATISFIED' for row in seed_summary),
                'automatic_evidence_items': sum(len(packet.get('scope', {}).get(key, [])) for key in
                    ('source_fragments', 'test_fragments', 'reference_fragments')),
                'seed_bytes': sum(row.get('excerpt_bytes', 0) for row in
                    packet.get('scope', {}).get('seeded_fragments', [])),
                'planner_started_after_seed_validation': False})
            if not gate.get('planner_may_run'):
                raise PlanningGateBlocked('Completeness gate BLOCKED planner: %s' %
                    '; '.join(gate.get('reasons', [])[:6] or gate.get('stale_check', {}).get('reasons', [])))
        route = state.get('routing') or {}
        planner_scope = route.get('pin_scope', 'task') in ('planner', 'task')
        selected_model = route.get('model') or (config['model'] if config['model'] != 'auto' else None)
        task_kind = ('expert' if planner_scope and selected_model == routing.ORDER[-1] else
                     routing.classify(state['prompt']) if route.get('kind') == 'auto' else
                     route.get('kind', 'normal'))
        requested_model = route.get('model') or (config['model'] if config['model'] != 'auto' else None)
        requested_reasoning = route.get('reasoning') or (config['reasoning'] if requested_model else None)
        pinned_model = requested_model if planner_scope else None
        pinned_reasoning = requested_reasoning if pinned_model else None
        if pinned_model and pinned_reasoning:
            decision = routing.choose(catalog, state['prompt'], kind=task_kind,
                                      model=pinned_model, reasoning=pinned_reasoning)
            decision['role'] = 'planner'
            decision['reason'] = 'Явный выбор модели и режима для планирования.'
        else:
            decision = planning.strong(catalog,
                                       medium=bool(state.get('plan_issue')) or
                                           (planner_scope and route.get('reasoning') == 'medium'),
                                       task_kind=task_kind)
        directory = root / '.ai-dev/runs' / state['id'] / ('plan-%d' % (len(state.get('planning_attempts', [])) + 1))
        directory.mkdir(parents=True, exist_ok=True)
        if new_pipeline:
            dossier, dossier_bytes, _ = planning_pipeline.render_planner_input(packet, gate)
            plan_rules = planning_pipeline.planner_instructions(packet, gate,
                reentry=bool(state.get('extra_evidence_rounds'))) + '\n' + dossier
            state.setdefault('planning_telemetry', {}).update({
                'planner_input_bytes': dossier_bytes, 'planner_model': decision.get('model'),
                'planner_reasoning': decision.get('reasoning'),
                'planner_turns': len(state.get('planning_attempts', [])) + 1,
                'evidence_packet_id': packet.get('packet_id'), 'evidence_status': gate.get('status'),
                'extra_evidence_rounds': state.get('extra_evidence_rounds', 0)})
        else:
            plan_rules = planning.PLAN_INSTRUCTIONS
        instructions, metrics = assemble(state['prompt'], config['verify'],
            plan_rules + '\n' + SELF_IMPROVEMENT_PROMPT + '\n' + project_scope_manifest(root) +
            ('\nCorrect the previous invalid plan: ' + state['plan_issue'] if state.get('plan_issue') else ''),
            budget_bytes=config.get('context_budget_bytes', DEFAULT_BUDGET_BYTES), planning=True)
        if new_pipeline and metrics.get('prompt_bytes', 0) > config.get('context_budget_bytes', DEFAULT_BUDGET_BYTES):
            raise PlanningGateBlocked('Evidence Packet and mandatory planner instructions exceed the configured prompt budget.')
        state['active_decision'] = decision
        reentry_reason = state.get('planning_reentry_reason')
        with waiting_turn(config, decision['model'], decision['reasoning'],
                lambda evidence: save('WAITING_FOR_SLOT', guard=evidence, reason='Планировщик ожидает слот.'),
                owner={'project': root.name, 'task_id': state['id']}, state=state) as guard:
            attempt = {'decision': decision, 'prompt_metrics': metrics, 'guard': guard.evidence}
            if reentry_reason:
                attempt['planner_reentry_reason'] = reentry_reason
                turn_telemetry = state.setdefault('planning_telemetry', {})
                if reentry_reason == 'SEMANTIC_CORRECTION':
                    turn_telemetry['planner_semantic_correction_turns'] = (
                        turn_telemetry.get('planner_semantic_correction_turns', 0) + 1)
                elif reentry_reason in ('EVIDENCE_REQUEST', 'CONTRACT_PATH_CLOSURE'):
                    turn_telemetry['evidence_reentry_turns'] = (
                        turn_telemetry.get('evidence_reentry_turns', 0) + 1)
                    if reentry_reason == 'CONTRACT_PATH_CLOSURE':
                        turn_telemetry['contract_path_reentry'] = (
                            turn_telemetry.get('contract_path_reentry', 0) + 1)
            state.pop('planning_reentry_reason', None)
            if new_pipeline:
                attempt['planning_evidence'] = dict(state.get('planning_evidence') or {})
                attempt['planning_telemetry'] = dict(state.get('planning_telemetry') or {})
            state.setdefault('planning_attempts', []).append(attempt)
            if new_pipeline:
                state.setdefault('planning_telemetry', {})['planner_started_after_seed_validation'] = True
            save('RUNNING', reason='Планирование: %s / %s' % (decision['model'], decision['reasoning']))
            with phase_telemetry.measure(state, 'planning_model_turn', 'model',
                                         overlap_group='planning'):
                with isolate_generated_context(root) as isolation:
                    result = codex.execute(root, dict(config, model=decision['model'], reasoning=decision['reasoning'],
                                                      _megaprog_task_planner=True),
                                           report, instructions, isolation.relocate(directory / 'codex.jsonl'),
                                           sandbox='read-only')
            attempt['codex'] = result
            attach_context(result, task_id=state['id'], stage_id='planning',
                           role='planner', retry_number=len(state['planning_attempts']),
                           attempt_number=len(state['planning_attempts']),
                           escalation_reason=decision.get('reason'),
                           configured_model=decision.get('model'),
                           configured_reasoning=decision.get('reasoning'))
            if not result['ok']:
                raise ValueError('Планировщик не завершил ход; детали сохранены в отчёте.')
            try:
                if new_pipeline:
                    parsed = planning_pipeline.parse_response(result.get('message'), state['id'], packet,
                                                              root=root)
                    if parsed['kind'] == 'needs_evidence':
                        state['planner_requested_more_evidence'] = True
                        request_telemetry = state.setdefault('planning_telemetry', {})
                        request_telemetry.update({
                            'planner_requested_more_evidence': True,
                            'evidence_request_count': parsed['request_count'],
                            'evidence_requests_valid': parsed['valid_count'],
                            'evidence_requests_rejected': parsed['rejected_count'],
                            'evidence_request_error_class': parsed['error_class'],
                            'evidence_reentry_attempted': False,
                            'evidence_reentry_added_evidence': False})
                        state['evidence_request_rejections'] = parsed['rejected']
                        validation = {'parse_status': 'ACCEPTED_REQUEST', 'error_class': parsed['error_class'],
                            'normalization_applied': bool(parsed.get('normalization_actions')),
                            'normalization_actions': parsed.get('normalization_actions', []),
                            'schema_valid': True, 'semantic_valid': None, 'retry_required': False,
                            'requests': parsed['requests'],
                            'rejected_requests': parsed['rejected'],
                            'evidence_request_count': parsed['request_count'],
                            'evidence_requests_valid': parsed['valid_count'],
                            'evidence_requests_rejected': parsed['rejected_count'],
                            'retry_required': False, 'retry_reason': 'none',
                            'validation_error': None,
                            'raw_artifact': str(directory.joinpath('codex.jsonl').relative_to(root))}
                        _record_plan_validation_telemetry(state, validation)
                        attempt['planner_validation'] = validation
                        state.setdefault('planner_validation_history', []).append(validation)
                        if not parsed['requests']:
                            state['evidence_reentry'] = {'status': 'NO_VALID_REQUESTS',
                                'requests': [], 'rejected_requests': parsed['rejected']}
                            raise PlanningGateBlocked('Planner requested evidence, but all lookup requests were invalid or ambiguous; stopped without another planner turn.')
                        if (state.get('extra_evidence_rounds', 0) >= 1 or
                                len(state.get('planning_attempts', [])) >= 2):
                            state['evidence_reentry'] = {'status': 'LIMIT_REACHED',
                                'requests': parsed['requests'],
                                'rejected_requests': parsed['rejected'],
                                'error_class': 'REENTRY_BUDGET_EXHAUSTED'}
                            raise PlanningGateBlocked('Planner requested evidence after the single shared planning re-entry budget was used; stopped.')
                        state['pending_evidence_requests'] = parsed['requests']
                        state['planning_reentry_reason'] = 'EVIDENCE_REQUEST'
                        request_telemetry['evidence_reentry_attempted'] = True
                        save('PREFLIGHT', reason='Planner requested one bounded evidence lookup; rebuilding the packet locally.')
                        continue
                    if parsed['kind'] == 'plan':
                        parsed_plan = parsed['plan']
                        state['plan_contract'] = parsed['contract']
                        state.setdefault('planning_telemetry', {})['plan_contract_id'] = parsed['contract']['contract_id']
                        validation = {'parse_status': parsed.get('parse_status',
                                'REPAIRED' if parsed.get('normalization_actions') else 'ACCEPTED'),
                            'error_class': parsed.get('error_class', 'NONE'),
                            'normalization_applied': bool(parsed.get('normalization_actions')),
                            'normalization_actions': parsed.get('normalization_actions', []),
                            'schema_valid': parsed.get('schema_valid', True),
                            'semantic_valid': parsed.get('semantic_valid', True),
                            'retry_required': parsed.get('retry_required', False),
                            'retry_reason': parsed.get('retry_reason', 'none'),
                            'validation_error': parsed.get('validation_error'),
                            'packet_id': packet.get('packet_id'),
                            'plan_contract_id': parsed['contract']['contract_id']}
                    else:
                        parsed_plan = parsed['plan']
                        validation = parsed['validation']
                else:
                    parsed_plan, validation = planning.parse_plan_detailed(result.get('message'))
            except planning_pipeline.ContractPathClosureRequired as exc:
                validation = dict(exc.diagnostics)
                validation.update({'requested_paths': exc.paths,
                    'raw_artifact': str(directory.joinpath('codex.jsonl').relative_to(root))})
                attempt['planner_validation'] = validation
                state.setdefault('planner_validation_history', []).append(validation)
                _record_plan_validation_telemetry(state, validation, error=str(exc))
                state['plan_issue'] = str(exc)[:400]
                telemetry = state.setdefault('planning_telemetry', {})
                telemetry.update({'contract_path_closure_attempted': True,
                    'contract_path_requests': len(exc.paths),
                    'contract_path_valid': len(exc.paths),
                    'contract_path_rejected': 0})
                if (state.get('extra_evidence_rounds', 0) >= 1 or
                        len(state.get('planning_attempts', [])) >= 2):
                    state['evidence_reentry'] = {'status': 'LIMIT_REACHED',
                        'error_class': 'CONTRACT_PATH_NEEDS_EVIDENCE',
                        'requests': [{'kind': 'path', 'query': path,
                                      'reason': 'Read the exact PlanContract path before accepting it.'}
                                     for path in exc.paths]}
                    raise PlanningGateBlocked('PlanContract path needs evidence, but the single shared planning re-entry budget is exhausted.')
                requests = [{'kind': 'path', 'query': path,
                             'reason': 'Read this existing PlanContract path before accepting it.'}
                            for path in exc.paths]
                state['pending_evidence_requests'] = requests
                state['pending_contract_path_closure'] = True
                state['planning_reentry_reason'] = 'CONTRACT_PATH_CLOSURE'
                telemetry['evidence_reentry_attempted'] = True
                telemetry['contract_path_reentry'] = 0
                save('PREFLIGHT', reason='PlanContract named existing paths missing from Evidence Packet; adding bounded local evidence before one planner re-entry.')
                continue
            except planning_pipeline.ContractPathInvalid as exc:
                validation = dict(exc.diagnostics)
                validation.update({'path': exc.path,
                    'raw_artifact': str(directory.joinpath('codex.jsonl').relative_to(root))})
                attempt['planner_validation'] = validation
                state.setdefault('planner_validation_history', []).append(validation)
                _record_plan_validation_telemetry(state, validation, error=str(exc))
                telemetry = state.setdefault('planning_telemetry', {})
                telemetry.update({'contract_path_closure_attempted': True,
                    'contract_path_requests': max(1, telemetry.get('contract_path_requests', 0)),
                    'contract_path_valid': 0, 'contract_path_rejected': 1})
                state['evidence_reentry'] = {'status': 'CONTRACT_PATH_INVALID',
                    'error_class': 'CONTRACT_PATH_INVALID', 'path': exc.path}
                raise PlanningGateBlocked(str(exc))
            except planning.PlanParseError as exc:
                validation = dict(exc.diagnostics)
                if validation['retry_required'] and len(state['planning_attempts']) >= 2:
                    validation['retry_required'] = False
                    validation['retry_reason'] = 'Ограниченный бюджет планировщика исчерпан; остановка без третьего хода.'
                validation['raw_artifact'] = str(directory.joinpath('codex.jsonl').relative_to(root))
                _record_plan_validation_telemetry(state, validation, error=str(exc))
                attempt['planner_validation'] = validation
                state.setdefault('planner_validation_history', []).append(validation)
                state['plan_issue'] = str(exc)[:400]
                state['planner_error_class'] = validation['error_class']
                if not validation['retry_required']:
                    state['planner_format_error'] = True
                    try:
                        state['chatgpt_plan_offer'] = create_plan_offer(root, state)
                    except OSError:
                        pass
                    raise PlanningFormatExhausted(
                        'Ответ планировщика отклонён (%s): %s; сильный повтор остановлен.' %
                        (validation.get('error_class', 'UNKNOWN'), str(exc)))
                save('PREFLIGHT', reason='План семантически не прошёл контракт; разрешена одна ограниченная корректировка.')
                state['planning_reentry_reason'] = 'SEMANTIC_CORRECTION'
                continue
            validation['raw_artifact'] = str(directory.joinpath('codex.jsonl').relative_to(root))
            if new_pipeline:
                validation.setdefault('parse_status', 'ACCEPTED')
                validation.setdefault('error_class', 'NONE')
                validation.setdefault('schema_valid', None)
                validation.setdefault('semantic_valid', None)
                validation.setdefault('retry_required', False)
                validation.setdefault('retry_reason', 'none')
                validation.setdefault('normalization_actions', [])
                _record_plan_validation_telemetry(state, validation)
            attempt['planner_validation'] = validation
            state.setdefault('planner_validation_history', []).append(validation)
            state['plan'] = parsed_plan
            state.pop('plan_issue', None)
            state.pop('planner_error_class', None)
            state.pop('planner_format_error', None)
            if new_pipeline:
                state.setdefault('planning_telemetry', {})['plan_contract_id'] = state.get('plan_contract', {}).get('contract_id')
            save('PREFLIGHT', reason='Сильный план готов; передаю исполнителю.')
            return
    try:
        state['chatgpt_plan_offer'] = create_plan_offer(root, state)
    except OSError:
        pass
    raise PlanningFormatExhausted('План не прошёл проверку за две попытки; исполнение не запускалось.')


def fresh_worker_session(state, reason):
    if state.get('session_id'):
        state.setdefault('session_handoffs', []).append({
            'from_session': state['session_id'], 'after_attempt': state['attempts'], 'reason': reason})
    state['session_id'] = None
    state.pop('compaction_summary', None)


def failure_evidence(root, state, checked, previous):
    from .verification_evidence import summarize, compare
    progress = compare(previous, checked)
    state['last_progress'] = progress
    facts = state.setdefault('failure_evidence', [])
    decision = state.get('active_decision', {})
    facts.append({'attempt': state['attempts'], 'model': decision.get('model'),
                  'reasoning': decision.get('reasoning'), 'verification': summarize(checked),
                  'progress': progress, 'checkpoint_issue': state.get('checkpoint_issue')})
    state['failure_evidence'] = facts[-2:]


def offer_external_advisory(root, state):
    """Offer a user-controlled ChatGPT review after a real repeated stall."""
    if state.get('chatgpt_offer', {}).get('status') == 'OFFERED':
        return
    checkpoint = None
    steps = (state.get('plan') or {}).get('steps') or []
    index = state.get('checkpoint_index', 0)
    if type(index) is int and 0 <= index < len(steps) and isinstance(steps[index], dict):
        checkpoint = steps[index]
    text = ' '.join(str((checkpoint or {}).get(key, '')).lower() for key in ('title', 'acceptance'))
    state['advisory_reason'] = ('architecture deadlock' if 'architecture' in text or 'архитектур' in text
                                else 'repeated_stall')
    try:
        offer = create_chatgpt_offer(root, state)
    except OSError:
        return
    if offer:
        state['chatgpt_offer'] = offer


def supersede_external_advisory(state):
    offer = state.get('chatgpt_offer')
    if isinstance(offer, dict) and offer.get('status') == 'OFFERED':
        offer['status'] = 'SUPERSEDED'
        offer['note'] = 'Текущий этап прошёл проверку до получения внешнего совета.'


def worker_access_failure(message):
    """Recognize an explicit worker access failure before old tests can pass it."""
    if not isinstance(message, str):
        return False
    text = ' '.join(message.lower().split())
    denied = any(phrase in text for phrase in (
        "couldn't inspect or modify the repository", "could not inspect or modify the repository",
        'initial file-list command was rejected by policy',
        'environment is read-only', 'environment is read only',
        'cannot access the repository', 'could not access the repository'))
    unfinished = any(phrase in text for phrase in (
        'no files were changed', 'no files were modified', 'could not run the requested verification',
        "couldn't run the requested verification", 'could not modify the repository',
        "couldn't modify the repository"))
    return denied and unfinished


def _task_impl(root, prompt=None, kind="auto", model=None, reasoning=None, orchestration_task_id=None,
               pin_scope=None, allow_no_progress_resume=False, initial_evidence_seeds=None):
    context_started = time.perf_counter()
    context_started_at = phase_telemetry.timestamp()
    config = configure(root)
    check_root(root)
    path = root / '.ai-dev/state.json'
    seed_rows = evidence_packet.validate_initial_evidence_seeds(initial_evidence_seeds or [])
    if prompt is not None:
        if not prompt.strip():
            raise ValueError('Задача не может быть пустой.')
        old = read(path) if path.exists() else {}
        if old.get('status') not in (None, 'COMPLETED', 'CANCELLED', 'IDLE'):
            raise ValueError('Есть незавершённая задача. Используйте resume или сохраните/разберите её состояние.')
        if git(root, 'status', '--porcelain'):
            raise ValueError('Перед новой задачей сохраните изменения в Git; рабочая папка должна быть чистой.')
        default_pin_scope = 'planner' if config.get('strong_planning', True) else 'worker'
        effective_pin_scope = pin_scope or default_pin_scope
        if effective_pin_scope not in ('planner', 'worker', 'task'):
            raise ValueError('pin_scope должен быть planner, worker или task.')
        state = {'id': uuid.uuid4().hex, 'prompt': prompt, 'status': 'PREFLIGHT',
                 'attempts': 0, 'session_id': None, 'base_head': git(root, 'rev-parse', 'HEAD'),
                 'config': config, 'created_at': time.time(),
                 'routing': {'kind': kind, 'model': model, 'reasoning': reasoning,
                             'pin_scope': effective_pin_scope}, 'decisions': [],
                 'routing_policy': 'plan-first-v1' if config.get('strong_planning', True) else 'legacy',
                 'planning_pipeline_version': 1 if config.get('strong_planning', True) else None,
                 'extra_evidence_rounds': 0,
                 'orchestration_task_id': orchestration_task_id,
                 'initial_evidence_seeds': seed_rows,
                 'planning_telemetry': {'evidence_seed_count': len(seed_rows),
                     'required_seed_count': sum(row['priority'] == 'required' for row in seed_rows),
                     'optional_seed_count': sum(row['priority'] == 'optional' for row in seed_rows),
                     'valid_seed_count': len(seed_rows), 'rejected_seed_count': 0,
                     'planner_started_after_seed_validation': False}}
    else:
        state = read(path)
        if state['status'] in ('COMPLETED', 'CANCELLED'):
            raise ValueError('Задача уже завершена.')
        anti_loop_data = state.get('anti_loop_telemetry')
        if isinstance(anti_loop_data, dict) and anti_loop_data.get('circuit_breaker_fired'):
            rescue_approved_retry = (state.get('rescue_status') == 'POST_RESCUE_RETRY' and
                                     state.get('post_rescue_pending') is True)
            if rescue_approved_retry:
                # A crash after the bounded rescue may resume its one already-authorized retry.
                pass
            elif not allow_no_progress_resume:
                print('BLOCKED: M9 остановил повтор без прогресса; после ручного исправления используйте resume --after-manual-fix.', flush=True)
                return 1
            else:
                state.setdefault('anti_loop_history', []).append(anti_loop_data)
                state['anti_loop_generation'] = state.get('anti_loop_generation', 0) + 1
                state['anti_loop_telemetry'] = {'manual_resume_authorized': True,
                    'generation': state['anti_loop_generation'], 'circuit_breaker_fired': False}
                state['rescue_status'] = 'MANUAL_RESUME'
                state['worker_failures'] = 0
                state['failure_evidence'] = []
                state.pop('last_progress', None)
                fresh_worker_session(state, 'Пользователь явно разрешил один новый цикл после ручного исправления.')
        # Resume compares effective local guard policy, so a state frozen by
        # an old template remains compatible with its safely migrated config.
        # Other configuration differences continue to block resume.
        state['config'] = normalize_legacy_defaults(state['config'])
        if state['config'] != config:
            raise ValueError('Конфигурация изменилась. Восстановите параметры начатой задачи.')
        if git(root, 'rev-parse', 'HEAD') != state['base_head']:
            raise ValueError('HEAD изменился после начала задачи; требуется ручная проверка изменений.')
        if config.get('strong_planning', True) and state.get('routing_policy') != 'plan-first-v1':
            state['routing_policy'] = 'plan-first-v1'
            state['worker_failures'] = sum(1 for item in state.get('attempts_detail', [])
                                          if item.get('verification', {}).get('ok') is False)
    phase_telemetry.record(state, 'context_load', context_started, context_started_at)
    def save(status, **fields):
        if 'reason' not in fields:
            state.pop('reason', None)
        state.update(status=status, updated_at=time.time(), **fields)
        state['megaprog_version'] = __version__
        if status == 'BLOCKED':
            failure = state.get('last_failure', 'unknown')
            if failure == 'megaprog_internal' and 'last_failure' not in fields:
                failure = 'unknown'  # A later stop must not replay an old exception.
            state['self_health'] = record_safely(root, state,
                kind='internal' if failure == 'megaprog_internal' else failure,
                frames=fields.get('fault_frames'), exception_type=fields.get('fault_type'))
        checks = (state.get('verification') or {}).get('checks') or []
        last_check = next((item for item in checks if item.get('exit_code') not in (None, 0)), checks[-1]) if checks else {}
        verify_status = ('PASS' if last_check['exit_code'] == 0 else 'FAIL') if 'exit_code' in last_check else 'NOT_RUN'
        state['progress'] = {'project': root.name, 'task_id': state['id'],
            'phase': 'VERIFYING' if status == 'VERIFY' else status,
            'attempt': state['attempts'], 'elapsed_seconds': max(0, int(time.time() - state['created_at'])),
            'doing': fields.get('reason', status),
            'verify': verify_status, 'last_verify_command': last_check.get('command'),
            'next_step': fields.get('next_step') or {
                'PREFLIGHT': 'Проверка проекта и модели', 'RUNNING': 'Дождаться model turn',
                'VERIFY': 'Дождаться проверок', 'COMPLETED': 'Просмотреть отчёт',
                'BLOCKED': ('Открыть запрос плана для обычного ChatGPT: ' + state['chatgpt_plan_offer']['path']
                            if (state.get('chatgpt_plan_offer') or {}).get('status') == 'OFFERED'
                            else ('Открыть готовый запрос внешнему ChatGPT: ' + state['chatgpt_offer']['path']
                                  if (state.get('chatgpt_offer') or {}).get('status') == 'OFFERED'
                                  else 'Устранить причину и выполнить resume')),
                'PENDING_APPROVAL': 'Подтвердить scope ' + fields.get('approval_scope', ''),
                'RETRY': 'Следующая попытка в пределах бюджета'}.get(status, 'Просмотреть status'),
            'report_path': str(root / '.ai-dev/runs' / state['id'] / 'report.json')}
        write(path, state)
        write(root / '.ai-dev/runs' / state['id'] / 'state.json', state)
        with (root / '.ai-dev/runs' / state['id'] / 'progress.jsonl').open('a', encoding='utf-8') as events:
            events.write(json.dumps(dict(state['progress'], timestamp=state['updated_at']), ensure_ascii=False) + '\n')
        try:
            project_memory.record_task_state(root, state, status, fields)
            state.pop('project_memory_error', None)
            state['project_memory_status'] = 'OK'
            write(path, state)
            write(root / '.ai-dev/runs' / state['id'] / 'state.json', state)
        except (OSError, ValueError) as memory_error:
            # Project Memory is a derived index. Its failure must not rewrite or
            # strand a valid canonical task; make degradation visible instead.
            state['project_memory_status'] = 'DEGRADED'
            state['project_memory_error'] = str(memory_error)[:400]
            write(path, state)
            write(root / '.ai-dev/runs' / state['id'] / 'state.json', state)
            print('WARNING: Project Memory не обновлена: ' + str(memory_error)[:300], flush=True)
        if status in ('COMPLETED', 'BLOCKED'):
            write_report(root, state)
        print(status + (': ' + fields['reason'] if 'reason' in fields else ''), flush=True)
        print(styled(progress_line(root, state, status, fields)), flush=True)
    save('PREFLIGHT')
    try:
        report = codex.doctor(root)
        write(root / '.ai-dev/doctor.json', report)
        if not report['ready']:
            save('BLOCKED', reason='Codex или вход через ChatGPT недоступен; смотрите doctor.',
                 last_failure='infrastructure')
            return 1
        if state['session_id'] and not report['resume']:
            save('BLOCKED', reason='Продолжение сессии недоступно.')
            return 1
        if not config['verify']:
            save('BLOCKED', reason='Сначала задайте команды verify в .ai-dev/config.json.')
            return 1
        catalog = discover(root, report['executable'])
        write(root / '.ai-dev/catalog.json', catalog)
        route = state.get('routing', {'kind': 'auto', 'model': None, 'reasoning': None})
        requested_model = route['model'] or (config['model'] if config['model'] != 'auto' else None)
        requested_reasoning = route.get('reasoning') or (config['reasoning'] if requested_model else None)
        route_scope = route.get('pin_scope', 'task')  # Old saved tasks keep their former task-wide pin.
        planner_model = requested_model if route_scope in ('planner', 'task') else None
        planner_reasoning = requested_reasoning if planner_model else None
        worker_model = requested_model if route_scope in ('worker', 'task') else None
        worker_reasoning = requested_reasoning if worker_model else None
        try:
            routing.validate_request(route['kind'], requested_model, requested_reasoning)
        except ValueError as exc:
            save('BLOCKED', reason=str(exc))
            return 1
        if (catalog.get('errors') or {}).get('models'):
            save('BLOCKED', reason='Не удалось получить каталог Codex-моделей: %s. '
                 'Это сбой discovery; модели и квоты не были проверены.' % catalog['errors']['models'],
                 last_failure='discovery')
            return 1
        modern = state.get('routing_policy') == 'plan-first-v1'
        base_limit = 5 if modern and config['max_attempts'] == 2 else config['max_attempts']
        # Never spend on a new planner when a resumed task has no turns left.
        if state['attempts'] >= state.get('attempt_limit', base_limit):
            if state.get('worker_failures', 0) >= 2:
                offer_external_advisory(root, state)
            save('BLOCKED', reason='Бюджет попыток уже исчерпан; планировщик повторно не запускается.')
            return 1
        # Validate a requested worker's model, effort and quota before spending
        # on its plan. Resume/recovery keeps its own current model selection.
        if not state.get('plan') and not state['attempts']:
            if planner_model:
                routing.choose(catalog, state['prompt'], kind=route['kind'],
                               model=planner_model, reasoning=planner_reasoning)
            # Check the worker route before spending a planner turn, even when
            # the user's explicit pin is scoped only to the planner.
            routing.choose(catalog, state['prompt'], kind=route['kind'],
                           model=worker_model, reasoning=worker_reasoning)
        if modern:
            if state.get('orchestration_task_id') and orchestration.approval_status(
                    root, 'model_turn', task_id=state['orchestration_task_id']).get('status') != 'APPROVED':
                save('PENDING_APPROVAL', reason='Подтвердите model_turn для плана и исполнения.', approval_scope='model_turn')
                return 1
            with phase_telemetry.measure(state, 'planning', 'orchestration',
                                         overlap_group='planning'):
                prepare_plan(root, state, config, catalog, report, save)
        # Old config templates shipped with two total attempts: reserve recovery
        # for new plan-first tasks, without rewriting snapshots of existing tasks.
        steps = planning.checkpoints(state['plan']) if modern else []
        if modern and state.get('planning_pipeline_version') == 1 and state.get('plan_contract'):
            evidence_state = state.get('planning_evidence') or {}
            packet_path = evidence_state.get('packet_path')
            if not isinstance(packet_path, str) or not packet_path.startswith('.ai-dev/evidence/'):
                save('BLOCKED', reason='PlanContract is missing its Evidence Packet reference.',
                     last_failure='planning_stale')
                return 1
            try:
                packet = evidence_packet.load_packet(root / packet_path)
                freshness = planning_pipeline.contract_staleness(root, state['plan_contract'], packet,
                    allow_planned_changes=bool(state.get('attempts')))
            except (OSError, ValueError, TypeError) as exc:
                freshness = {'status': 'UNKNOWN', 'reasons': [str(exc)[:200]]}
            if freshness.get('status') != 'VALID':
                save('BLOCKED', reason='PlanContract устарел до начала реализации (%s); пересоберите Evidence Packet и перепланируйте.' %
                     ', '.join(freshness.get('reasons', [])[:5]),
                     last_failure='planning_stale', planning_staleness=freshness)
                return 1
        attempt_limit = base_limit + max(0, len(steps) - 1)
        state['attempt_limit'] = attempt_limit
        state.setdefault('checkpoint_index', 0)
        state.setdefault('checkpoint_attempts', state['attempts'])
        while state['attempts'] < attempt_limit:
            checkpoint = steps[state['checkpoint_index']] if modern else None
            if modern and state['checkpoint_attempts'] >= base_limit:
                if state.get('worker_failures', 0) >= 2:
                    offer_external_advisory(root, state)
                save('BLOCKED', reason='Бюджет текущего этапа исчерпан; сохранены ошибки и результаты.')
                return 1
            # Fetch usage immediately before routing and the model turn.  A
            # missing limit is unknown, not zero; routing decides how to
            # handle that without inventing a quota value.
            fresh_catalog = discover(root, report['executable'])
            catalog = fresh_catalog
            write(root / '.ai-dev/catalog.json', catalog)
            if (catalog.get('errors') or {}).get('models'):
                save('BLOCKED', reason='Не удалось обновить каталог Codex-моделей: %s. '
                     'Это сбой discovery; автоматический выбор не выполнялся.' % catalog['errors']['models'],
                     last_failure='discovery')
                return 1
            state['fresh_limits'] = catalog.get('limits')
            last_log = Path(state['last_run']) if isinstance(state.get('last_run'), str) else None
            if last_log is not None and last_log.is_dir():
                last_log = last_log / 'codex.jsonl'
            if state.get('session_id') and last_log and last_log.exists():
                metrics = inspect_jsonl(last_log)
                compact, reason = should_compact(
                    metrics, config.get('context_compaction_bytes', 1_000_000),
                    config.get('context_compaction_tokens', 180_000))
                if compact:
                    summary = build_summary(state, _relevant_files(root), state.get('result'),
                                            'Continue from the verified state; run the configured checks.')
                    state['compaction'] = dict(metrics, compaction_reason=reason,
                                               summary_bytes=len(summary.encode('utf-8')),
                                               old_session_id=state.get('session_id'),
                                               new_session_id=None, usage_report_evidence=metrics.get('usage_events', []))
                    state['compaction_summary'] = summary
                    state['session_id'] = None
                    save('RETRY', reason='Контекст Codex сжат и будет безопасно продолжен новой сессией.',
                         last_failure='compaction')
            decisions = state.setdefault('decisions', [])
            # Only a completed Codex turn followed by a failed verification
            # authorizes escalation.  In particular, a stale verification
            # result must not turn a network/Codex/infrastructure failure on
            # resume into an expensive model change.
            failed_checks = state.get('last_failure') == 'verification'
            if 'last_failure' not in state:
                # Backward-compatible read of states written before this
                # marker existed.  RETRY is the only legacy state that
                # unambiguously means verification just failed; BLOCKED may
                # contain a stale result from an unrelated failure.
                failed_checks = (state.get('status') == 'RETRY' and
                                 state.get('result', {}).get('ok') is True and
                                 state.get('verification', {}).get('ok') is False)
            worker_pinned = route.get('pin_scope', 'task') in ('worker', 'task')
            worker_model = requested_model if worker_pinned else None
            worker_reasoning = requested_reasoning if worker_model else None
            prior = decisions[-1] if decisions and failed_checks and not worker_model else None
            continuing = decisions[-1] if decisions and not failed_checks and not worker_model else None
            local_decisions = [d for d in decisions if d.get('checkpoint', 'step-1') == checkpoint['id']] if modern else decisions
            last_local = local_decisions[-1] if local_decisions else None
            if modern and state.get('post_rescue_pending'):
                decision = routing.choose(catalog, state['prompt'],
                    kind=checkpoint['worker_kind'] if route['kind'] == 'auto' else route['kind'],
                    model=worker_model, reasoning=worker_reasoning)
                decision['role'] = 'worker'
            elif modern and state.get('worker_failures', 0) >= 2:
                decision = planning.strong(catalog, recovery=True,
                    medium=state.get('worker_failures', 0) >= 4 or bool(last_local and
                        last_local.get('role') == 'recovery' and (
                            last_local.get('reasoning') == 'medium' or
                            state.get('last_progress', {}).get('outcome') in ('unchanged', 'regressed'))))
            elif modern:
                decision = routing.choose(catalog, state['prompt'],
                    kind=checkpoint['worker_kind'] if route['kind'] == 'auto' else route['kind'],
                    model=last_local['model'] if last_local else worker_model,
                    reasoning=last_local['reasoning'] if last_local else worker_reasoning)
                decision['role'] = 'worker'
            else:
                decision = routing.choose(catalog, state['prompt'], kind=route['kind'],
                                      model=continuing['model'] if continuing else worker_model,
                                      reasoning=continuing['reasoning'] if continuing else worker_reasoning, previous=prior)
            if continuing and not modern:
                decision['reason'] = 'Продолжение ранее выбранной модели после остановки.'
            if state.get('orchestration_task_id'):
                gate = orchestration.approval_status(root, 'model_turn', task_id=state['orchestration_task_id'])
                if gate.get('status') != 'APPROVED':
                    save('PENDING_APPROVAL', reason='PENDING_APPROVAL: ручное подтверждение model turn требуется.',
                         approval_scope='model_turn')
                    return 1
                if not modern and state['attempts'] > 0 and decision['model'] in routing.ORDER[2:]:
                    gate = orchestration.approval_status(root, 'expensive_escalation', task_id=state['orchestration_task_id'])
                    if gate.get('status') != 'APPROVED':
                        save('PENDING_APPROVAL', reason='PENDING_APPROVAL: ручное подтверждение дорогой эскалации требуется.',
                             approval_scope='expensive_escalation')
                        return 1
            if modern:
                decision['checkpoint'] = checkpoint['id']
                if last_local and (decision['model'], decision['reasoning'], decision.get('role')) != (last_local['model'], last_local['reasoning'], last_local.get('role')):
                    fresh_worker_session(state, 'Смена модели/режима: короткая сводка вместо истории неудач.')
            decisions.append(decision)
            state['active_decision'] = decision
            effective = dict(config, model=decision['model'], reasoning=decision['reasoning'], detect_stall=modern)
            print(styled('MODEL DECISION: %s / %s — %s' % (decision['model'], decision['reasoning'], decision['reason'])), flush=True)
            directory = root / '.ai-dev/runs' / state['id'] / str(state['attempts'] + 1)
            directory.mkdir(parents=True, exist_ok=True)
            anti_loop_enabled = bool(modern and state.get('planning_pipeline_version') == 1
                                     and state.get('plan_contract'))
            state['anti_loop_enabled'] = anti_loop_enabled
            before_attempt = _anti_loop_snapshot(root, checkpoint['id']) if anti_loop_enabled else None
            lesson_context = prompt_context(root)
            advisory_context = codex_advisory_context(root, state['id'])
            prior_review = next((item for item in reversed(state.get('review_cascade', []))
                                 if isinstance(item, dict) and item.get('status') == 'FIX_REQUIRED'), None)
            if prior_review:
                feedback = prior_review.get('semantic_review') or prior_review.get('astra_review') or {}
                advisory_context = (str(advisory_context or '') +
                    '\n\nPlanContract review feedback (evidence-bound; preserve the contract):\n' +
                    json.dumps(feedback, ensure_ascii=False)[:3000])
            if anti_loop_enabled and isinstance(state.get('rescue_instruction'), dict):
                advice = state['rescue_instruction']
                advisory_context = (str(advisory_context or '') +
                    '\n\nFresh rescue advice (one controlled attempt; validate against PlanContract):\n' +
                    json.dumps(advice, ensure_ascii=False)[:1800])
            context_phase = 'correction' if state.get('checkpoint_attempts', 0) > 0 else 'worker_context_build'
            with phase_telemetry.measure(state, context_phase, 'local_tool'):
                instructions, prompt_metrics = assemble(
                    state['prompt'], config['verify'],
                    SELF_IMPROVEMENT_PROMPT + '\n\n' + project_scope_manifest(root) +
                    ('\n\n' + planning.work_packet(state) if modern else ''),
                    lessons=lesson_context,
                    previous_verification=state.get('verification'),
                    advisory=advisory_context,
                    compaction_summary=(state.get('compaction_summary')
                                        if summary_is_fresh(state.get('compaction_summary'), state) else ''),
                    budget_bytes=config.get('context_budget_bytes', DEFAULT_BUDGET_BYTES))
            state['prompt_metrics'] = prompt_metrics
            ensure_turn_budget(state, config)
            try:
                # Routing, rather than the guard, selects this pair.  The
                # guard only applies a known local concurrency tier to it.
                with waiting_turn(config, decision['model'], decision['reasoning'],
                                  lambda evidence: save('WAITING_FOR_SLOT', guard=evidence,
                                      reason='Занят локальный слот MegaProg; ожидаю автоматически.',
                                      next_step='Продолжение после освобождения слота; Ctrl+C сохраняет задачу.'),
                                  owner={'project': root.name, 'task_id': state['id']}, state=state) as guard:
                    state['guard'] = guard.evidence
                    # A routing decision can be made before the local guard
                    # rejects it.  Mark it as an actual model turn only after
                    # the guard has granted a slot, so reports and usage never
                    # invent a token-consuming attempt for a blocked preflight.
                    decision['attempt'] = state['attempts'] + 1
                    state['attempts'] += 1
                    state['checkpoint_attempts'] += 1
                    state['last_failure'] = None
                    save('RUNNING', last_run=str(directory))
                    isolation = None
                    try:
                        with phase_telemetry.measure(state, 'worker_execution', 'model',
                                                     overlap_group='worker'):
                            with isolate_generated_context(root) as active_isolation:
                                isolation = active_isolation
                                result = codex.execute(root, effective, report, instructions,
                                                       active_isolation.relocate(directory / 'codex.jsonl'),
                                                       state['session_id'])
                    finally:
                        if isolation is not None:
                            state['context_isolation'] = dict(isolation.metrics)
            except AccountGuardError as exc:
                state['guard'] = exc.evidence
                save('BLOCKED', reason=str(exc), last_failure='account_guard')
                return 1
            state['session_id'] = result['session_id']
            telemetry_stage = checkpoint.get('id') if modern and checkpoint else 'legacy-stage-1'
            attach_context(result, task_id=state['id'], stage_id=telemetry_stage,
                           role=decision.get('role', 'worker'),
                           retry_number=state.get('checkpoint_attempts', state['attempts']),
                           attempt_number=state['attempts'],
                           escalation_reason=decision.get('reason'),
                           configured_model=decision.get('model'),
                           configured_reasoning=decision.get('reasoning'))
            if isinstance(state.get('compaction'), dict) and state['compaction'].get('new_session_id') is None:
                state['compaction']['new_session_id'] = result.get('session_id')
            decision['runtime_verified'] = bool(result['ok'])
            state.setdefault('attempts_detail', []).append({'attempt': state['attempts'],
                'codex': result})
            if not result['ok']:
                if modern and result.get('stalled'):
                    previous_verification = state.get('verification')
                    state['worker_failures'] = state.get('worker_failures', 0) + 2
                    state['total_worker_failures'] = state.get('total_worker_failures', 0) + 2
                    # The watchdog carries the observed failed checks, not a
                    # stale verification result from a previous attempt.
                    observed = result.get('stall_verification') or {'ok': False, 'checks': []}
                    failure_evidence(root, state, observed, state.get('verification'))
                    state['verification'] = observed
                    if anti_loop_enabled:
                        state['attempts_detail'][-1]['verification'] = observed
                        after_attempt = _anti_loop_snapshot(root, checkpoint['id'])
                        attempt_row, previous_attempt = _record_attempt(
                            state, decision, checkpoint, result, observed, before_attempt,
                            after_attempt, previous_verification,
                            hypothesis=planning.worker_attempt_hypothesis(result.get('message'), checkpoint))
                        if state.pop('post_rescue_pending', False):
                            attempt_row['post_rescue_attempt'] = True
                            state['post_rescue_attempts'] = state.get('post_rescue_attempts', 0) + 1
                            state.pop('rescue_instruction', None)
                        stage_attempts = [item for item in state.get('attempt_history', [])
                                          if item.get('stage_id') == checkpoint.get('id') and
                                          item.get('generation', 0) == state.get('anti_loop_generation', 0)]
                        stop_decision = anti_loop.should_stop(
                            previous_attempt.get('failure') if previous_attempt else None,
                            attempt_row.get('failure'),
                            previous_attempt.get('approach') if previous_attempt else None,
                            attempt_row.get('approach'), attempt_row.get('progress_signals', []),
                            len(stage_attempts), post_rescue=bool(attempt_row.get('post_rescue_attempt')))
                        attempt_row['no_progress_detected'] = stop_decision['stop']
                        attempt_row['circuit_breaker_reason'] = stop_decision['reason']
                        if stop_decision['stop'] and attempt_row.get('failure'):
                            terminal = _no_progress_stop(root, state, checkpoint, attempt_row['failure'],
                                stage_attempts, attempt_row, after_attempt, report, config, catalog, save)
                            if terminal == 'STOP':
                                return 1
                            continue
                    save('RETRY', reason='Два повторных провала настроенной проверки внутри хода; повышаю модель.',
                         last_failure='verification', result=result)
                    continue
                error_text = json.dumps(result.get('error', ''), ensure_ascii=False).lower()
                if any(word.lower() in error_text for word in ('contextwindowexceeded',
                                                               'context window exceeded',
                                                               'context length exceeded',
                                                               'maximum context length')):
                    summary = build_summary(state, _relevant_files(root), result,
                                            'Retry with a fresh Codex session and preserve verification.')
                    metrics = inspect_jsonl(directory / 'codex.jsonl')
                    state['compaction'] = dict(metrics, compaction_reason='ContextWindowExceeded',
                                               summary_bytes=len(summary.encode('utf-8')),
                                               old_session_id=result.get('session_id'),
                                               new_session_id=None, usage_report_evidence=metrics.get('usage_events', []))
                    state['compaction_summary'] = summary
                    state['session_id'] = None
                    save('RETRY', reason='Codex сообщил ContextWindowExceeded; запускается новая сессия.',
                         last_failure='compaction', result=result)
                    continue
            if not result['ok']:
                save('BLOCKED', reason='Codex не завершил ход успешно. Смотрите журнал; автоматического повтора нет.', error=result.get('error'))
                return 1
            if worker_access_failure(result.get('message')):
                save('BLOCKED', reason='NO_CHANGE: Codex сообщил, что доступ к проекту запрещён и задача не выполнена. Старые тесты не подтверждают новую работу.',
                     last_failure='worker_access', result=result,
                     next_step='Исправить доступ Codex к проекту и затем вручную выполнить resume.')
                return 1
            # A structured prerequisite/runtime block is terminal for this
            # attempt. Do not run configured project tests before the stage's
            # verification gate has been reached.
            try:
                worker_result = planning.worker_result(result.get('message'), checkpoint) if modern else None
            except (ValueError, TypeError):
                worker_result = None
            if modern and isinstance(worker_result, dict) and worker_result.get('status') == 'blocked':
                state['stage_worker_result'] = worker_result
                state['checkpoint_issue'] = worker_result.get('summary', 'Prerequisite/runtime unavailable.')
                save('BLOCKED', reason='Этап остановлен worker до verification gate: ' +
                     state['checkpoint_issue'][:400], last_failure='prerequisite',
                     next_step='Проверить только отсутствующий prerequisite или runtime; полный suite не запускался.')
                return 1
            save('VERIFY', result=result, last_failure=None)
            previous_verification = state.get('verification')
            checked = verify(root, config, directory, state=state)
            try:
                changed = [line[3:] for line in git(root, 'status', '--short').splitlines()
                           if len(line) > 3]
                state['test_recommendation'] = test_recommendation.recommend(
                    root, changed, explicit_checks=config.get('verify', []))
            except (OSError, ValueError):
                state['test_recommendation'] = {'schema_version': 1,
                    'method': 'explicit_mapping+test_naming', 'automatic_execution': False,
                    'status': 'UNKNOWN'}
            state['verification'] = checked
            state['attempts_detail'][-1]['verification'] = checked
            receipt = ''
            state.pop('checkpoint_issue', None)
            if modern and checked['ok'] and state['plan'].get('schema') == 2:
                try:
                    parsed_result = worker_result or planning.worker_result(result.get('message'), checkpoint)
                    receipt = parsed_result['summary']
                    state['stage_worker_result'] = parsed_result
                    state['stage_deviation'] = planning.worker_deviation(result.get('message'), checkpoint)
                except ValueError as exc:
                    state['checkpoint_issue'] = str(exc)[:400]
            review_result = None
            if (modern and not state.get('checkpoint_issue') and
                    state.get('planning_pipeline_version') == 1 and state.get('plan_contract')):
                with phase_telemetry.measure(state, 'review', 'orchestration',
                                             overlap_group='review'):
                    review_result = run_review_cascade(root, state, config, report, directory,
                                                       checkpoint, checked)
                state['attempts_detail'][-1]['review_cascade'] = review_result
                state['review_telemetry'] = [item.get('telemetry') for item in
                    state.get('review_cascade', []) if isinstance(item, dict)]
                if review_result.get('status') == 'FIX_REQUIRED':
                    review_issue = review_result.get('semantic_review') or review_result.get('astra_review') or {}
                    state['checkpoint_issue'] = ('Review Cascade found contract violations: ' +
                        json.dumps(review_issue, ensure_ascii=False)[:700])
                elif review_result.get('status') not in ('PASS', 'PASS_NO_MODEL_REVIEW'):
                    save('BLOCKED', reason='Review Cascade не смог доказать соответствие PlanContract (%s). Изменения сохранены для просмотра.' %
                         review_result.get('status', 'UNKNOWN'), last_failure='review',
                         review_cascade=review_result,
                         next_step='Проверить Review Packet и результат; автоматическое завершение запрещено.')
                    return 1
            if anti_loop_enabled:
                after_attempt = _anti_loop_snapshot(root, checkpoint['id'])
                attempt_row, previous_attempt = _record_attempt(
                    state, decision, checkpoint, result, checked, before_attempt,
                    after_attempt, previous_verification, receipt=receipt,
                    hypothesis=planning.worker_attempt_hypothesis(result.get('message'), checkpoint),
                    new_evidence=state.get('new_evidence_since_attempt'),
                    review_issue=state.get('checkpoint_issue'))
                if state.get('checkpoint_issue') and checked.get('ok'):
                    attempt_row['result'] = 'FIX_REQUIRED'
                if isinstance(review_result, dict):
                    attempt_row['review_result'] = {
                        'deterministic_triggers': review_result.get('deterministic_triggers', []),
                        'astra_trigger': review_result.get('astra_trigger')}
                state.pop('new_evidence_since_attempt', None)
                if state.pop('post_rescue_pending', False):
                    attempt_row['post_rescue_attempt'] = True
                    state['post_rescue_attempts'] = state.get('post_rescue_attempts', 0) + 1
                    state.pop('rescue_instruction', None)
            if checked['ok'] and not state.get('checkpoint_issue'):
                if (anti_loop_enabled and state.get('rescue_status') == 'POST_RESCUE_RETRY' and
                        isinstance(state.get('anti_loop_telemetry'), dict) and
                        state['anti_loop_telemetry'].get('circuit_breaker_fired')):
                    state.setdefault('anti_loop_history', []).append(state['anti_loop_telemetry'])
                    state['anti_loop_telemetry'] = {'circuit_breaker_fired': False,
                        'resolved_after_rescue': True,
                        'generation': state.get('anti_loop_generation', 0)}
                    state['rescue_status'] = 'RESOLVED'
                if modern:
                    supersede_external_advisory(state)
                    state.setdefault('checkpoint_history', []).append({
                        'id': checkpoint['id'], 'title': checkpoint['title'],
                        'summary': receipt or 'Настроенные проверки пройдены.',
                        'attempt': state['attempts'], 'model': decision['model'],
                        'reasoning': decision['reasoning'],
                        'session_id': state.get('session_id')})
                    if state['checkpoint_index'] + 1 < len(steps):
                        state['checkpoint_index'] += 1
                        state['checkpoint_attempts'] = 0
                        state['worker_failures'] = 0
                        state['failure_evidence'] = []
                        state.pop('last_progress', None)
                        state.pop('stage_deviation', None)
                        state.pop('verification', None)
                        fresh_worker_session(state, 'Этап проверен; новый этап начинает подходящий недорогой исполнитель.')
                        save('RETRY', reason='Этап проверен. Перехожу к следующему.', last_failure=None)
                        continue
                completion_reason = 'Проверки пройдены; изменения оставлены для просмотра.'
                if anti_loop_enabled:
                    _record_failure_resolution(root, state)
                    if state.get('failure_memory_resolved'):
                        completion_reason = 'Проверки пройдены; прежний сбой отмечен устранённым.'
                save('COMPLETED', snapshot=snapshot(root), diff=git(root, 'diff', '--stat'),
                     git_status=git(root, 'status', '--short'), reason=completion_reason)
                return 0
            if modern and planning.infrastructure_check(checked):
                save('BLOCKED', reason='Проверку блокирует среда/доступ/сеть; повышение модели это не исправит.',
                     last_failure='infrastructure')
                return 1
            if modern:
                failure_evidence(root, state, checked, previous_verification)
                state['worker_failures'] = state.get('worker_failures', 0) + 1
                state['total_worker_failures'] = state.get('total_worker_failures', 0) + 1
                if anti_loop_enabled and attempt_row.get('failure'):
                    stage_attempts = [item for item in state.get('attempt_history', [])
                                      if item.get('stage_id') == checkpoint.get('id') and
                                      item.get('generation', 0) == state.get('anti_loop_generation', 0)]
                    previous_failure = previous_attempt.get('failure') if previous_attempt else None
                    current_failure = attempt_row.get('failure')
                    stop_decision = anti_loop.should_stop(
                        previous_failure, current_failure,
                        previous_attempt.get('approach') if previous_attempt else None,
                        attempt_row.get('approach'), attempt_row.get('progress_signals', []),
                        len(stage_attempts), post_rescue=bool(attempt_row.get('post_rescue_attempt')))
                    attempt_row['no_progress_detected'] = stop_decision['stop']
                    attempt_row['circuit_breaker_reason'] = stop_decision['reason']
                    if stop_decision['stop']:
                        terminal = _no_progress_stop(root, state, checkpoint, current_failure,
                            stage_attempts, attempt_row, after_attempt, report, config, catalog, save)
                        if terminal == 'STOP':
                            return 1
                        continue
                if state['worker_failures'] >= 4:
                    offer_external_advisory(root, state)
                save('RETRY', reason='%s (%s); %s.' % (
                     'Результат этапа не подтверждён' if state.get('checkpoint_issue') else 'Проверки не прошли', state['worker_failures'],
                     'следующий ход — сильная модель' if state['worker_failures'] >= 2 else 'осталась одна попытка исполнителя'),
                     last_failure='verification')
            else:
                save('RETRY', reason='Проверки не прошли.', last_failure='verification')
        if state.get('worker_failures', 0) >= 2:
            offer_external_advisory(root, state)
        save('BLOCKED', reason='Лимит попыток исчерпан; журналы и изменения сохранены.')
        return 1
    except AccountGuardError as exc:
        save('BLOCKED', reason=str(exc), guard=exc.evidence, last_failure='account_guard')
        return 1
    except (KeyboardInterrupt, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        last_log = root / '.ai-dev/runs' / state['id'] / str(state['attempts']) / 'codex.jsonl'
        if last_log.exists():
            for line in last_log.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    event = json.loads(line)
                    if event.get('type') == 'thread.started':
                        state['session_id'] = event['thread_id']
                except (ValueError, KeyError):
                    pass
        if isinstance(exc, evidence_packet.EvidenceSeedError):
            state.setdefault('planning_telemetry', {}).update({
                'planner_started_after_seed_validation': False,
                'rejected_seed_count': len(exc.details.get('rejected', [])),
                'evidence_seed_failure_code': exc.code,
                'evidence_seed_failure': exc.as_dict()})
            planning_failure = exc.code
        elif isinstance(exc, PlanningGateBlocked):
            planning_failure = 'planning_gate'
        elif isinstance(exc, PlanningFormatExhausted):
            planning_failure = ('planning_semantic' if state.get('planner_error_class') in
                                ('SEMANTIC_PLAN_ERROR', 'PLAN_CONTRACT_SEMANTIC_ERROR')
                                else 'planning_format')
        else:
            planning_failure = 'infrastructure'
        save('BLOCKED', reason='Остановлено: ' + (str(exc) or 'прервано пользователем'),
             last_failure=planning_failure)
        return 1
    except (AttributeError, KeyError, IndexError, TypeError, RuntimeError) as exc:
        save('BLOCKED', reason='Внутренняя ошибка MegaProg: ' + type(exc).__name__,
             last_failure='megaprog_internal', fault_type=type(exc).__name__,
             fault_frames=exception_frames(exc))
        return 1


def task(root, prompt=None, kind="auto", model=None, reasoning=None, orchestration_task_id=None,
         pin_scope=None, allow_no_progress_resume=False, initial_evidence_seeds=None):
    """Measure end-to-end task wall time without adding a model turn."""
    started_perf = time.perf_counter()
    started_at = phase_telemetry.timestamp()
    root = Path(root)
    result = 1
    try:
        result = _task_impl(root, prompt, kind, model, reasoning,
                            orchestration_task_id, pin_scope, allow_no_progress_resume,
                            initial_evidence_seeds)
        return result
    finally:
        state_path = root / '.ai-dev/state.json'
        try:
            if state_path.is_file():
                state = read(state_path)
                state['total_wall_time_ms'] = max(0, round((time.perf_counter() - started_perf) * 1000))
                phase_telemetry.record(state, 'total', started_perf, started_at, category='total',
                                       status='COMPLETED' if result == 0 else 'STOPPED')
                write(state_path, state)
                run_state = root / '.ai-dev/runs' / str(state.get('id')) / 'state.json'
                write(run_state, state)
                if state.get('status') in ('COMPLETED', 'BLOCKED'):
                    write_report(root, state)
        except (OSError, ValueError, TypeError):
            # Timing is diagnostic only and must never change task outcome.
            pass


def commit(root, message):
    """Measure the separately invoked, reverified commit/finalization step."""
    started_perf = time.perf_counter()
    started_at = phase_telemetry.timestamp()
    root = Path(root)
    result = 1
    try:
        result = _commit_impl(root, message)
        return result
    finally:
        state_path = root / '.ai-dev/state.json'
        try:
            if state_path.is_file():
                state = read(state_path)
                phase_telemetry.record(state, 'commit_finalization', started_perf,
                    started_at, category='local_tool',
                    status='COMPLETED' if result == 0 else 'STOPPED')
                write(state_path, state)
                write(root / '.ai-dev/runs' / str(state.get('id')) / 'state.json', state)
                write_report(root, state)
        except (OSError, ValueError, TypeError):
            pass
