"""Explicit project-local concurrency configuration; no model calls."""
from contextlib import nullcontext
from .account_guard import settings
from .storage import lock, read, write


def configure(root, maximum=None, wait_seconds=None):
    if maximum is not None and not 1 <= maximum <= 100:
        raise ValueError('max-concurrent: 1..100')
    if wait_seconds is not None and not 0 <= wait_seconds <= 86400:
        raise ValueError('wait-seconds: 0..86400')
    changed = maximum is not None or wait_seconds is not None
    with lock(root) if changed else nullcontext():
        path = root / '.ai-dev/config.json'
        config = read(path)
        if changed:
            state_path = root / '.ai-dev/state.json'
            state = read(state_path) if state_path.exists() else {}
            if state.get('status') in ('RUNNING', 'VERIFY', 'WAITING_FOR_SLOT', 'PREFLIGHT', 'RETRY'):
                raise ValueError('Сначала остановите задачу через Ctrl+C; затем измените локальную параллельность.')
            guard = config.setdefault('account_guard', {})
            if maximum is not None:
                guard['max_concurrent'] = maximum
            if wait_seconds is not None:
                guard['wait_seconds'] = wait_seconds
            write(path, config)
            # Only local scheduling parameters change; model/verification remain frozen.
            if state.get('status') in ('BLOCKED', 'PENDING_APPROVAL'):
                state.setdefault('config', {})['account_guard'] = dict(guard)
                write(state_path, state)
                write(root / '.ai-dev/runs' / state['id'] / 'state.json', state)
        result = settings(config)
        result.update(project=str(root), wait_seconds=config.get('account_guard', {}).get('wait_seconds', 300),
                      source='local MegaProg policy, not OpenAI quota', changed=changed)
        return result
