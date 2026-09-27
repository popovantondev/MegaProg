"""Explicit, conservative routing policy; not a price estimator."""
import time

ORDER = ['gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra']
KINDS = ('auto', 'small', 'normal', 'hard', 'expert')


def validate_model_turn(model, reasoning):
    """Block retired models before any new or resumed CLI model turn."""
    if model not in ORDER:
        raise ValueError('Для новых ходов MegaProg разрешены только GPT-6 Luna, Sol и Astra; '
                         'явный старый выбор нужно изменить в config и затем безопасно возобновить задачу.')
    allowed = ('low', 'medium') if model == ORDER[-1] else ('low', 'medium', 'high')
    if reasoning not in allowed:
        raise ValueError('Недопустимый reasoning для %s: %s.' % (model, reasoning))


def validate_request(kind='auto', model=None, reasoning=None):
    """Reject impossible pinned choices before a planner can spend a turn."""
    if kind not in KINDS:
        raise ValueError('Неизвестный тип задачи.')
    if model is not None and model not in ORDER:
        validate_model_turn(model, reasoning)
    if (model == ORDER[-1] or (not model and kind == 'expert')) and reasoning not in (None, 'low', 'medium'):
        raise ValueError('Политика MegaProg разрешает Astra только low/medium; выбран %s.' % reasoning)
    if reasoning is not None and reasoning not in ('low', 'medium', 'high'):
        raise ValueError('MVP не включает дорогие xhigh/max/ultra режимы.')


def classify(prompt):
    text = prompt.lower()
    if any(word in text for word in ('race condition', 'deadlock', 'concurrency', 'distributed', 'гонк', 'дедлок', 'распределён', 'архитектур')):
        return 'hard'
    if len(text) < 500 and any(word in text for word in ('typo', 'rename', 'css', 'опечатк', 'переимен', 'цвет', 'calculator', 'одну функцию')):
        return 'small'
    return 'normal'


def exhausted(limits, model):
    """Use only known bucket mappings and future reset windows."""
    if not limits:
        return False
    buckets = limits.get('rateLimitsByLimitId')
    candidates = []
    if isinstance(buckets, dict):
        # A model-specific bucket is authoritative.  An unknown bucket must
        # not accidentally be interpreted as a zero remaining quota.
        for bucket in buckets.values():
            if not isinstance(bucket, dict):
                continue
            if (bucket.get('normalModelSlug') == model or
                    bucket.get('modelSlug') == model or
                    bucket.get('model') == model or
                    bucket.get('limitName') == 'Codex'):
                candidates.append(bucket)
        if isinstance(buckets.get('codex'), dict):
            candidates.append(buckets['codex'])
    if not candidates and isinstance(limits.get('rateLimits'), dict):
        candidates.append(limits['rateLimits'])
    for bucket in candidates:
        if bucket.get('spendControlReached') is True:
            return True
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used, resets = window.get('usedPercent'), window.get('resetsAt')
            if (isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 100 and
                    isinstance(resets, (int, float)) and not isinstance(resets, bool) and
                    resets > time.time()):
                return True
    return False


def choose(catalog, prompt, kind='auto', model=None, reasoning=None, previous=None):
    validate_request(kind, model, reasoning)
    if (catalog.get('errors') or {}).get('models'):
        raise ValueError('Сбой получения каталога Codex: %s' % catalog['errors']['models'])
    models = {m['model']: m for m in catalog.get('models', []) if
              isinstance(m, dict) and isinstance(m.get('model'), str) and not m.get('hidden', False)}
    # Explicit Astra is itself an opt-in. Never require a second expert flag.
    actual = 'expert' if model == ORDER[-1] else classify(prompt) if kind == 'auto' else kind
    # Luna is the cheap default. Sol owns ordinary judgement and the old
    # Terra role; Astra is reserved for explicit expert or verified stalls.
    start = {'small': 0, 'normal': 0, 'hard': 1, 'expert': 2}[actual]
    if previous:
        start = min(ORDER.index(previous['model']) + 1, len(ORDER) - 1) if previous['model'] in ORDER else start
    candidates = [model] if model else ORDER[start:]
    rejected = []
    for candidate in candidates:
        # Automatic workers stay economical. Planner/recovery and explicit
        # expert/model choices are the separate authorized paths to Astra.
        if candidate == ORDER[-1] and actual != 'expert' and not previous:
            continue
        entry = models.get(candidate)
        if not entry:
            rejected.append('%s: отсутствует в доступном каталоге' % candidate)
            continue
        if exhausted(catalog.get('limits'), candidate):
            rejected.append('%s: подтверждено исчерпание квоты' % candidate)
            continue
        efforts = {e.get('reasoningEffort') for e in entry.get('supportedReasoningEfforts', []) if isinstance(e, dict)}
        desired = reasoning or ('low' if candidate == ORDER[-1] or actual == 'small' else 'medium')
        if candidate == 'gpt-6-astra' and desired not in ('low', 'medium'):
            raise ValueError('Astra high/xhigh/max/ultra отключены в MVP.')
        if desired not in ('low', 'medium', 'high'):
            raise ValueError('MVP не включает дорогие xhigh/max/ultra режимы.')
        if desired not in efforts:
            if reasoning or 'low' not in efforts:
                rejected.append('%s: режим %s не заявлен в каталоге' % (candidate, desired))
                continue
            desired = 'low'
        reason = 'Явный выбор пользователя.' if model else (
            'Повышение после провала проверок.' if previous else
            'Тип задачи: %s%s; первый доступный вариант по политике проекта.' %
            (actual, ' (эвристика текста)' if kind == 'auto' else ''))
        return {'model': candidate, 'reasoning': desired, 'kind': actual, 'reason': reason,
                'catalog_verified': True, 'runtime_verified': False}
    raise ValueError('Выбор модели остановлен: ' + '; '.join(rejected or ['нет кандидатов по политике задачи']))
