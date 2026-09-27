"""Normalize telemetry returned by Codex without inferring unavailable facts."""

UNKNOWN = 'UNKNOWN'
USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens')


def _reported_value(events, names):
    """Read an explicit runtime field from a Codex turn event, if present."""
    for event in reversed(events):
        if not isinstance(event, dict) or event.get('type') not in ('turn.started', 'turn.completed'):
            continue
        for name in names:
            value = event.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return UNKNOWN


def runtime_model(events):
    return _reported_value(events, ('model', 'model_name', 'model_slug'))


def runtime_reasoning(events):
    return _reported_value(events, ('reasoning_effort', 'reasoning'))


def usage_snapshot(value):
    """Preserve only provider-reported numeric token counters; missing stays null."""
    value = value if isinstance(value, dict) else {}
    result = {}
    for field in USAGE_FIELDS:
        number = value.get(field)
        result[field] = number if type(number) is int and number >= 0 else None
    return result


def make_turn(*, configured_model, configured_reasoning, observed_model=UNKNOWN,
              observed_reasoning=UNKNOWN, usage=None, duration_seconds=None,
              session_id=None, result_status='UNKNOWN'):
    return {
        'schema_version': 1,
        'task_id': UNKNOWN,
        'stage_id': UNKNOWN,
        'role': UNKNOWN,
        'configured_model': configured_model or UNKNOWN,
        'configured_reasoning': configured_reasoning or UNKNOWN,
        'observed_model': observed_model or UNKNOWN,
        'observed_reasoning': observed_reasoning or UNKNOWN,
        'usage': usage_snapshot(usage),
        'duration_seconds': duration_seconds,
        'retry_number': None,
        'attempt_number': None,
        'session_id': session_id or UNKNOWN,
        'escalation_reason': UNKNOWN,
        'result': result_status,
    }


def attach_context(result, *, task_id, stage_id, role, retry_number,
                   attempt_number, escalation_reason, configured_model=None,
                   configured_reasoning=None):
    """Enrich an existing Codex result with supervisor-owned identities."""
    result = result if isinstance(result, dict) else {}
    record = result.get('telemetry')
    if not isinstance(record, dict):
        record = make_turn(
            configured_model=configured_model,
            configured_reasoning=configured_reasoning,
            usage=result.get('usage'), session_id=result.get('session_id'),
            result_status='COMPLETED' if result.get('ok') else 'FAILED')
        result['telemetry'] = record
    record.update({
        'task_id': task_id or UNKNOWN,
        'stage_id': stage_id or UNKNOWN,
        'role': role or UNKNOWN,
        'retry_number': retry_number if type(retry_number) is int else None,
        'attempt_number': attempt_number if type(attempt_number) is int else None,
        'session_id': result.get('session_id') or record.get('session_id') or UNKNOWN,
        'escalation_reason': escalation_reason or UNKNOWN,
        'result': 'COMPLETED' if result.get('ok') else 'FAILED',
    })
    return record
