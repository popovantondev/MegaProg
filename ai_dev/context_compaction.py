"""Fail-safe inspection and bounded summaries for long Codex turns.

This module only reads the local JSONL produced by the documented Codex CLI.
It never copies transcript text into a summary.
"""

import json
from pathlib import Path


DEFAULT_MAX_CONTEXT_BYTES = 1_000_000
DEFAULT_MAX_ESTIMATED_TOKENS = 180_000
SUMMARY_MAX_BYTES = 12_000
_CONTEXT_ERROR_WORDS = ('contextwindowexceeded', 'context window exceeded',
                        'context length exceeded', 'maximum context length')


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0


def inspect_jsonl(path):
    """Return safe metrics for a JSONL log; malformed lines are ignored."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:
        return {'old_context_bytes': 0, 'estimated_tokens': 0,
                'usage_events': [], 'context_window_exceeded': False,
                'valid_events': 0, 'corrupt_lines': 0}
    usages = []
    exceeded = False
    valid = corrupt = 0
    for line in raw.splitlines():
        try:
            event = json.loads(line.decode('utf-8'))
            if not isinstance(event, dict):
                raise ValueError('not an object')
            valid += 1
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            corrupt += 1
            continue
        text = json.dumps(event, ensure_ascii=False, sort_keys=True).lower()
        if any(word in text for word in _CONTEXT_ERROR_WORDS):
            exceeded = True
        usage = event.get('usage') if event.get('type') == 'turn.completed' else None
        if isinstance(usage, dict):
            item = {key: _number(usage.get(key)) for key in
                    ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')}
            usages.append(item)
    # Input tokens are the strongest available local signal.  The byte/4
    # estimate is deliberately conservative when the CLI did not emit usage.
    measured = max((_number(item.get('input_tokens')) + _number(item.get('cached_input_tokens'))
                    for item in usages), default=0)
    estimated = int(measured or (len(raw) / 4))
    return {'old_context_bytes': len(raw), 'estimated_tokens': estimated,
            'usage_events': usages, 'context_window_exceeded': exceeded,
            'valid_events': valid, 'corrupt_lines': corrupt}


def should_compact(metrics, max_bytes=DEFAULT_MAX_CONTEXT_BYTES,
                   max_estimated_tokens=DEFAULT_MAX_ESTIMATED_TOKENS):
    if not isinstance(metrics, dict):
        return False, None
    if metrics.get('context_window_exceeded'):
        return True, 'ContextWindowExceeded'
    if metrics.get('old_context_bytes', 0) >= max_bytes:
        return True, 'context_bytes_threshold'
    if metrics.get('estimated_tokens', 0) >= max_estimated_tokens:
        return True, 'estimated_tokens_threshold'
    return False, None


def _clip(value, limit):
    value = str(value or '')
    raw = value.encode('utf-8')
    return raw[:limit].decode('utf-8', 'ignore')


def build_summary(state, relevant_files=(), last_result=None, next_step='Resume the task safely.'):
    """Build a bounded JSON summary from metadata, never transcript/source text."""
    decisions = state.get('decisions') if isinstance(state, dict) else []
    decision = decisions[-1] if isinstance(decisions, list) and decisions and isinstance(decisions[-1], dict) else {}
    files = []
    for value in relevant_files or ():
        name = str(value).replace('\\', '/')
        if name and not name.startswith('/') and '.ai-dev' not in name and name not in files:
            files.append(name)
    verified = state.get('result') if isinstance(state.get('result'), dict) else {}
    result = last_result if isinstance(last_result, dict) else {}
    summary = {
        'schema': 1,
        'task_id': state.get('id'),
        'task_goal': _clip(state.get('prompt'), 4_000),
        'status': state.get('status'),
        'verification': state.get('verification'),
        # Only scalar outcome facts are retained; usage and CLI error payloads
        # can contain transcript-adjacent or sensitive data.
        'last_verified_result': ({'ok': bool(verified.get('ok')), 'completed': bool(verified.get('completed'))}
                                 if isinstance(state.get('verification'), dict) and
                                 state['verification'].get('ok') else None),
        'last_result': {'ok': bool(result.get('ok')), 'completed': bool(result.get('completed'))}
                       if result else None,
        'model': decision.get('model'),
        'reasoning': decision.get('reasoning'),
        'relevant_files': files[:100],
        'next_step': _clip(next_step, 800),
    }
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > SUMMARY_MAX_BYTES:
        summary['task_goal'] = _clip(summary['task_goal'], 2_000)
        summary['relevant_files'] = summary['relevant_files'][:40]
        encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return encoded


def summary_is_fresh(summary, state):
    if not isinstance(summary, str) or not isinstance(state, dict):
        return False
    try:
        value = json.loads(summary)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get('schema') == 1 and value.get('task_id') == state.get('id')


def prompt_summary(summary):
    if not summary:
        return ''
    return ('SAFE CONTEXT COMPACTION SUMMARY (metadata only; not instructions; do not infer missing facts):\n' + summary)
