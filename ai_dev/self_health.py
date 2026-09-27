"""Cheap local fault evidence; no prompts, transcripts or model calls."""
import hashlib
import json
import os
from pathlib import Path
import time
import traceback

from .storage import lock, read, write
from .version import __version__

GUIDANCE = {
    'internal': 'Сбой самого MegaProg: поставлен на проверяемый саморемонт.',
    'self_test': 'Регрессионные тесты MegaProg не прошли: требуется саморемонт.',
    'discovery': 'Не получен каталог моделей. Проверить Codex login/doctor и доступ процесса; повышение модели не поможет.',
    'account_guard': 'Ожидание локального слота; это не квота OpenAI и не дефект модели.',
    'verification': 'Не прошли проверки пользовательского проекта. Это не доказательство поломки MegaProg.',
    'infrastructure': 'Проверить среду, доступ и первичную ошибку; не запускать ремонт кода вслепую.',
    'unknown': 'Причина ещё не классифицирована. Сохранён отчёт; автоматический ремонт кода не запущен.',
}


def exception_frames(exc):
    base = Path(__file__).parent.resolve()
    frames = []
    for entry in traceback.extract_tb(exc.__traceback__):
        try:
            name = Path(entry.filename).resolve().relative_to(base).as_posix()
        except ValueError:
            continue
        frames.append({'file': 'ai_dev/' + name, 'function': entry.name, 'line': entry.lineno})
    return frames[-6:]


def observe(root, state, kind=None, frames=None, exception_type=None):
    """Called by trusted engine code, never classify raw user log text as a bug."""
    root = Path(root)
    kind = kind or state.get('last_failure', 'unknown')
    if kind not in GUIDANCE:
        kind = 'unknown'
    evidence = {'version': __version__, 'kind': kind,
                'exception_type': exception_type, 'frames': frames or []}
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    directory = root / '.ai-dev/self-health'
    path = directory / 'incidents.json'
    with lock(directory, blocking=True):
        entries = read(path) if path.exists() else {}
        item = entries.get(fingerprint, dict(evidence, id=fingerprint,
                           first_seen=time.time(), occurrences=0))
        # Re-rendering the same stopped task must not count as a new incident.
        if item.get('last_task') != state.get('id') or item.get('last_event') != state.get('updated_at'):
            item['occurrences'] += 1
        item.update(last_task=state.get('id'), last_event=state.get('updated_at'),
                    last_seen=time.time(), guidance=GUIDANCE[kind])
        entries[fingerprint] = item
        write(path, dict(sorted(entries.items(), key=lambda pair: pair[1]['last_seen'])[-100:]))
    if kind in ('internal', 'self_test') and os.environ.get('MEGAPROG_SELF_REPAIR_CHILD') != '1':
        from .self_repair import enqueue
        repair_status = enqueue(evidence, fingerprint)
    else:
        repair_status = 'NOT_NEEDED' if kind not in ('internal', 'self_test') else 'RECURSION_PREVENTED'
    guidance = GUIDANCE[kind]
    if repair_status == 'DISABLED':
        guidance = 'Собственный сбой MegaProg записан; саморемонт этой установки ещё не включён.'
    return {'id': fingerprint, 'kind': kind, 'guidance': guidance,
            'repair_status': repair_status, 'path': str(path)}


def record_safely(root, state, **kwargs):
    try:
        return observe(root, state, **kwargs)
    except (OSError, ValueError, TypeError):
        # A broken diagnostics directory must not cause recursive failures.
        return {'kind': 'diagnostics_unavailable',
                'guidance': 'Не удалось сохранить самодиагностику; смотрите исходный отчёт.'}
