"""Task reports assembled from the durable supervisor state."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .storage import write
from .chatgpt_policy import decide as chatgpt_advisory_decision


def _timestamp(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _git_context(root, state):
    """Return best-effort changed-file information without affecting the task."""
    result = {
        'diff_stat': state.get('diff'),
        'status': state.get('git_status'),
        'files': [],
    }
    try:
        import subprocess
        status = subprocess.run(['git', 'status', '--short'], cwd=str(root),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, check=False).stdout
        result['status'] = status.strip() or result['status']
        result['files'] = [line[3:] for line in status.splitlines() if len(line) > 3]
        if result['diff_stat'] is None:
            result['diff_stat'] = subprocess.run(
                ['git', 'diff', '--stat'], cwd=str(root), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if not result['files'] and result['status']:
        result['files'] = [line[3:] for line in str(result['status']).splitlines()
                           if len(line) > 3]
    return result


def build_report(root, state, generated_at=None):
    """Build a stable, JSON-serializable report from a supervisor state."""
    generated_at = generated_at if generated_at is not None else time.time()
    attempts = []
    details = state.get('attempts_detail', [])
    if not isinstance(details, list):
        details = []
    decisions = state.get('decisions', [])
    if not isinstance(decisions, list):
        decisions = []
    # Since 2.0.5 a decision gains ``attempt`` only once account guard has
    # granted a slot.  Retain the old positional mapping for historical state
    # files, which did not have that marker.
    marked_decisions = {item.get('attempt'): item for item in decisions
                        if isinstance(item, dict) and type(item.get('attempt')) is int and
                        item['attempt'] > 0}
    actual_attempts = state.get('attempts')
    if type(actual_attempts) is not int or actual_attempts < 0:
        actual_attempts = len(details)
    actual_attempts = max(actual_attempts, len(details))
    kind = (state.get('routing') or {}).get('kind', 'auto')
    for index in range(actual_attempts):
        decision = marked_decisions.get(index + 1)
        if decision is None and index < len(decisions):
            # A positional decision is safe only for legacy state without
            # markers.  New unmarked decisions are blocked before a turn.
            candidate = decisions[index]
            if not marked_decisions and isinstance(candidate, dict):
                decision = candidate
        if not isinstance(decision, dict):
            decision = {}
        item = {
            'attempt': index + 1,
            'model': decision.get('model'),
            'reasoning': decision.get('reasoning'),
            'kind': kind,
            'reason': decision.get('reason'),
            'role': decision.get('role', 'worker'),
            'checkpoint': decision.get('checkpoint'),
            'runtime_verified': decision.get('runtime_verified'),
        }
        if index < len(details) and isinstance(details[index], dict):
            item.update({key: details[index][key] for key in ('codex', 'verification', 'review_cascade')
                         if key in details[index]})
        attempts.append(item)
    for index, entry in enumerate(state.get('planning_attempts', [])):
        decision = entry.get('decision', {})
        attempts.append({'attempt': 'plan-%d' % (index + 1), 'role': 'planner',
                         'model': decision.get('model'), 'reasoning': decision.get('reasoning'),
                         'reason': decision.get('reason'), 'kind': kind,
                         'codex': entry.get('codex', {}),
                         'planning_evidence': entry.get('planning_evidence'),
                         'planning_telemetry': entry.get('planning_telemetry'),
                         'planner_validation': entry.get('planner_validation')})
    turn_telemetry = []
    for item in attempts:
        codex_result = item.get('codex') if isinstance(item, dict) else None
        telemetry = codex_result.get('telemetry') if isinstance(codex_result, dict) else None
        if isinstance(telemetry, dict):
            turn_telemetry.append(dict(telemetry))
    review_turns = state.get('review_turns', [])
    for item in review_turns:
        if isinstance(item, dict) and isinstance(item.get('telemetry'), dict):
            turn_telemetry.append(dict(item['telemetry']))
    for item in state.get('rescue_turns', []):
        if isinstance(item, dict) and isinstance(item.get('telemetry'), dict):
            turn_telemetry.append(dict(item['telemetry']))
    changed = _git_context(root, state)
    advisory_task = {
        'status': state.get('status'), 'attempt_count': actual_attempts,
        'prompt': state.get('prompt'),
        'advisory_reason': state.get('advisory_reason'),
        'disagreement': state.get('disagreement'),
        'assumption_review': state.get('assumption_review'),
    }
    chatgpt_advisory = state.get('chatgpt_advisory')
    if not isinstance(chatgpt_advisory, dict):
        chatgpt_advisory = chatgpt_advisory_decision(advisory_task)
    return {
        'id': state.get('id'),
        'megaprog_version': state.get('megaprog_version'),
        'self_health': state.get('self_health'),
        'task_id': state.get('id'),
        'project_memory_status': state.get('project_memory_status', 'NOT_INITIALIZED'),
        'project_memory_error': state.get('project_memory_error'),
        'progress': state.get('progress'),
        'plan': state.get('plan'),
        'plan_contract': state.get('plan_contract'),
        'planning_evidence': state.get('planning_evidence'),
        'planning_telemetry': state.get('planning_telemetry'),
        'evidence_reentry': state.get('evidence_reentry'),
        'planner_validation_history': state.get('planner_validation_history', []),
        'plan_source': state.get('plan_source'),
        'pin_scope': (state.get('routing') or {}).get('pin_scope', 'task'),
        'routing_policy': state.get('routing_policy'),
        'worker_access': 'FULL_ACCESS' if state.get('config', {}).get('windows_worker_full_access') else 'WORKSPACE',
        'worker_failures': state.get('worker_failures', 0),
        'total_worker_failures': state.get('total_worker_failures', state.get('worker_failures', 0)),
        'checkpoints_completed': state.get('checkpoint_history', []),
        'session_handoffs': state.get('session_handoffs', []),
        'last_progress': state.get('last_progress'),
        'attempt_history': state.get('attempt_history', []),
        'anti_loop_history': state.get('anti_loop_history', []),
        'anti_loop_telemetry': state.get('anti_loop_telemetry'),
        'rescue_status': state.get('rescue_status'),
        'rescue_result': state.get('rescue_result'),
        'rescue_packet': state.get('rescue_packet'),
        'rescue_turns': state.get('rescue_turns', []),
        'post_rescue_attempts': state.get('post_rescue_attempts', 0),
        'failure_memory_match': state.get('failure_memory_match'),
        'phase_telemetry': state.get('phase_telemetry', []),
        'wall_time': state.get('wall_time', {}),
        'test_recommendation': state.get('test_recommendation'),
        'prompt': state.get('prompt'),
        'status': state.get('status'),
        'models': [{'model': item['model'], 'reasoning': item['reasoning']}
                   for item in attempts],
        'kind': kind,
        'selected_models': [item['model'] for item in attempts],
        'attempts': attempts,
        'turn_telemetry': turn_telemetry,
        'review_turns': review_turns,
        'review_cascade': state.get('review_cascade', []),
        'review_telemetry': state.get('review_telemetry', []),
        'routing_decisions': [{'model': item.get('model'), 'reasoning': item.get('reasoning'),
                               'reason': item.get('reason'), 'attempt': item.get('attempt')}
                              for item in decisions if isinstance(item, dict)],
        'codex': state.get('result'),
        'verification': state.get('verification'),
        'usage_guard': state.get('guard'),
        'fresh_limits': state.get('fresh_limits'),
        'prompt_budget': state.get('prompt_metrics'),
        'context_isolation': state.get('context_isolation'),
        'context_compaction': state.get('compaction'),
        'chatgpt_advisory': chatgpt_advisory,
        'chatgpt_offer': state.get('chatgpt_offer'),
        'chatgpt_plan_offer': state.get('chatgpt_plan_offer'),
        'changed': changed,
        'changed_files': changed['files'],
        'git_diff_stat': changed['diff_stat'],
        'timestamps': {
            'created_at': _timestamp(state.get('created_at')),
            'updated_at': _timestamp(state.get('updated_at')),
            'generated_at': _timestamp(generated_at),
        },
        'reason': state.get('reason'),
        'next_step': _next_step(state),
    }


def _next_step(state):
    anti_loop_data = state.get('anti_loop_telemetry')
    if isinstance(anti_loop_data, dict) and anti_loop_data.get('circuit_breaker_fired'):
        packet = state.get('rescue_packet', {}).get('path')
        return (('Остановлено на повторе без прогресса; пакет: ' + str(packet) +
                 '; после ручного исправления: ai-dev resume --after-manual-fix')
                if packet else 'Остановлено на повторе без прогресса; после ручного исправления используйте ai-dev resume --after-manual-fix.')
    if state.get('status') == 'COMPLETED':
        return 'Просмотрите изменения и при необходимости выполните ai-dev commit -m "...".'
    if (state.get('chatgpt_plan_offer') or {}).get('status') == 'OFFERED':
        return 'Откройте запрос плана для обычного ChatGPT: ' + state['chatgpt_plan_offer']['path']
    if state.get('last_failure') == 'account_guard':
        return 'Account guard не выдал локальный слот; дождитесь освобождения и используйте resume. Модель не запускалась.'
    reason = state.get('reason') or ''
    if 'verify' in reason.lower() or 'провер' in reason.lower():
        return 'Исправьте код или команды проверки и используйте resume после просмотра журналов.'
    if 'лимит' in reason.lower():
        return 'Разберите журналы и изменения; после ручной проверки начните новую задачу.'
    return 'Разберите причину блокировки и журналы, затем устраните её перед resume.'


def render_markdown(report):
    """Render a human-readable report without depending on markdown packages."""
    lines = [
        '# Отчёт о задаче',
        '',
        '| Поле | Значение |',
        '| --- | --- |',
        '| Идентификатор | `%s` |' % report.get('id'),
        '| Статус | `%s` |' % report.get('status'),
        '| Доступ Windows worker | `%s` |' % report.get('worker_access', 'WORKSPACE'),
        '| Следующий шаг | %s |' % report.get('next_step'),
        '',
        '## Wall-clock phases',
        '',
        '| Фаза | мс | начало | конец | статус |',
        '| --- | ---: | --- | --- | --- |',
    ]
    for phase in report.get('phase_telemetry', []):
        if isinstance(phase, dict):
            lines.append('| %s | %s | %s | %s | %s |' % (
                phase.get('phase', 'UNKNOWN'), phase.get('duration_ms', 'UNKNOWN'),
                phase.get('started_at', 'UNKNOWN'), phase.get('finished_at', 'UNKNOWN'),
                phase.get('status', 'UNKNOWN')))
    lines += [
        '',
        'Category durations can overlap nested phases and must not be summed as task total.',
        '',
        '## Prompt',
        '',
        report.get('prompt') or '(не указан)',
        '',
        '## Попытки и модели',
        '',
    ]
    if report.get('attempts'):
        lines.extend(['| № | Роль | Модель | Reasoning | Codex | Verification |',
                      '| --- | --- | --- | --- | --- | --- |'])
        for item in report['attempts']:
            codex = item.get('codex', {}) or {}
            verification = item.get('verification', {}) or {}
            lines.append('| %s | %s | `%s` | `%s` | `%s` | `%s` |' % (
                item.get('attempt'), item.get('role', 'worker'), item.get('model'), item.get('reasoning'),
                'ok' if codex.get('ok') else 'failed/unknown',
                'read-only plan' if item.get('role') == 'planner' else
                'ok' if verification.get('ok') else 'failed/unknown'))
    else:
        lines.append('Попытки запуска модели отсутствуют.')
    lines.extend(['', '## Результаты Codex', '',
                  '```json', json.dumps(report.get('codex'), ensure_ascii=False, indent=2),
                  '```', '', '## Verification', '',
                  '```json', json.dumps(report.get('verification'), ensure_ascii=False, indent=2),
                  '```', '', '## Изменения', ''])
    changed = report.get('changed', {})
    lines.append('Diff stat: `%s`' % (changed.get('diff_stat') or '(нет данных)'))
    lines.append('Изменённые файлы: %s' % (', '.join(changed.get('files', [])) or '(нет данных)'))
    lines.extend(['', '## Timestamps', '', '```json',
                  json.dumps(report.get('timestamps'), ensure_ascii=False, indent=2),
                  '```', '', '## Usage / account guard', '', '```json',
                  json.dumps({'usage_guard': report.get('usage_guard'),
                              'fresh_limits': report.get('fresh_limits'),
                              'prompt_budget': report.get('prompt_budget'),
                              'context_isolation': report.get('context_isolation')},
                             ensure_ascii=False, indent=2),
                  '```', '', 'Причина: %s' % (report.get('reason') or '(не указана)'), ''])
    return '\n'.join(lines)


def write_report(root, state):
    """Persist the JSON and text report in the task's run directory."""
    directory = Path(root) / '.ai-dev' / 'runs' / state['id']
    report = build_report(root, state)
    write(directory / 'report.json', report)
    (directory / 'report.md').write_text(render_markdown(report), encoding='utf-8')
    return report
