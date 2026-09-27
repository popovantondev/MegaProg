"""Bounded planning contract and strong recovery policy."""
import json
import re
from pathlib import PurePosixPath
from . import routing

MAX_TITLE_CHARS = 160
MAX_ACCEPTANCE_CHARS = 1200
PLAN_KEYS = {'schema', 'objective', 'worker_kind', 'steps'}
STEP_KEYS_V2 = {'title', 'worker_kind', 'files', 'acceptance'}


class PlanParseError(ValueError):
    """A classified planner-output rejection with durable diagnostic fields."""
    def __init__(self, message, error_class, *, schema_valid=False,
                 semantic_valid=None, actions=None):
        super().__init__(message)
        self.diagnostics = {
            'parse_status': 'REJECTED',
            'error_class': error_class,
            'normalization_applied': bool(actions),
            'normalization_actions': list(actions or []),
            'schema_valid': schema_valid,
            'semantic_valid': (False if semantic_valid is None and error_class in
                               ('SEMANTIC_PLAN_ERROR', 'PLAN_CONTRACT_SEMANTIC_ERROR')
                               else semantic_valid),
            'retry_required': error_class in ('SEMANTIC_PLAN_ERROR', 'PLAN_CONTRACT_SEMANTIC_ERROR'),
            'retry_reason': (str(message)[:400] if error_class in
                             ('SEMANTIC_PLAN_ERROR', 'PLAN_CONTRACT_SEMANTIC_ERROR')
                             else 'Детерминированная нормализация не восстановила контракт; сильный повтор остановлен.'),
        }


def _remove_trailing_commas(text):
    output = []
    actions = []
    quoted = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if char == ',':
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in '}]':
                actions.append('removed_trailing_comma')
                index += 1
                continue
        output.append(char)
        index += 1
    return ''.join(output), actions


def _extract_object(text, actions=None):
    actions = list(actions or [])
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PlanParseError('Повторяющееся поле JSON: ' + str(key),
                                     'FORMAT_ERROR', actions=actions)
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    leading = text.lstrip()
    try:
        root_value, _ = decoder.raw_decode(text)
    except json.JSONDecodeError:
        if leading.startswith('{'):
            raise PlanParseError('Корневой JSON-объект повреждён и не может быть однозначно восстановлен.',
                                 'FORMAT_ERROR', actions=actions)
        root_value = None
    else:
        if not isinstance(root_value, dict):
            raise PlanParseError('Корневой JSON-элемент плана должен быть объектом.',
                                 'FORMAT_ERROR', actions=actions)
    candidates = []
    covered_until = -1
    for start, char in enumerate(text):
        if char != '{' or start < covered_until:
            continue
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            end = start + consumed
            candidates.append((start, end, value))
            covered_until = end
    if len(candidates) != 1:
        reason = ('В ответе не найден JSON-объект.' if not candidates else
                  'В ответе найдено несколько JSON-объектов; выбор неоднозначен.')
        raise PlanParseError(reason, 'FORMAT_ERROR', actions=actions)
    start, end, value = candidates[0]
    return text[start:end], value, (start != 0 or end != len(text))


def _normalized_plan_source(message):
    actions = []
    if not isinstance(message, str) or len(message.encode('utf-8')) > 12000:
        raise PlanParseError('План отсутствует или превышает 12000 байт.', 'FORMAT_ERROR')
    value = message
    if value.startswith('\ufeff'):
        value = value[1:]
        actions.append('removed_utf8_bom')
    stripped = value.strip()
    if stripped != value:
        actions.append('trimmed_outer_whitespace')
    value = stripped
    fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```', value,
                         flags=re.IGNORECASE | re.DOTALL)
    if fence:
        value = fence.group(1).strip()
        actions.append('stripped_markdown_code_fence')
    elif value.startswith('```') or value.endswith('```'):
        raise PlanParseError('Неполный JSON-блок плана.', 'FORMAT_ERROR', actions=actions)
    value, comma_actions = _remove_trailing_commas(value)
    actions.extend(comma_actions)
    extracted, data, did_extract = _extract_object(value, actions)
    if did_extract:
        actions.append('extracted_single_json_object')
    return extracted, data, actions


def parse_plan_detailed(message):
    """Normalize only unambiguous serialization defects, then validate the contract."""
    try:
        _, data, actions = _normalized_plan_source(message)
    except PlanParseError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise PlanParseError('Не удалось прочитать JSON плана: ' + str(exc)[:250],
                             'FORMAT_ERROR') from exc

    unknown = set(data) - PLAN_KEYS
    if unknown:
        raise PlanParseError('Неизвестные поля плана нельзя безопасно отбросить: %s.' %
                             ', '.join(sorted(map(str, unknown))), 'FORMAT_ERROR', actions=actions)
    schema = data.get('schema')
    if 'schema' in data and (type(schema) is not int or schema != 2):
        raise PlanParseError('Неподдерживаемая версия схемы плана.', 'FORMAT_ERROR', actions=actions)
    modern = 'schema' in data
    if 'objective' not in data or 'steps' not in data:
        missing = [key for key in ('objective', 'steps') if key not in data]
        raise PlanParseError('В плане отсутствуют обязательные смысловые поля: %s.' %
                             ', '.join(missing), 'SEMANTIC_PLAN_ERROR',
                             schema_valid=False, semantic_valid=False, actions=actions)
    if not isinstance(data.get('objective'), str):
        raise PlanParseError('Поле objective должно быть строкой.', 'FORMAT_ERROR', actions=actions)
    if not isinstance(data.get('steps'), list):
        raise PlanParseError('Поле steps должно быть массивом.', 'FORMAT_ERROR', actions=actions)
    if 'worker_kind' in data and not isinstance(data['worker_kind'], str):
        raise PlanParseError('Поле worker_kind должно быть строкой.', 'FORMAT_ERROR', actions=actions)

    objective = data['objective']
    steps = data['steps']
    if not objective.strip():
        raise PlanParseError('В плане нет цели.', 'SEMANTIC_PLAN_ERROR',
                             schema_valid=True, semantic_valid=False, actions=actions)
    if modern:
        if not 1 <= len(steps) <= 4:
            raise PlanParseError('Нужны 1–4 самостоятельных проверяемых этапа.',
                                 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
    elif not 1 <= len(steps) <= 8:
        raise PlanParseError('План должен содержать от 1 до 8 конкретных шагов.',
                             'SEMANTIC_PLAN_ERROR', schema_valid=True,
                             semantic_valid=False, actions=actions)

    cleaned = []
    for index, step in enumerate(steps):
        if modern and (not isinstance(step, dict)):
            raise PlanParseError('Этап %d должен быть объектом.' % (index + 1),
                                 'FORMAT_ERROR', actions=actions)
        if not modern and (not isinstance(step, str) or not step.strip()):
            raise PlanParseError('Legacy steps должны быть непустыми строками.',
                                 'FORMAT_ERROR', actions=actions)
        if not modern:
            continue
        unknown_step = set(step) - STEP_KEYS_V2
        if unknown_step:
            raise PlanParseError('Неизвестные поля этапа нельзя безопасно отбросить: %s.' %
                                 ', '.join(sorted(map(str, unknown_step))),
                                 'FORMAT_ERROR', actions=actions)
        worker_kind = step.get('worker_kind')
        if 'worker_kind' not in step:
            raise PlanParseError('Этап %d: отсутствует worker_kind.' % (index + 1),
                                 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
        if not isinstance(worker_kind, str):
            raise PlanParseError('Этап %d: worker_kind должен быть строкой.' % (index + 1),
                                 'FORMAT_ERROR', actions=actions)
        if worker_kind not in ('small', 'normal', 'hard'):
            raise PlanParseError('Этап %d: worker_kind должен быть small/normal/hard.' %
                                 (index + 1), 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
        title = step.get('title')
        acceptance = step.get('acceptance')
        if 'title' in step and not isinstance(title, str):
            raise PlanParseError('Этап %d: title должен быть строкой.' % (index + 1),
                                 'FORMAT_ERROR', actions=actions)
        if 'acceptance' in step and not isinstance(acceptance, str):
            raise PlanParseError('Этап %d: acceptance должен быть строкой.' % (index + 1),
                                 'FORMAT_ERROR', actions=actions)
        if not isinstance(title, str) or not title.strip():
            raise PlanParseError('Этап %d: отсутствует содержательный title.' % (index + 1),
                                 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
        if not isinstance(acceptance, str) or not acceptance.strip():
            raise PlanParseError('Этап %d: отсутствует проверяемый acceptance.' % (index + 1),
                                 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
        if len(title) > MAX_TITLE_CHARS:
            raise PlanParseError('Этап %d: title содержит %d символов, максимум %d.' %
                                 (index + 1, len(title), MAX_TITLE_CHARS),
                                 'FORMAT_ERROR', schema_valid=False,
                                 semantic_valid=None, actions=actions)
        if len(acceptance) > MAX_ACCEPTANCE_CHARS:
            raise PlanParseError('Этап %d: acceptance содержит %d символов, максимум %d. '
                                 'Автоматически сокращать критерий небезопасно.' %
                                 (index + 1, len(acceptance), MAX_ACCEPTANCE_CHARS),
                                 'FORMAT_ERROR', schema_valid=False,
                                 semantic_valid=None, actions=actions)
        files = step.get('files')
        if 'files' in step and not isinstance(files, list):
            raise PlanParseError('Этап %d: files должен быть массивом.' % (index + 1),
                                 'FORMAT_ERROR', actions=actions)
        if not isinstance(files, list) or not 1 <= len(files) <= 6:
            raise PlanParseError('Укажите 1–6 файлов/каталогов этапа.',
                                 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
        paths = []
        for name in files:
            if not isinstance(name, str) or not name.strip() or len(name) > 300:
                raise PlanParseError('Недопустимый путь в плане.', 'FORMAT_ERROR', actions=actions)
            name = name.replace('\\', '/')
            path = PurePosixPath(name)
            if path.is_absolute() or ':' in name or '..' in path.parts or any(
                    part in ('.git', '.ai-dev') for part in path.parts):
                raise PlanParseError('Путь этапа выходит за разрешённую область проекта.',
                                     'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                     semantic_valid=False, actions=actions)
            paths.append(name)
        cleaned.append({'id': 'step-%d' % (index + 1), 'title': title,
                        'worker_kind': worker_kind, 'files': paths,
                        'acceptance': acceptance})
    if not modern:
        if data.get('worker_kind') not in ('small', 'normal', 'hard'):
            raise PlanParseError('План должен задавать worker_kind: small/normal/hard.',
                                 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
        plan = {'objective': objective, 'worker_kind': data['worker_kind'], 'steps': steps}
    else:
        if ('worker_kind' in data and
                data['worker_kind'] != cleaned[0]['worker_kind']):
            raise PlanParseError('Верхнеуровневый worker_kind противоречит первому этапу.',
                                 'SEMANTIC_PLAN_ERROR', schema_valid=True,
                                 semantic_valid=False, actions=actions)
        plan = {'schema': 2, 'objective': objective, 'worker_kind': cleaned[0]['worker_kind'],
                'steps': cleaned}
    return plan, {
        'parse_status': 'REPAIRED' if actions else 'ACCEPTED',
        'error_class': 'NONE', 'normalization_applied': bool(actions),
        'normalization_actions': actions, 'schema_valid': True,
        'semantic_valid': True, 'retry_required': False,
        'retry_reason': 'none',
    }


def strong(catalog, recovery=False, medium=False, task_kind='hard'):
    discovery_error = (catalog.get('errors') or {}).get('models')
    if discovery_error:
        raise ValueError('Не удалось получить каталог Codex-моделей: %s. '
                         'Это сбой discovery, а не доказательство занятой квоты или отсутствия Astra/Sol.' %
                         discovery_error)
    reasons = []
    if not recovery and task_kind in ('small', 'normal'):
        candidates = ((routing.ORDER[-2], 'medium'),)
    else:
        candidates = ((routing.ORDER[-1], 'medium' if medium else 'low'),
                      (routing.ORDER[-2], 'medium'))
    for model, effort in candidates:
        try:
            decision = routing.choose(catalog, '', kind='expert', model=model, reasoning=effort)
            decision['role'] = 'recovery' if recovery else 'planner'
            decision['reason'] = ('Повышение после двух неудач исполнителя.' if recovery else
                                  'Постановка задачи и план перед недорогим исполнителем.')
            return decision
        except ValueError as exc:
            reasons.append(str(exc))
    raise ValueError('Сильный этап не запущен. ' + ' | '.join(reasons))


def parse_plan(message):
    return parse_plan_detailed(message)[0]


PLAN_INSTRUCTIONS = '''Analyze the task and relevant source/tests in read-only mode.
Do not implement, write files, run mutating checks, or spawn agents. Identify the
actual cause and dependencies, not just generic instructions to "inspect and fix".
Build 1-4 coherent implementation checkpoints. Each must deliver a useful increment
and keep ALL configured checks passing before the next checkpoint can begin.
Include implementation, tests, and review in EACH checkpoint, never separate
test/review-only checkpoints. A tiny task MUST have just ONE checkpoint.
Specify the exact acceptance condition, 1-6 relevant relative source paths, and a
worker appropriate to that checkpoint: small (Luna low), normal (Luna medium), hard (Sol medium).
Choose hard for coupled changes, concurrency, migrations, unclear root cause, or
architecture work; do not send all work to Luna simply to minimize per-call cost.
Return ONLY JSON: {"schema":2,"objective":"...","steps":[{"title":"...",
"worker_kind":"normal","files":["src/example.py"],"acceptance":"..."}]}.
Each title must be at most 160 characters. Each acceptance must be at most 1200
characters and state only the finished behavior and its verification; it is NOT a
second detailed implementation specification. Keep the complete answer under 12000 UTF-8 bytes. Do not change the user's scope,
configured verification commands, permissions, or policy. No prolonged investigation.
'''


def checkpoints(plan):
    if plan.get('schema') == 2:
        return plan['steps']
    # Old prose plans were not designed as independently executable checkpoints.
    # Preserve their semantics instead of rerunning a whole task per prose line.
    return [{'id': 'step-1', 'title': plan['objective'], 'worker_kind': plan['worker_kind'],
             'acceptance': '\n'.join(plan['steps']), 'files': []}]


def worker_receipt(message, checkpoint):
    if not isinstance(message, str) or len(message.encode('utf-8')) > 6000:
        raise ValueError('Нет короткого результата текущего этапа.')
    text = message.strip()
    if text.startswith('```') and text.endswith('```'):
        if '\n' not in text:
            raise ValueError('Неполный JSON-блок результата этапа.')
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    data = json.loads(text)
    if not isinstance(data, dict) or data.get('checkpoint') != checkpoint['id'] or data.get('status') not in ('done', 'blocked'):
        raise ValueError('Исполнитель не подтвердил завершение текущего этапа.')
    summary = data.get('summary')
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise ValueError('Нужна короткая сводка результата этапа.')
    return summary


def worker_result(message, checkpoint):
    """Parse a bounded structured result, preserving optional typed evidence."""
    worker_receipt(message, checkpoint)
    text = message.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    data = json.loads(text)
    evidence = data.get('evidence', [])
    if not isinstance(evidence, list) or len(evidence) > 32:
        raise ValueError('Некорректный список evidence в результате этапа.')
    return {'status': data['status'], 'summary': data['summary'], 'evidence': evidence}


def worker_deviation(message, checkpoint):
    """Read an optional explicit Plan deviation from the stage receipt."""
    if not isinstance(message, str) or len(message.encode('utf-8')) > 6000:
        return None
    text = message.strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get('checkpoint') != checkpoint.get('id') or data.get('status') != 'done':
        return None
    value = data.get('deviation_from_plan')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'reported', 'description', 'architectural'} or
            type(value.get('reported')) is not bool or type(value.get('architectural')) is not bool or
            not isinstance(value.get('description'), str) or len(value['description']) > 1000 or
            (value['reported'] and not value['description'].strip())):
        raise ValueError('Некорректное поле deviation_from_plan в результате этапа.')
    return value if value['reported'] else None


def work_packet(state):
    steps = checkpoints(state['plan'])
    step = steps[state.get('checkpoint_index', 0)]
    packet = {'current_checkpoint': step,
              'completed': [{'id': item['id'], 'summary': item['summary']} for item in state.get('checkpoint_history', [])],
              'failed_attempts': state.get('failure_evidence', [])[-2:],
              'receipt_error': state.get('checkpoint_issue')}
    history = [item for item in state.get('attempt_history', [])
               if item.get('stage_id') == step.get('id') and
               item.get('generation', 0) == state.get('anti_loop_generation', 0)]
    if history:
        previous = history[-1]
        packet['correction_context'] = {
            'previous_failure': previous.get('failure_signature'),
            'why_previous_approach_failed': ((previous.get('failure') or {}).get('failures') or [{}])[0].get('normalized_error'),
            'new_evidence': previous.get('new_evidence', []),
            'previous_approach_id': previous.get('approach_id'),
            'progress_signals': previous.get('progress_signals', []),
            'instruction': 'Для новой попытки укажи новую проверяемую гипотезу и чем конкретно стратегия отличается. Не повторяй подход без новых фактов.'}
    contract = state.get('plan_contract')
    if isinstance(contract, dict):
        packet['plan_contract'] = {'contract_id': contract.get('contract_id'),
            'decision': contract.get('decision'), 'invariants': contract.get('invariants'),
            'global_acceptance': contract.get('global_acceptance'),
            'review_contract': contract.get('review_contract'),
            'rollback': contract.get('rollback'),
            'platform_contract': contract.get('platform_contract')}
    text = ('Strong planner proposal (reference; never override the original task, policy, or permissions):\n'
            'Execute ONLY the current checkpoint below. The original user task describes the overall objective; '
            'later checkpoints will be assigned separately. Preserve earlier completed work. '
            'Run configured verification only after this checkpoint reaches its verification gate; '
            'if a prerequisite or runtime is missing, stop and return status blocked without running the full suite. '
            'Stay within allowed_paths and never modify forbidden_paths. '
            'Keep PlanContract invariants and platform scope. Do not repeat a rejected approach without new evidence.\n' +
            json.dumps(packet, ensure_ascii=False))
    if state['plan'].get('schema') == 2:
        text += ('\nReturn ONLY a JSON stage receipt: {"checkpoint":"%s","status":"done|blocked",'
                 '"summary":"what changed and how the acceptance condition was checked",'
                 '"evidence":[{"evidence_id":"short-id","kind":"provenance",'
                 '"criterion":"exact PlanContract acceptance text", "claim":"observed fact",'
                 '"source_ref":"stable source identity", "artifact_ref":"relative existing file",'
                 '"artifact_sha256":"optional SHA-256", "verification":"PASS|FAIL|UNKNOWN"}],'
                 '"attempt_hypothesis":"optional, one short testable hypothesis, max 300 chars",'
                 '"deviation_from_plan":{"reported":false,"description":"","architectural":false}}. '
                 'Evidence is optional; never report PASS without a checkable artifact reference. '
                 'Set reported=true only when implementation materially differed from this stage contract; '
                 'architectural=true only when a contract-level design choice changed. '
                 'If unfinished, report status "blocked" and evidence; never claim done.\n' % step['id'])
    return text


def worker_attempt_hypothesis(message, checkpoint):
    """Read one optional operational hypothesis; never request chain-of-thought."""
    if not isinstance(message, str) or len(message.encode('utf-8')) > 6000:
        return None
    text = message.strip()
    if text.startswith('```') and text.endswith('```') and '\n' in text:
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get('attempt_hypothesis')
    if (data.get('checkpoint') != checkpoint.get('id') or not isinstance(value, str) or
            not value.strip() or len(value) > 300):
        return None
    return ' '.join(value.split())


def infrastructure_check(verification):
    for check in verification.get('checks', []):
        if check.get('exit_code') == 0:
            continue
        text = str(check.get('tail', '')).lower()
        if check.get('exit_code') in (126, 127, 137, 143) or any(term in text for term in (
                'operation not permitted', 'permission denied', 'connection reset',
                'network is unreachable', 'command not found', 'usage limit', 'rate limit')):
            return True
    return False
