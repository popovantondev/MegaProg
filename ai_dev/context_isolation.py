"""Temporarily remove generated workspace context during a model turn.

The supervisor still keeps the small state/config files in ``.ai-dev``.  Large
and generated trees are moved, rather than copied or deleted, to a temporary
directory on the same filesystem (local temp on Windows when possible). This keeps the operation cheap
and makes ``os.replace`` a crash-safe primitive.
"""

import hashlib
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


# Names are deliberately an allow-list: an unexpected directory is never
# hidden merely because it happens to be large.
HIDDEN_RELATIVE_PATHS = (
    Path('.ai-dev/schema'), Path('.ai-dev/runs'), Path('.ai-dev/logs'),
    Path('.ai-dev/release-archives'), Path('.ai-dev/release_archives'),
    Path('.ai-dev/releases'), Path('.ai-dev/archives'), Path('.ai-dev/previews'),
    Path('.ai-dev/cache'), Path('.ai-dev/caches'), Path('generated'),
    Path('artifacts'), Path('previews'), Path('logs'), Path('cache'),
    Path('caches'), Path('release-archives'), Path('releases'), Path('archives'),
)


def _lock_path(root):
    digest = hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:24]
    return root.parent / ('.ai-dev-context-lock-' + digest)


def _acquire(path):
    handle = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError('Другой context isolation уже работает в этом проекте.')
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('Другой context isolation уже работает в этом проекте.')
        return handle
    except Exception:
        handle.close()
        raise


def _release(handle):
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _same_path(path, other):
    try:
        return path.resolve() == other.resolve()
    except OSError:
        return path.absolute() == other.absolute()


def _staging_parent(root):
    """Use local Windows temp only when atomic directory moves stay on one volume."""
    if os.name == 'nt':
        local = Path(tempfile.gettempdir()).resolve()
        try:
            if os.stat(root).st_dev == os.stat(local).st_dev:
                return local
        except OSError:
            pass
    return root.parent


@contextmanager
def isolate_generated_context(root, required_paths=()):
    """Hide generated paths for one model turn and always restore them.

    ``required_paths`` is an explicit escape hatch for a task that genuinely
    needs one generated path.  Paths are relative to ``root`` and cannot
    escape it.  The yielded object contains ``relocate(path)`` for turn logs
    that must remain writable while the original tree is hidden.
    """
    root = Path(root).resolve()
    required = set()
    for value in required_paths or ():
        path = Path(value)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('required context path must be relative to the project root')
        required.add(path)

    lock_handle = _acquire(_lock_path(root))
    staging = Path(tempfile.mkdtemp(prefix='.ai-dev-context-', dir=str(_staging_parent(root))))
    moved = []
    metrics = {'hidden_paths': [], 'restored_paths': [], 'conflicts': [],
               'staging_parent': str(staging.parent)}

    def relocate(path):
        path = Path(path)
        try:
            relative = path.resolve().relative_to(root)
        except ValueError:
            raise ValueError('context path must be inside the project root')
        for original, staged in moved:
            if _same_path(path, original) or _same_path(path, original.parent):
                return staged if _same_path(path, original) else staged / path.relative_to(original.parent)
        return staging / relative

    try:
        for relative in HIDDEN_RELATIVE_PATHS:
            # Keep a generated container visible when the task explicitly
            # requires anything below it.
            if any(relative == item or relative in item.parents for item in required):
                continue
            original = root / relative
            if not original.exists() and not original.is_symlink():
                continue
            staged = staging / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(original), str(staged))
            moved.append((original, staged))
            metrics['hidden_paths'].append(str(relative))
        yield type('Isolation', (), {
            'metrics': metrics,
            'relocate': staticmethod(relocate),
        })
    finally:
        restore_error = None
        for original, staged in reversed(moved):
            try:
                original.parent.mkdir(parents=True, exist_ok=True)
                if original.exists() or original.is_symlink():
                    conflict = original.with_name(original.name + '.during-turn-' + uuid.uuid4().hex)
                    os.replace(str(original), str(conflict))
                    metrics['conflicts'].append(str(conflict.relative_to(root)))
                os.replace(str(staged), str(original))
                metrics['restored_paths'].append(str(original.relative_to(root)))
            except Exception as exc:  # preserve the first failure but attempt all paths
                restore_error = restore_error or exc
        try:
            shutil.rmtree(str(staging))
        except FileNotFoundError:
            pass
        except Exception as exc:
            restore_error = restore_error or exc
        _release(lock_handle)
        if restore_error:
            raise restore_error
