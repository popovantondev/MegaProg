"""Read-only aggregation of usage and routing facts in local task runs."""

import json
from pathlib import Path
from .account_guard import classify
from .chatgpt_limits import report as chatgpt_limit_report
from .lessons import sync_summary

TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
UNKNOWN = '(unknown)'
STATUS_NAMES = ('completed', 'blocked', 'failed')
SPARK = 'gpt-5.3-codex-spark'


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0


def _usage(item):
    usage = item.get('usage') if isinstance(item, dict) else None
    if not isinstance(usage, dict) and isinstance(item, dict):
        usage = (item.get('codex') or {}).get('usage')
    return usage if isinstance(usage, dict) else None


def _attempts(report, state):
    source = report.get('attempts') if isinstance(report, dict) else None
    if not isinstance(source, list):
        source = state.get('attempts_detail') if isinstance(state, dict) else None
    source = source if isinstance(source, list) else []
    decisions = state.get('decisions') if isinstance(state, dict) else None
    result = []
    for index, detail in enumerate(source):
        item = dict(detail) if isinstance(detail, dict) else {}
        decision = decisions[index] if isinstance(decisions, list) and index < len(decisions) else {}
        if not isinstance(decision, dict):
            decision = {}
        for key in ('model', 'reasoning', 'kind', 'reason'):
            if item.get(key) is None and decision.get(key) is not None:
                item[key] = decision[key]
        result.append(item)
    rescue_turns = (report.get('rescue_turns') if isinstance(report, dict) else None)
    if not isinstance(rescue_turns, list):
        rescue_turns = state.get('rescue_turns') if isinstance(state, dict) else None
    for row in rescue_turns if isinstance(rescue_turns, list) else []:
        if not isinstance(row, dict):
            continue
        decision = row.get('decision') if isinstance(row.get('decision'), dict) else {}
        result.append({'role': 'rescue', 'model': decision.get('model'),
                       'reasoning': decision.get('reasoning'), 'reason': decision.get('reason'),
                       'codex': row.get('codex')})
    return result


def _roots(project_root, roots):
    values = roots or [Path(project_root)]
    result = []
    for value in values:
        path = Path(value).expanduser().resolve()
        if path.name == 'runs':
            result.append(path)
        elif (path / '.ai-dev' / 'runs').is_dir() or path.name == '.ai-dev':
            result.append(path / 'runs' if path.name == '.ai-dev' else path / '.ai-dev' / 'runs')
        else:
            result.append(path)
    return result


def _report_files(project_root, roots):
    seen = set()
    for base in _roots(project_root, roots):
        if base.is_file() and base.name == 'report.json':
            candidates = [base]
        elif base.is_dir():
            candidates = base.rglob('report.json')
        else:
            candidates = []
        for path in candidates:
            path = path.resolve()
            if path not in seen and path.name == 'report.json':
                seen.add(path)
                yield path


def _review_imports(project_root):
    """Read only the explicit advisory artifact directory, never arbitrary .ai-dev files."""
    directory = Path(project_root) / '.ai-dev' / 'reviews'
    if directory.is_symlink() or not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and not path.is_symlink() and path.suffix == '.json':
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(value, dict) and value.get('artifact_type') == 'untrusted_chatgpt_advisory':
                    result.append(value)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    return result


def _status(value):
    value = str(value or '').upper()
    if value == 'COMPLETED':
        return 'completed'
    if value == 'BLOCKED':
        return 'blocked'
    if value in ('FAILED', 'ERROR'):
        return 'failed'
    return UNKNOWN


def _reason_kind(reason):
    text = str(reason or '').lower()
    return (any(word in text for word in ('fallback', 'запасн', 'подмен')),
            any(word in text for word in ('escalat', 'эскалац', 'повышение после')))


def _blank_bucket(**fields):
    return dict(fields, tasks=0, attempts=0, retry_tasks=0, retry_attempts=0,
                completed=0, blocked=0, failed=0, unknown_status=0,
                routing_reasons={}, fallback_attempts=0, escalation_attempts=0,
                **{field: 0 for field in TOKEN_FIELDS})


def _add_reason(bucket, reason):
    reason = str(reason) if reason else UNKNOWN
    bucket['routing_reasons'][reason] = bucket['routing_reasons'].get(reason, 0) + 1


def aggregate(project_root, roots=None):
    """Aggregate local report/state files without network or model calls."""
    groups = {}
    total = _blank_bucket(guard_blocks=0, missing_usage_tasks=0, missing_usage_attempts=0,
                          compactions=0, compaction_evidence=[])
    errors = []
    task_ids = set()
    files = []
    routing_events = {name: {'tasks': 0, 'attempts': 0, 'reasons': {}}
                      for name in ('fallback', 'escalation')}
    spark = {'selected_tasks': 0, 'skipped_tasks': 0, 'skipped_reasons': {}}
    review_imports = _review_imports(project_root)
    advisory = {'eligible': 0, 'skipped': 0, 'levels': {'normal': 0, 'deep': 0},
                'reasons': {}, 'cache': {'exact': 0, 'negative': 0, 'stale': 0, 'miss': 0},
                'non_blocking': True, 'resume_models': ['Luna', 'Spark'],
                'codex_only_models': ['Terra', 'Sol', 'Astra']}
    guard_summary = {'tier_limits': {}, 'max_concurrent': [], 'occupied_slots': {},
                     'blocked_slots': {}, 'block_reasons': {}}

    for report_path in _report_files(project_root, roots):
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if not isinstance(report, dict):
                raise ValueError('report is not an object')
            state_path = report_path.parent / 'state.json'
            state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            if not isinstance(state, dict):
                state = {}
            compaction = state.get('compaction')
            if isinstance(compaction, dict):
                total['compactions'] += 1
                total['compaction_evidence'].append({key: compaction.get(key) for key in
                    ('old_context_bytes', 'estimated_tokens', 'compaction_reason',
                     'summary_bytes', 'new_session_id', 'usage_report_evidence')})
            task_id = report.get('id') or report.get('task_id') or state.get('id') or str(report_path)
            if task_id in task_ids:
                continue
            task_ids.add(task_id)
            files.append(str(report_path))
            attempts = _attempts(report, state)
            status = _status(report.get('status') or state.get('status'))
            policy = report.get('chatgpt_advisory') or state.get('chatgpt_advisory') or {}
            if isinstance(policy, dict):
                if policy.get('enabled'):
                    advisory['eligible'] += 1
                    level = policy.get('level')
                    if level in advisory['levels']:
                        advisory['levels'][level] += 1
                elif policy:
                    advisory['skipped'] += 1
                reason = policy.get('reason')
                if reason:
                    advisory['reasons'][str(reason)] = advisory['reasons'].get(str(reason), 0) + 1
                cache = policy.get('cache_status')
                if cache in advisory['cache']:
                    advisory['cache'][cache] += 1
            total['tasks'] += 1
            total[status if status in STATUS_NAMES else 'unknown_status'] += 1
            default_model = report.get('model') or state.get('model')
            default_reasoning = report.get('reasoning') or state.get('reasoning')
            default_kind = report.get('kind') or (state.get('routing') or {}).get('kind') or 'auto'
            task_keys = set()
            task_events = {'fallback': False, 'escalation': False}
            has_spark = False
            task_missing = False
            for index, raw_attempt in enumerate(attempts):
                attempt = raw_attempt if isinstance(raw_attempt, dict) else {}
                model = attempt.get('model') or default_model or UNKNOWN
                reasoning = attempt.get('reasoning') or default_reasoning or UNKNOWN
                kind = attempt.get('kind') or default_kind or UNKNOWN
                tier, _ = classify(model, reasoning)
                tier = tier or UNKNOWN
                key = (str(model), str(reasoning), str(kind), tier)
                group = groups.setdefault(key, _blank_bucket(model=key[0], reasoning=key[1], kind=key[2], tier=key[3]))
                group['attempts'] += 1
                total['attempts'] += 1
                if index and attempt.get('role') != 'planner':
                    group['retry_attempts'] += 1
                    total['retry_attempts'] += 1
                task_keys.add(key)
                reason = attempt.get('reason')
                _add_reason(group, reason)
                _add_reason(total, reason)
                fallback, escalation = _reason_kind(reason)
                for name, detected in (('fallback', fallback), ('escalation', escalation)):
                    if detected:
                        group[name + '_attempts'] += 1
                        total[name + '_attempts'] += 1
                        task_events[name] = True
                        routing_events[name]['attempts'] += 1
                        label = str(reason)
                        routing_events[name]['reasons'][label] = routing_events[name]['reasons'].get(label, 0) + 1
                if str(model).lower() == SPARK:
                    has_spark = True
                usage = _usage(attempt)
                if usage is None:
                    task_missing = True
                    group['missing_usage_attempts'] = group.get('missing_usage_attempts', 0) + 1
                    total['missing_usage_attempts'] += 1
                    continue
                for field in TOKEN_FIELDS:
                    value = _number(usage.get(field))
                    group[field] += value
                    total[field] += value
            for key in task_keys:
                group = groups[key]
                group['tasks'] += 1
                group[status if status in STATUS_NAMES else 'unknown_status'] += 1
                if len(attempts) > 1:
                    group['retry_tasks'] += 1
            if len(attempts) > 1:
                total['retry_tasks'] += 1
            if task_missing:
                total['missing_usage_tasks'] += 1
            for name in ('fallback', 'escalation'):
                if task_events[name]:
                    routing_events[name]['tasks'] += 1
            if has_spark:
                spark['selected_tasks'] += 1
            else:
                spark['skipped_tasks'] += 1
                explicit = [str(item.get('reason')) for item in attempts
                            if isinstance(item, dict) and item.get('reason') and
                            ('spark' in str(item.get('reason')).lower() or 'спарк' in str(item.get('reason')).lower())]
                why = explicit[0] if explicit else UNKNOWN
                spark['skipped_reasons'][why] = spark['skipped_reasons'].get(why, 0) + 1
            guard = report.get('usage_guard') or state.get('guard') or {}
            if isinstance(guard, dict):
                for tier, maximum in (guard.get('tier_limits') or {}).items():
                    if isinstance(maximum, int):
                        guard_summary['tier_limits'][tier] = maximum
                if isinstance(guard.get('max_concurrent'), int):
                    guard_summary['max_concurrent'].append(guard['max_concurrent'])
                for name in ('occupied_slots', 'blocked_slots'):
                    for tier, count in (guard.get(name) or {}).items():
                        if isinstance(count, int):
                            guard_summary[name][tier] = max(guard_summary[name].get(tier, 0), count)
                if guard.get('acquired') is False and guard.get('reason'):
                    total['guard_blocks'] += 1
                    reason = str(guard['reason'])
                    guard_summary['block_reasons'][reason] = guard_summary['block_reasons'].get(reason, 0) + 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append({'path': str(report_path), 'error': str(exc)})

    recorded = sum(total[field] for field in TOKEN_FIELDS)
    total['selected_models'] = sorted({key[0] for key in groups})
    total['selected_reasoning'] = sorted({key[1] for key in groups})
    total['selected_kinds'] = sorted({key[2] for key in groups})

    def finish(item):
        item['recorded_tokens'] = sum(item[field] for field in TOKEN_FIELDS)
        item['percent_of_recorded_tokens'] = item['recorded_tokens'] * 100.0 / recorded if recorded else None
        item['retry'] = {'tasks': item['retry_tasks'], 'attempts': item['retry_attempts']}
        item['status_counts'] = {name: item[name] for name in STATUS_NAMES}
        item['missing_usage'] = {'tasks': item.get('missing_usage_tasks', 0),
                                 'attempts': item.get('missing_usage_attempts', 0)}
        return item

    hints = []
    if total['retry_attempts']:
        hints.append('retry: %d повторных попыток в %d задачах.' % (total['retry_attempts'], total['retry_tasks']))
    if total['missing_usage_attempts']:
        hints.append('usage отсутствует: %d попыток в %d задачах; причина: unknown, если её нет в state.json.' %
                     (total['missing_usage_attempts'], total['missing_usage_tasks']))
    expensive = sum(1 for key in groups if 'astra' in key[0].lower() or 'expensive' in key[0].lower())
    if expensive:
        hints.append('Выбрана дорогая модель в %d группе(ах); стоимость локальными отчётами не измеряется.' % expensive)
    if spark['skipped_tasks']:
        hints.append('Spark пропущен в %d задачах; причина: %s.' %
                     (spark['skipped_tasks'], ', '.join(sorted(spark['skipped_reasons'])) or UNKNOWN))
    if guard_summary['block_reasons']:
        hints.append('Guard заблокировал %d запуск(а) до model turn: %s; usage для них не расходуется и resume безопасен после освобождения слота.' %
                     (total['guard_blocks'], ', '.join(sorted(guard_summary['block_reasons']))))
    if routing_events['fallback']['attempts']:
        hints.append('Использован fallback в %d попытках; проверьте доступность предпочтительной модели.' %
                     routing_events['fallback']['attempts'])
    if review_imports:
        accepted = sum(item.get('status') == 'ACCEPTED_AS_HINT' for item in review_imports)
        stale = sum(item.get('status') == 'STALE' for item in review_imports)
        for item in review_imports:
            cache_status = (item.get('cache') or {}).get('status')
            if cache_status == 'exact':
                advisory['cache']['exact'] += 1
            elif cache_status == 'stale':
                advisory['cache']['stale'] += 1
        hints.append('Импортировано advisory: %d; accepted hints: %d, stale: %d; advisory не меняет usage и требует проверки Codex.' %
                     (len(review_imports), accepted, stale))
    if advisory['eligible']:
        hints.append('ChatGPT advisory выбран для %d задач (normal %d, deep %d); он неблокирующий, после ответа продолжайте Luna/Spark, а Terra/Sol/Astra остаются Codex-only.' %
                     (advisory['eligible'], advisory['levels']['normal'], advisory['levels']['deep']))
    if advisory['cache']['stale']:
        hints.append('ChatGPT advisory cache: %d stale записей не используются; task/repo hash должен точно совпасть.' %
                     advisory['cache']['stale'])
    if not hints:
        hints.append('Недостаточно фактов для improvement hints: unknown.')
    return {
        'source': 'recorded local usage',
        'disclaimer': 'Проценты рассчитаны только от суммы записанных локально токенов; это не процент общего лимита Codex.',
        'roots': [str(path) for path in _roots(project_root, roots)],
        'files': files,
        'groups': [finish(groups[key]) for key in sorted(groups)],
        'totals': finish(total),
        'missing_usage': {'tasks': total['missing_usage_tasks'], 'attempts': total['missing_usage_attempts']},
        'unknown_model': {'tasks': sum(group['tasks'] for key, group in groups.items() if key[0] == UNKNOWN),
                          'attempts': sum(group['attempts'] for key, group in groups.items() if key[0] == UNKNOWN)},
        'routing_events': routing_events,
        'guard_blocks': total['guard_blocks'],
        'guard': {**guard_summary,
                  'max_concurrent': max(guard_summary['max_concurrent']) if guard_summary['max_concurrent'] else None},
        'spark': spark,
        'review_imports': {'count': len(review_imports),
                           'accepted_as_hint': sum(item.get('status') == 'ACCEPTED_AS_HINT' for item in review_imports),
                           'stale': sum(item.get('status') == 'STALE' for item in review_imports)},
        'lessons_sync': sync_summary(project_root),
        'chatgpt_advisory': advisory,
        'chatgpt_limits': chatgpt_limit_report(project_root),
        'context_compaction': {'count': total['compactions'],
                               'evidence': total['compaction_evidence']},
        'improvement_hints': hints,
        'account_level_breakdown': {'available': False, 'reason': 'Историческая account-level разбивка в локальных отчётах отсутствует.'},
        'errors': errors,
    }
