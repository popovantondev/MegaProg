"""Local multi-project status dashboard; never starts a model turn."""
import json
import time
from pathlib import Path
from datetime import datetime
from .account_guard import _pid_alive
from .version import __version__


def duration(seconds):
    if not isinstance(seconds, (int, float)):
        return 'нет данных'
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return '%d д %d ч' % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return '%d ч %d мин' % (seconds // 3600, seconds % 3600 // 60)
    return '%d мин %02d с' % (seconds // 60, seconds % 60)


def owner_duration(seconds):
    """Keep one slot-owner line compact while retaining longer hold times."""
    if not isinstance(seconds, (int, float)):
        return 'unknown'
    seconds = max(0, int(seconds))
    return '%dс' % seconds if seconds < 60 else duration(seconds)


def execution_signal(root, state):
    if state.get('status') not in ('RUNNING', 'VERIFY'):
        return {}
    try:
        signal = json.loads((root / '.ai-dev/live.json').read_text(encoding='utf-8'))
        if not isinstance(signal, dict) or not state.get('id') or signal.get('task_id') != state['id']:
            return {}
        if signal.get('phase', 'RUNNING') != state.get('status'):
            return {}
        now = time.time()
        signal['heartbeat_age'] = max(0, now - float(signal['heartbeat_at']))
        signal['event_age'] = max(0, now - float(signal['last_event_at'])) if signal.get('last_event_at') else None
        signal['alive'] = signal.get('process_state') == 'running' and _pid_alive(signal.get('pid'))
        return signal
    except (OSError, ValueError, KeyError, TypeError):
        return {}


LABELS = {'IDLE': 'Задача ещё не запущена', 'PENDING': 'В очереди',
          'PREFLIGHT': 'Подготовка', 'RUNNING': 'Модель выполняет задачу',
          'CLAIMED': 'Задача назначена исполнителю', 'CHECKPOINTED': 'Ожидает проверки',
          'VERIFY': 'Выполняются проверки', 'VERIFYING': 'Выполняются проверки',
          'WAITING_FOR_SLOT': 'Ожидание свободного слота',
          'PENDING_APPROVAL': 'Нужно подтверждение', 'RETRY': 'Повторная попытка',
          'COMPLETED': 'Завершено', 'BLOCKED': 'Остановлено — см. ошибку',
          'ERROR': 'Не удалось прочитать состояние', 'CANCELLED': 'Отменено'}

ACTIVE_STATUSES = frozenset(('PENDING', 'PREFLIGHT', 'RUNNING', 'CLAIMED',
                             'CHECKPOINTED', 'VERIFY', 'VERIFYING',
                             'WAITING_FOR_SLOT', 'PENDING_APPROVAL', 'RETRY'))


def focus_order(item):
    """Show the newest active project last in both native terminal monitors."""
    age = item.get('updated_seconds_ago')
    # Unknown age is older than every known update, not current work.
    recency = -age if isinstance(age, (int, float)) else float('-inf')
    return (item.get('status') in ACTIVE_STATUSES, recency,
            str(item.get('path') or item.get('project') or ''))


def focus_label(item):
    status = item.get('status', 'UNKNOWN')
    live = item.get('live') or {}
    if status in ('RUNNING', 'VERIFY'):
        if live.get('alive') and live.get('heartbeat_age', 999) <= 10:
            return 'процесс работает'
        if live.get('alive'):
            return 'процесс есть, свежего сигнала нет'
        return 'работа записана, живость не подтверждена'
    return LABELS.get(status, status)


def safe_text(value):
    # State/error text is data: do not allow terminal escape sequences.
    return ''.join(c if c.isprintable() or c == '\n' else ' ' for c in str(value))


def guard_display(value):
    """Bound untrusted saved guard evidence before it reaches a monitor."""
    value = value if isinstance(value, dict) else {}
    owners = []
    for item in value.get('owners', [])[:49] if isinstance(value.get('owners'), list) else []:
        if not isinstance(item, dict):
            continue
        record = {key: safe_text(item.get(key, 'unknown')).replace('\n', ' ')[:120]
                  for key in ('project', 'task_id', 'model', 'reasoning')}
        held = item.get('held_seconds')
        # Bound numeric text too; state is untrusted and a giant integer is
        # not useful monitor evidence.
        record['held_seconds'] = min(max(0, held), 315360000) if type(held) is int else 'unknown'
        owners.append(record)
    return {'wait_seconds': value.get('wait_seconds') if isinstance(value.get('wait_seconds'), int) else 0,
            'owners': owners}

def project_status(project):
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        return {'project': root.name, 'path': str(root), 'status': 'ERROR', 'reason': 'Project folder missing'}
    try:
        configured = json.loads((root / '.ai-dev/config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        configured = {}
    access = 'FULL_ACCESS' if isinstance(configured, dict) and configured.get('windows_worker_full_access') is True else 'WORKSPACE'
    path = root / '.ai-dev' / 'state.json'
    if not path.exists():
        candidates = list((root / '.ai-dev/orchestration/tasks').glob('*.json'))
        if not candidates:
            return {'project': root.name, 'path': str(root), 'status': 'IDLE', 'worker_access': access}
        try:
            path = max(candidates, key=lambda candidate: candidate.stat().st_mtime)
        except OSError as exc:
            return {'project': root.name, 'path': str(root), 'status': 'ERROR', 'reason': str(exc)}
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'project': root.name, 'path': str(root), 'status': 'ERROR', 'reason': str(exc)}
    if not isinstance(state, dict):
        return {'project': root.name, 'path': str(root), 'status': 'ERROR', 'reason': 'Invalid state object'}
    if not isinstance(state.get('status', 'UNKNOWN'), str):
        return {'project': root.name, 'path': str(root), 'status': 'ERROR', 'reason': 'Invalid status'}
    updated = state.get('updated_at')
    verification = state.get('verification') or {}
    checks = verification.get('checks') or [] if isinstance(verification, dict) else []
    last_check = checks[-1] if isinstance(checks, list) and checks and isinstance(checks[-1], dict) else {}
    valid_checks = [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    failures = [c for c in valid_checks if c.get('status') in ('FAIL', 'ERROR') or
                ('exit_code' in c and c['exit_code'] != 0)]
    if failures:
        last_check = failures[0]
    progress = state.get('progress') or {}
    if not isinstance(progress, dict):
        progress = {}
    verify = ('FAIL' if failures or (isinstance(verification, dict) and verification.get('ok') is False) else
              'PASS' if valid_checks and all(c.get('status') == 'PASS' or c.get('exit_code') == 0 for c in valid_checks) else 'NOT_RUN')
    decisions = [d for d in state.get('decisions', []) if isinstance(d, dict)]
    active = state.get('active_decision')
    if not isinstance(active, dict) or not active:
        active = decisions[-1] if decisions else {}
    # Only show a planner whose result was accepted; no prompt/transcript here.
    planner = {}
    if state.get('plan'):
        for attempt in reversed(state.get('planning_attempts', [])):
            if isinstance(attempt, dict) and (attempt.get('codex') or {}).get('ok'):
                planner = attempt.get('decision') or {}
                break
    previous_worker = next((d for d in reversed(decisions)
                            if (d.get('model'), d.get('reasoning')) !=
                            (active.get('model'), active.get('reasoning'))), {})
    return {'project': root.name, 'path': str(root), 'task_id': state.get('id') or state.get('task_id'),
            'worker_access': 'FULL_ACCESS' if state.get('config', {}).get('windows_worker_full_access') else access,
            'title': str(state.get('prompt') or state.get('objective') or '')[:180],
            'model': active.get('model', '?'), 'reasoning': active.get('reasoning', '?'),
            'role': active.get('role', 'worker'), 'selection_reason': active.get('reason', ''),
            'routing_policy': state.get('routing_policy'),
            'planner': {k: planner[k] for k in ('model', 'reasoning') if k in planner},
            'previous_worker': {k: previous_worker[k] for k in ('model', 'reasoning') if k in previous_worker},
            'worker_failures': state.get('worker_failures', 0),
            'megaprog_version': state.get('megaprog_version'), 'self_health': state.get('self_health'),
            'chatgpt_offer': state.get('chatgpt_offer'),
            'chatgpt_plan_offer': state.get('chatgpt_plan_offer'),
            'checkpoint_index': state.get('checkpoint_index', 0),
            'checkpoint_count': len((state.get('plan') or {}).get('steps', [])) if (state.get('plan') or {}).get('schema') == 2 else (1 if state.get('plan') else 0),
            'checkpoints_completed': len(state.get('checkpoint_history', [])),
            'live': execution_signal(root, state),
            'status': state.get('status', 'UNKNOWN'), 'attempts': state.get('attempts', 0),
            'updated_seconds_ago': max(0, int(time.time() - updated)) if isinstance(updated, (int, float)) else None,
            'reason': state.get('reason', '') or (last_check.get('output_tail') or last_check.get('tail') or '')[-4000:],
            'source': str(path), 'verify': verify,
            'last_verify_command': last_check.get('command') or last_check.get('name'),
            'progress': progress, 'guard': guard_display(state.get('guard'))}

def dashboard(projects):
    items = sorted((project_status(project) for project in projects), key=focus_order)
    counts = {}
    for item in items:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    return {'projects': items, 'counts': counts, 'model_turns_started': 0}

def render(result, overview=False):
    if overview:
        return render_overview(result)
    lines = ['MEGAPROG — THIS TASK | v%s | %s | проектов: %d' %
             (__version__, datetime.now().strftime('%H:%M:%S'), len(result['projects'])),
             'Экран обновляется каждые 2 с. Ctrl+C закрывает только монитор.']
    for item in sorted(result['projects'], key=focus_order):
        progress = item.get('progress') or {}
        lines.extend(['', '[%s] %s — %s' % (item['status'], item['project'], LABELS.get(item['status'], item['status'])),
                      '  Модель: %s | режим: %s' % (item.get('model', '?'), item.get('reasoning', '?')),
                      '  Доступ Windows worker: %s' % item.get('worker_access', 'WORKSPACE'),
                      '  Роль: %s | счётчик неудач: %s' %
                      ({'planner': 'Планировщик', 'worker': 'Исполнитель', 'recovery': 'Сильная модель разбирает ошибки'}.get(item.get('role'), 'Исполнитель'), item.get('worker_failures', 0)),
                      '  Попытка: %s | проверка: %s' % (item.get('attempts', 0), item.get('verify', 'NOT_RUN'))])
        planner = item.get('planner') or {}
        if planner:
            lines.append('  План составила: %s / %s' % (planner.get('model', '?'), planner.get('reasoning', '?')))
        if item.get('selection_reason'):
            lines.append('  Почему эта модель: %s' % item['selection_reason'])
        previous = item.get('previous_worker') or {}
        if previous and item['status'] in ('RUNNING', 'VERIFY', 'WAITING_FOR_SLOT'):
            lines.append('  Смена исполнителя: %s / %s → %s / %s' % (
                previous.get('model', '?'), previous.get('reasoning', '?'), item.get('model', '?'), item.get('reasoning', '?')))
        if (item.get('routing_policy') == 'plan-first-v1' and item.get('role') == 'worker'
                and item['status'] == 'RUNNING'):
            lines.append('  После двух неудач: сильная модель в новой сессии.')
        live = item.get('live') or {}
        if item.get('checkpoint_count'):
            lines.append('  Этап: %s/%s | проверено этапов: %s' % (
                item.get('checkpoint_index', 0) + 1, item['checkpoint_count'], item.get('checkpoints_completed', 0)))
        if item['status'] in ('RUNNING', 'VERIFY') and live:
            if live.get('alive') and live['heartbeat_age'] <= 10:
                lines.append('  Исполнитель: процесс работает; сигнал получен %s назад' % duration(live['heartbeat_age']))
            elif live.get('alive'):
                lines.append('  ВНИМАНИЕ: процесс существует, но свежего сигнала исполнителя нет.')
            else:
                lines.append('  ВНИМАНИЕ: процесс завершился; ожидаем итоговый статус задачи.')
            lines.append('  Сейчас: %s' % live.get('activity', 'Ожидание событий'))
            lines.append('  В работе: %s | последнее действие: %s назад | событий: %s' %
                         (duration(time.time() - live['started_at']), duration(live.get('event_age')), live.get('events', 0)))
            if item['status'] == 'RUNNING' and (live.get('event_age') is None or live['event_age'] > 60):
                lines.append('  Ждём новое событие Codex. Живой процесс не гарантирует продвижение задачи.')
        else:
            doing = progress.get('doing')
            lines.append('  Сейчас: %s' % (doing if doing and doing != item['status'] else LABELS.get(item['status'], item['status'])))
            lines.append('  Состояние обновлено: %s назад' % duration(item.get('updated_seconds_ago')))
            if item['status'] == 'RUNNING':
                lines.append('  Нет телеметрии этого запуска: подтвердить живость исполнителя нельзя.')
        # These labels deliberately describe stored/observed state.  In
        # particular, a RUNNING value without a live signal is never phrased
        # as evidence that a worker is still doing work.
        operation = (live.get('activity') if live else None) or progress.get('doing') or 'нет наблюдаемой операции'
        stage = progress.get('stage') or item.get('status', 'UNKNOWN')
        last_result = item.get('verify', 'NOT_RUN')
        lines.append('  Наблюдаемый этап: %s | операция: %s' % (safe_text(stage), safe_text(operation)))
        if live.get('observed_file'):
            lines.append('  Файл: %s' % safe_text(live['observed_file'])[:120])
        lines.append('  Последний результат: %s | следующая операция: %s' % (
            safe_text(last_result), safe_text(progress.get('next_step') or 'Проверить состояние задачи')))
        if item['status'] == 'WAITING_FOR_SLOT':
            guard = item.get('guard') or {}
            # The wait notice is persisted only when ownership changes. Add
            # time since that notice so the displayed wait keeps advancing
            # without writing the same state every two seconds.
            elapsed_since_notice = item.get('updated_seconds_ago')
            wait_seconds = guard.get('wait_seconds', 0)
            if isinstance(elapsed_since_notice, (int, float)):
                wait_seconds += elapsed_since_notice
            lines.append('  Ожидание слота: %s' % duration(wait_seconds))
            owners = guard.get('owners') or []
            if owners:
                for owner in owners[:5]:
                    lines.append('  Владелец слота: %s | %s | %s / %s | удерживает %s' % (
                        owner.get('project', 'unknown'), owner.get('task_id', 'unknown'),
                        owner.get('model', 'unknown'), owner.get('reasoning', 'unknown'),
                        owner_duration(owner.get('held_seconds'))))
            else:
                lines.append('  Владелец слота: unknown (старый или недоступный slot record)')
        lines.append('  Дальше: %s' % (progress.get('next_step') or 'Ожидать новую задачу' if item['status'] == 'COMPLETED' else progress.get('next_step') or 'Проверить состояние задачи'))
        if item.get('self_health') and item['status'] == 'BLOCKED':
            lines.append('  Самодиагностика: ' + item['self_health'].get('guidance', 'Смотрите отчёт'))
        offer = item.get('chatgpt_offer')
        if isinstance(offer, dict) and offer.get('status') == 'OFFERED':
            lines.append('  Внешний ChatGPT advisory: %s | файл: %s' %
                         (offer.get('recommended_mode', 'normal'), offer.get('path', '?')))
        plan_offer = item.get('chatgpt_plan_offer')
        if isinstance(plan_offer, dict) and plan_offer.get('status') == 'OFFERED':
            lines.append('  План через обычный ChatGPT: %s | затем plan-import и resume' %
                         plan_offer.get('path', '?'))
        lines.append('  Папка: %s' % item['path'])
        age = item.get('updated_seconds_ago')
        if not live and isinstance(age, (int, float)) and age > 60 and item['status'] in ('RUNNING', 'CLAIMED', 'VERIFY', 'VERIFYING'):
            lines.append('  Нет новых событий больше минуты. Монитор работает; живость исполнителя не подтверждена.')
        if item['status'] in ('BLOCKED', 'ERROR'):
            lines.extend(['--- ERROR COPY START ---', 'project: %s' % item['path'],
                          'task_id: %s' % item.get('task_id', '?'), 'phase: %s' % item['status'],
                          'reason: %s' % (item.get('reason') or 'Причина не записана'),
                          'verify_command: %s' % item.get('last_verify_command', '?'),
                          'state_path: %s' % item.get('source', '?'),
                          'report_path: %s' % progress.get('report_path', 'не записан'),
                          '--- ERROR COPY END ---'])
        title = item.get('title') or item.get('task_id') or 'нет задачи'
        title = safe_text(title).replace('\n', ' ')
        if len(title) > 120:
            title = title[:119] + '…'
        heading = 'ТЕКУЩАЯ ЗАДАЧА' if item['status'] in ACTIVE_STATUSES else 'ПОСЛЕДНЯЯ ЗАДАЧА'
        lines.extend(['', '══ %s: %s ══' % (heading, item['project']),
                      '  Задача: %s' % title,
                      '  Статус: %s — %s' % (item['status'], focus_label(item)),
                      '  Модель: %s / %s' % (item.get('model', '?'), item.get('reasoning', '?')),
                      '  Дальше: %s' % (progress.get('next_step') or 'Проверить состояние задачи')])
    return safe_text('\n'.join(lines))


def render_overview(result):
    projects = sorted(result['projects'], key=focus_order)
    counts = result.get('counts') or {}
    lines = ['MEGAPROG — ALL PROJECTS | v%s | %s' % (__version__, datetime.now().strftime('%H:%M:%S')),
             'Работа: %d | проверки: %d | завершено: %d | остановлено: %d' %
             (counts.get('RUNNING', 0), counts.get('VERIFY', 0) + counts.get('VERIFYING', 0),
              counts.get('COMPLETED', 0), counts.get('BLOCKED', 0) + counts.get('ERROR', 0)),
             'Все проекты: одна строка на проект. Подробности и ошибки — в отдельных окнах.']
    for item in projects:
        live = item.get('live') or {}
        status = LABELS.get(item['status'], item['status'])
        if live:
            status = ('Процесс работает' if live.get('alive') and live.get('heartbeat_age', 999) <= 10 else
                      'Нет свежего сигнала' if live.get('alive') else 'Процесс завершился; ждём итог')
        elif item['status'] == 'RUNNING':
            status = 'живость пока не подтверждена'
        project = safe_text(item['project']).replace('\n', ' ')[:20]
        status = safe_text(status).replace('\n', ' ')[:30]
        model = str(item.get('model') or '?').rsplit('-', 1)[-1].title()
        reasoning = str(item.get('reasoning') or '?')[:3]
        lines.append('[%s] %-20s %s | %s/%s | %s' %
                     (item['status'], project, status, model, reasoning, item.get('worker_access', 'WORKSPACE')))
    active = [item for item in projects if item.get('status') in ACTIVE_STATUSES]
    if active:
        item = active[-1]
        lines.extend(['', '══ ТЕКУЩАЯ ЗАДАЧА: %s ══' % item['project'],
                      '  Статус: %s — %s' % (item['status'], focus_label(item)),
                      '  Модель: %s / %s' % (item.get('model', '?'), item.get('reasoning', '?'))])
    if not projects:
        lines.append('\nНет зарегистрированных проектов. Новые задачи появятся автоматически.')
    lines.append('\nОбновление каждые 2 с. Этот обзор не запускает дополнительные модели.')
    return safe_text('\n'.join(lines))


def signature(result):
    """Return state-change evidence, excluding clock-only telemetry fields.

    The monitor still prints a compact heartbeat with elapsed time between
    changes.  Repainting a full screen merely because an age ticked would make
    a quiet or stalled task look busy and creates noisy redirected output.
    """
    items = []
    for item in result['projects']:
        record = {k: v for k, v in item.items() if k != 'updated_seconds_ago'}
        live = record.get('live')
        if isinstance(live, dict):
            live = dict(live)
            heartbeat_age = live.pop('heartbeat_age', None)
            event_age = live.pop('event_age', None)
            # Threshold crossings are meaningful display changes; individual
            # seconds are not.
            live['heartbeat_fresh'] = (isinstance(heartbeat_age, (int, float)) and heartbeat_age <= 10)
            live['event_stale'] = (event_age is None or
                                   (isinstance(event_age, (int, float)) and event_age > 60))
            record['live'] = live
        age = item.get('updated_seconds_ago')
        record['_stale'] = (isinstance(age, (int, float)) and age > 60 and
                            item['status'] in ('RUNNING', 'CLAIMED', 'VERIFY', 'VERIFYING'))
        items.append(record)
    return json.dumps(items, sort_keys=True, ensure_ascii=False)


def heartbeat(result, tick):
    ages = [item.get('updated_seconds_ago') for item in result['projects']]
    known = [age for age in ages if isinstance(age, (int, float))]
    return safe_text('[%s] %s Монитор | проектов: %d | последнее событие: %ss' %
                     (datetime.now().strftime('%H:%M:%S'), '|/-\\'[tick % 4],
                      len(result['projects']), min(known) if known else '?'))
