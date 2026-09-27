"""Manual ordinary-ChatGPT fallback for a planner contract that cannot proceed."""

import json
import os
import subprocess
import time
from pathlib import Path

from . import planning
from .report import write_report
from .storage import read, write


OFFER_NAME = 'chatgpt-plan.md'
MAX_RESPONSE_BYTES = 16000
NON_SEMANTIC_PLANNER_ERRORS = {'FORMAT_ERROR', 'SERIALIZATION_FORMAT_ERROR',
    'PLAN_CONTRACT_SCHEMA_ERROR', 'PLAN_CONTRACT_ENUM_ERROR', 'PLAN_CONTRACT_AMBIGUOUS'}


def _handoff_eligible(state):
    attempts = state.get('planning_attempts', [])
    history = state.get('planner_validation_history', [])
    stopped_for_format = bool(state.get('planner_format_error') and history and
                              history[-1].get('error_class') in NON_SEMANTIC_PLANNER_ERRORS and
                              not history[-1].get('retry_required'))
    return not state.get('plan') and (len(attempts) >= 2 or (attempts and stopped_for_format))


def create_plan_offer(root, state):
    """Create a copyable request; never contact ChatGPT or run another model."""
    root = Path(root)
    if not _handoff_eligible(state):
        return None
    directory = root / '.ai-dev' / 'runs' / state['id']
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / OFFER_NAME
    prompt = str(state.get('prompt') or '')
    if len(prompt) > 12000:
        prompt = prompt[:12000] + '\n[Задача сокращена: добавьте оставшуюся часть вручную перед отправкой.]'
    error_class = state.get('planner_error_class') or 'UNKNOWN'
    guidance = ('Выберите обычный режим ChatGPT: исправьте сериализацию или точную форму PlanContract; '
                'Deep Research для этого не требуется. '
                if error_class in NON_SEMANTIC_PLANNER_ERRORS else
                'Начните с обычного режима ChatGPT и попросите проверить полноту плана и ограничения задачи. ')
    lines = [
        '# MegaProg: план через обычный ChatGPT', '',
        'Планировщик не выдал пригодный контрактный план. MegaProg остановился; '
        'этот файл не отправлен в ChatGPT и не расходует его лимит.', '',
        guidance + 'Перед вставкой проверьте текст и удалите личные данные или секреты.', '',
        '## Скопируйте запрос', '',
        'Составь план разработки для задачи ниже. Не редактируй файлы и не запускай команды. '
        'Верни ТОЛЬКО один JSON-объект без Markdown и пояснений, с ключами task_id, base_head, plan. '
        'Не меняй исходную задачу, права, проверки или ограничения. План — подсказка: MegaProg '
        'проверит формат, а результат каждого этапа проверит командами проекта.', '',
        'task_id: ' + state['id'],
        'base_head: ' + state['base_head'],
        'Класс ошибки планирования: ' + error_class,
        'Ошибка прошлого плана: ' + str(state.get('plan_issue') or 'неверный контракт'), '',
        'Исходная задача:', prompt, '',
        'Обязательный формат ответа:',
        '{"task_id":"' + state['id'] + '","base_head":"' + state['base_head'] + '",'
        '"plan":{"schema":2,"objective":"полная цель задачи",'
        '"steps":[{"title":"короткий этап","worker_kind":"normal",'
        '"files":["src/example.py"],"acceptance":"проверяемый результат"}]}}', '',
        'Нужны 1–4 этапа. Каждый включает реализацию и тесты. worker_kind: small, normal '
        'или hard. title ≤160 символов, acceptance ≤1200, files — 1–6 относительных путей '
        'внутри проекта. Ответ ≤16000 байт UTF-8.', '',
        '## После ответа',
        'Сохраните полученный JSON вне папки проекта и выполните '
        '`ai-dev -C PROJECT plan-import PATH.json`, '
        'затем `ai-dev -C PROJECT resume`. Импорт сам не запускает модель.',
    ]
    temporary = destination.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(destination))
    error_class = state.get('planner_error_class') or 'FORMAT_ERROR'
    return {'status': 'OFFERED', 'path': str(destination), 'recommended_mode': 'normal',
            'reason': 'planner_format' if error_class in NON_SEMANTIC_PLANNER_ERRORS else 'planner_semantic',
            'note': 'ChatGPT не запускался; его доступность и лимит неизвестны.'}


def import_plan(root, response_path):
    """Validate an untrusted external plan against the stopped task and Git base."""
    root = Path(root).resolve()
    state_path = root / '.ai-dev' / 'state.json'
    state = read(state_path)
    if (state.get('status') != 'BLOCKED' or not _handoff_eligible(state) or
            state.get('attempts', 0) != 0 or
            state.get('chatgpt_plan_offer', {}).get('status') != 'OFFERED'):
        raise ValueError('Нет остановленной задачи, для которой разрешён ручной импорт плана.')
    raw = Path(response_path).expanduser().read_bytes()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError('Ответ ChatGPT превышает 16000 байт.')
    try:
        envelope = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Ответ должен быть UTF-8 JSON-объектом.') from exc
    if not isinstance(envelope, dict) or set(envelope) != {'task_id', 'base_head', 'plan'}:
        raise ValueError('Ответ должен содержать только task_id, base_head и plan.')
    if envelope['task_id'] != state['id'] or envelope['base_head'] != state['base_head']:
        raise ValueError('Ответ относится к другой задаче или версии проекта.')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=str(root), text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=str(root), text=True).strip()
    if head != state['base_head'] or dirty:
        raise ValueError('После остановки изменился Git HEAD или рабочая папка; план не импортирован.')
    plan = planning.parse_plan(json.dumps(envelope['plan'], ensure_ascii=False))
    if plan.get('schema') != 2:
        raise ValueError('Внешний план должен использовать schema 2.')
    state['plan'] = plan
    state['plan_source'] = 'ordinary_chatgpt_manual'
    state['chatgpt_plan_offer']['status'] = 'IMPORTED'
    state['planner_format_issue'] = state.pop('plan_issue', None)
    state.pop('planner_format_error', None)
    state.pop('last_failure', None)
    state['status'] = 'PREFLIGHT'
    state['reason'] = 'Внешний план прошёл проверку формата; исполнение ещё не запускалось.'
    state['updated_at'] = time.time()
    state['progress'] = dict(state.get('progress') or {}, phase='PREFLIGHT',
                             doing=state['reason'], next_step='Выполнить ai-dev resume')
    write(state_path, state)
    write(root / '.ai-dev' / 'runs' / state['id'] / 'state.json', state)
    write_report(root, state)
    return {'task_id': state['id'], 'status': 'IMPORTED', 'steps': len(plan['steps']),
            'next_step': 'Выполнить ai-dev resume'}
