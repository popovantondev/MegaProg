"""Crash-safe, cross-project limits for concurrent model turns.

Each turn holds an atomically-created global slot and a tier slot. The global
slot preserves legacy ``max_concurrent``; tier slots are a local safety guard,
not a statement about an account quota.
"""

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DEFAULT_MAX_CONCURRENT = 49
DEFAULT_STALE_SECONDS = 3600
DEFAULT_TIER_LIMITS = {'cheap': 30, 'terra': 12, 'sol': 4, 'expensive': 3}
LEGACY_DEFAULT_MAX_CONCURRENT = 11
LEGACY_DEFAULT_TIER_LIMITS = {'cheap': 5, 'terra': 3, 'sol': 2, 'expensive': 1}
TIER_NAMES = tuple(DEFAULT_TIER_LIMITS)
MAX_OWNER_TEXT = 120
MAX_OWNER_RECORDS = 49


class AccountGuardError(RuntimeError):
    """No safe local slot is available (or the selected pair is unknown)."""
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def _safe_owner_text(value, fallback='unknown'):
    """Return one bounded terminal-safe field from an untrusted slot file."""
    if not isinstance(value, str):
        return fallback
    value = ''.join(char if char.isprintable() and char not in '\r\n' else ' '
                    for char in value).strip()
    return value[:MAX_OWNER_TEXT] if value else fallback


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        # os.kill(pid, 0) can terminate a process on Windows.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False
    return True


def _lock(handle, blocking):
    # Windows byte-range locks deny reads of the locked byte. Keep the JSON
    # header readable by other projects while its owner holds the slot.
    handle.seek(8192 if os.name == 'nt' else 0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    handle.seek(8192 if os.name == 'nt' else 0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def directory(config):
    configured = (config.get('account_guard') or {}).get('directory')
    return Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / 'ai-dev-account-guard'


def normalize_legacy_defaults(config):
    """Return a copy with only the unambiguous former defaults upgraded.

    A standalone old number may have been deliberately chosen by a project,
    so it is not reinterpreted.  The complete former default set, however,
    was emitted by released config templates and can be migrated safely.  The
    function is idempotent and leaves every unrelated setting untouched.
    """
    result = dict(config)
    guard = config.get('account_guard')
    if not isinstance(guard, dict):
        return result
    copied_guard = dict(guard)
    legacy_limits = copied_guard.get('tier_limits')
    if (copied_guard.get('max_concurrent') == LEGACY_DEFAULT_MAX_CONCURRENT and
            legacy_limits == LEGACY_DEFAULT_TIER_LIMITS):
        copied_guard['max_concurrent'] = DEFAULT_MAX_CONCURRENT
        copied_guard['tier_limits'] = dict(DEFAULT_TIER_LIMITS)
    result['account_guard'] = copied_guard
    return result


def settings(config):
    config = normalize_legacy_defaults(config)
    value = config.get('account_guard') or {}
    if value.get('enabled', True) is False:
        return {'enabled': False, 'max_concurrent': 0, 'stale_seconds': DEFAULT_STALE_SECONDS,
                'tier_limits': dict(DEFAULT_TIER_LIMITS)}
    limits = dict(DEFAULT_TIER_LIMITS)
    limits.update(value.get('tier_limits') or {})
    return {'enabled': True, 'max_concurrent': value.get('max_concurrent', DEFAULT_MAX_CONCURRENT),
            'stale_seconds': value.get('stale_seconds', DEFAULT_STALE_SECONDS), 'tier_limits': limits}


def classify(model, reasoning):
    """Classify only an explicit known model/reasoning pair.

    GPT-6 Sol takes the former Terra implementation tier at medium/high;
    Sol low keeps its smaller focused-work tier. Astra has its own strong tier.
    """
    if not isinstance(model, str) or not model or not isinstance(reasoning, str) or not reasoning:
        return None, 'model_or_reasoning_missing'
    model, reasoning = model.lower(), reasoning.lower()
    if model == 'gpt-6-luna' and reasoning in ('low', 'medium', 'high'):
        return 'cheap', None
    if model == 'gpt-6-sol' and reasoning in ('medium', 'high'):
        return 'terra', None
    if model == 'gpt-6-sol' and reasoning == 'low':
        return 'sol', None
    if model == 'gpt-6-astra' and reasoning in ('low', 'medium'):
        return 'expensive', None
    return None, 'unknown_model_or_reasoning'


class AccountGuard:
    def __init__(self, config, model=None, reasoning=None, owner=None):
        self.config, self.options = config, settings(config)
        self.root, self.token = directory(config), uuid.uuid4().hex
        self.model, self.reasoning = model, reasoning
        owner = owner if isinstance(owner, dict) else {}
        # These names are deliberately supplied by the supervisor, rather than
        # inferred from a prompt or a current directory in a shared temp area.
        self.owner = {'project': _safe_owner_text(owner.get('project')),
                      'task_id': _safe_owner_text(owner.get('task_id'))}
        self.tier, self.classification_error = classify(model, reasoning)
        self.paths, self.handles = [], []
        self.evidence = {'enabled': self.options['enabled'], 'max_concurrent': self.options['max_concurrent'],
                         'tier_limits': dict(self.options['tier_limits']), 'model': model,
                         'reasoning': reasoning, 'tier': self.tier, 'acquired': False}

    def _cleanup_stale(self):
        now = time.time()
        for path in self.root.glob('*.json'):
            if not (path.name.startswith('slot-') or path.name.startswith('global-slot-') or path.name.startswith('tier-')):
                continue
            handle = None
            try:
                handle = path.open('r+b')
                _lock(handle, blocking=False)
                handle.seek(0)
                data = json.load(handle)
                reclaim = not _pid_alive(data.get('pid'))
                _unlock(handle)
                handle.close()
                if reclaim:
                    path.unlink()
            except (OSError, BlockingIOError):
                if handle:
                    handle.close()
            except (ValueError, TypeError, KeyError):
                if handle:
                    try:
                        _unlock(handle)
                        handle.close()
                    except OSError:
                        pass
                try:
                    if now - path.stat().st_mtime > self.options['stale_seconds']:
                        path.unlink()
                except OSError:
                    pass

    def _occupied(self):
        result = {'global': 0, **{tier: 0 for tier in TIER_NAMES}}
        for path in self.root.glob('*.json'):
            name = path.name
            if name.startswith('global-slot-') or name.startswith('slot-'):
                result['global'] += 1
            for tier in TIER_NAMES:
                if name.startswith('tier-%s-slot-' % tier):
                    result[tier] += 1
        return result

    def _owners(self, prefix=None):
        """Read compact owner evidence without exposing slot paths or content.

        A model turn owns two files.  Their shared random token makes the
        global and tier records one displayed owner, while old/corrupt records
        stay visible as explicitly unknown rather than silently disappearing.
        """
        owners, seen = [], set()
        if not self.root.exists():
            return owners
        for path in sorted(self.root.glob('*.json')):
            name = path.name
            if prefix:
                # ``slot-*`` was the released pre-tier global naming scheme.
                # It still consumes a global slot, so do not hide its owner
                # evidence merely because it lacks current metadata.
                if prefix == 'global':
                    if not (name.startswith('global-slot-') or name.startswith('slot-')):
                        continue
                elif not name.startswith(prefix + '-slot-'):
                    continue
            if not prefix and not (name.startswith('global-slot-') or name.startswith('slot-')):
                continue
            data = {}
            try:
                if path.stat().st_size > 8192:
                    raise ValueError('oversized')
                with path.open('r', encoding='utf-8') as handle:
                    data = json.load(handle)
                if not isinstance(data, dict):
                    raise ValueError('not an object')
            except (OSError, ValueError, TypeError, UnicodeError):
                data = {}
            token = data.get('token') if isinstance(data.get('token'), str) else None
            identity = ('token', token) if token else ('path', name)
            if identity in seen:
                continue
            seen.add(identity)
            acquired = data.get('acquired_at')
            try:
                held = max(0, int(time.time() - acquired)) if isinstance(acquired, (int, float)) else None
            except (OverflowError, ValueError):
                held = None
            owners.append({'project': _safe_owner_text(data.get('project')),
                           'task_id': _safe_owner_text(data.get('task_id')),
                           'model': _safe_owner_text(data.get('model')),
                           'reasoning': _safe_owner_text(data.get('reasoning')),
                           'held_seconds': held if held is not None else 'unknown'})
            if len(owners) >= MAX_OWNER_RECORDS:
                break
        return owners

    def _claim(self, prefix, maximum):
        for index in range(maximum):
            path = self.root / ('%s-slot-%d.json' % (prefix, index))
            # Pre-tier releases used ``slot-N.json`` for the global guard.
            # Treat a still-held legacy index as its current equivalent so a
            # mixed-version machine never exceeds the advertised global cap.
            if prefix == 'global' and (self.root / ('slot-%d.json' % index)).exists():
                continue
            try:
                fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            handle = None
            try:
                handle = os.fdopen(fd, 'r+', encoding='utf-8')
                _lock(handle, blocking=True)
                handle.seek(0)
                json.dump({'pid': os.getpid(), 'token': self.token, 'model': self.model,
                           'reasoning': self.reasoning, 'tier': self.tier, 'acquired_at': time.time(),
                           **self.owner}, handle,
                          separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                try:
                    if handle:
                        handle.close()
                    else:
                        os.close(fd)
                    path.unlink()
                except OSError:
                    pass
                raise
            self.paths.append(path)
            self.handles.append(handle)
            return index
        return None

    def _block(self, reason, message):
        occupied = self._occupied() if self.root.exists() else {}
        prefix = 'global' if reason == 'no_free_global_slot' else 'tier-%s' % self.tier
        self.evidence.update({'reason': reason, 'block_reason': message, 'occupied': occupied,
                              'blocked_slots': occupied, 'owners': self._owners(prefix)})
        raise AccountGuardError(message, dict(self.evidence))

    def acquire(self):
        if not self.options['enabled']:
            self.evidence['reason'] = 'disabled'
            return self
        if self.tier is None:
            self._block(self.classification_error,
                        'Account guard заблокировал ход: модель или reasoning не классифицированы безопасно.')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(str(self.root), 0o700)
        except OSError:
            pass
        self._cleanup_stale()
        global_slot = self._claim('global', self.options['max_concurrent'])
        if global_slot is None:
            self._block('no_free_global_slot', 'Нет свободного общего account guard слота; повторите позже.')
        try:
            tier_slot = self._claim('tier-%s' % self.tier, self.options['tier_limits'][self.tier])
        except BaseException:
            self.release()
            raise
        if tier_slot is None:
            self.release()
            self._block('no_free_tier_slot', 'Нет свободного слота tier %s; повторите позже.' % self.tier)
        occupied = self._occupied()
        self.evidence.update({'acquired': True, 'global_slot': global_slot, 'tier_slot': tier_slot,
                              'occupied': occupied, 'blocked_slots': occupied})
        return self

    def release(self):
        for path, handle in reversed(list(zip(self.paths, self.handles))):
            try:
                handle.seek(0)
                data = json.load(handle)
                if data.get('token') == self.token:
                    _unlock(handle)
                    handle.close()
                    path.unlink()
                else:
                    handle.close()
            except (OSError, ValueError):
                try:
                    handle.close()
                except OSError:
                    pass
        self.paths, self.handles = [], []
        self.evidence['released'] = True

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False


@contextmanager
def model_turn(config, model=None, reasoning=None, owner=None):
    guard = AccountGuard(config, model, reasoning, owner=owner)
    with guard:
        yield guard
