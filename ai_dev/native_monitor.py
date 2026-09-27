"""Native, read-only monitor. Opening it never starts a worker/model."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import tempfile
import hashlib
import uuid

from .storage import lock, read, write


def registry_root():
    identity = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / ('megaprog-monitors-' + identity)


def register(root):
    root = str(Path(root).resolve())
    key = hashlib.sha256(root.encode()).hexdigest()
    write(registry_root() / 'projects' / (key + '.json'), {'root': root})


def registered_projects():
    roots = []
    for path in sorted((registry_root() / 'projects').glob('*.json')):
        try:
            root = json.loads(path.read_text(encoding='utf-8'))['root']
            if Path(root).is_dir():
                roots.append(root)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return roots


def open_monitors(root):
    register(root)
    result = launch(root)
    launch(registry_root())
    return result


def monitor_root(root):
    return Path(root) / '.ai-dev' / 'native-monitor'


def readiness_path(root):
    return monitor_root(root) / 'ready.json'


def mark_ready(root, token=None):
    """Record that this monitor has rendered at least one real status view."""
    parent = os.environ.get('MEGAPROG_MONITOR_PARENT_PID', '')
    write(readiness_path(root), {'token': token or '', 'rendered_at': time.time(),
                                 'parent_pid': int(parent) if parent.isdigit() else None,
                                 'platform': sys.platform})


def windows_process_alive(pid):
    """Conservatively check a Windows CMD owner; unknown access is not dead."""
    if sys.platform != 'win32' or not isinstance(pid, int) or pid <= 0:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    except (OSError, AttributeError, ImportError, ValueError):
        return True


def orphaned_window(root):
    try:
        ready = read(readiness_path(root))
        return (ready.get('platform') == 'win32' and
                isinstance(ready.get('parent_pid'), int) and
                not windows_process_alive(ready['parent_pid']))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def is_ready(root, token):
    """A stale readiness file must never confirm a newly requested window."""
    try:
        return read(readiness_path(root)).get('token') == token
    except (OSError, ValueError, TypeError, KeyError):
        return False


def running(root):
    try:
        with lock(monitor_root(root)):
            return False
    except ValueError:
        return True


def launch(root):
    root = Path(root).resolve()
    if sys.platform not in ('darwin', 'win32'):
        raise OSError('Native monitor supports macOS Terminal and Windows CMD only')
    # Serialize launchers, separately from both the worker and monitor locks.
    with lock(monitor_root(root) / 'launcher'):
        if running(root):
            if not orphaned_window(root):
                return 'REUSED'
            deadline = time.monotonic() + 5
            while running(root) and time.monotonic() < deadline:
                time.sleep(.1)
            if running(root):
                raise OSError('Старый CMD закрыт, но его monitor-процесс не освободил lock за 5 секунд')
        runner = str(Path(__file__).with_name('native_monitor_runner.py'))
        token = 'megaprog-' + uuid.uuid4().hex
        # Native monitor windows are deliberately visual.  Codex often passes
        # NO_COLOR=1 to child processes for its own logs; that should not turn
        # a new, user-facing Terminal/CMD window monochrome.  Keep a dedicated
        # monitor opt-out for users who actually want plain native windows.
        monitor_env = dict(os.environ)
        if monitor_env.get('MEGAPROG_MONITOR_NO_COLOR') != '1':
            monitor_env.pop('NO_COLOR', None)
            monitor_env['MEGAPROG_COLOR'] = 'always'
        if sys.platform == 'darwin':
            monitor_env.setdefault('MEGAPROG_THEME', 'light')
        if sys.platform == 'darwin':
            # The custom title lets the child close exactly its own Terminal tab
            # after a successful task.  It must never close the user's other tabs.
            command_parts = ['env']
            if monitor_env.get('MEGAPROG_MONITOR_NO_COLOR') != '1':
                command_parts.extend(['-u', 'NO_COLOR', 'MEGAPROG_COLOR=always',
                                      'MEGAPROG_THEME=' + monitor_env['MEGAPROG_THEME']])
            command_parts.extend(['MEGAPROG_MONITOR_TOKEN=' + token,
                                  sys.executable, '-u', runner, str(root)])
            command = shlex.join(command_parts)
            script = ('tell application "Terminal"\nactivate\n'
                      'set monitorTab to do script %s\n'
                      'set custom title of monitorTab to %s\nend tell') % (
                          json.dumps(command, ensure_ascii=False), json.dumps(token))
            subprocess.run(['/usr/bin/osascript', '-e', script], check=True,
                           capture_output=True, text=True, timeout=20)
        else:
            # Constant CMD source: paths travel in environment variables, never
            # interpolated as shell source. /v:off preserves ! in paths.
            env = dict(monitor_env, MEGAPROG_PY=sys.executable,
                       MEGAPROG_MONITOR_RUNNER=runner, MEGAPROG_MONITOR_ROOT=str(root),
                       MEGAPROG_MONITOR_KIND='overview' if root == registry_root().resolve() else 'task',
                       MEGAPROG_MONITOR_TOKEN=token,
                       PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            # Explicit conhost avoids Windows' default-terminal redirection:
            # CREATE_NEW_CONSOLE alone may open a Windows Terminal tab.
            conhost = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'conhost.exe')
            subprocess.Popen(subprocess.list2cmdline([conhost, os.environ.get('COMSPEC', 'cmd.exe')]) +
                             ' /d /v:off /c native_monitor_windows.cmd',
                             cwd=str(Path(__file__).resolve().parent),
                             env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # Acquiring the monitor lock proves only that the runner started.
            # Do not claim STARTED until it has rendered an observed status.
            if running(root) and is_ready(root, token):
                return 'STARTED'
            time.sleep(.1)
        raise OSError('Окно запрошено, но монитор не подтвердил вывод статуса за 10 секунд')


def auto_open(root):
    if os.environ.get('MEGAPROG_NO_MONITOR') == '1':
        return
    try:
        result = open_monitors(root)
        print('MegaProg native monitor: ' + result, file=sys.stderr, flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, 'stderr', None) or str(exc)
        print('Не удалось открыть монитор: %s. Задача продолжает работу. '
              'Повторить: ai-dev -C <проект> monitor' % detail, file=sys.stderr, flush=True)


ACTIVE_STATUSES = frozenset(('PENDING', 'PREFLIGHT', 'RUNNING', 'CLAIMED', 'CHECKPOINTED',
                             'VERIFY', 'VERIFYING', 'WAITING_FOR_SLOT',
                             'PENDING_APPROVAL', 'RETRY'))


def should_auto_close(result, overview):
    """Native task windows close only after verified successful completion.

    The all-project overview remains available until the user closes it.  A
    blocked/cancelled task window intentionally stays visible as well.
    """
    statuses = [item.get('status') for item in result.get('projects', [])]
    if overview:
        return False
    return (bool(statuses) and
            all(item.get('status') == 'COMPLETED' and item.get('verify') == 'PASS'
                for item in result.get('projects', [])))


def _native_watch(root, close_delay=4, poll_seconds=2):
    """Render a native monitor and return after its safe auto-close condition."""
    from . import dashboard
    from .terminal_style import styled, interactive, color_enabled
    root = Path(root).resolve()
    overview = root == registry_root().resolve()
    token = os.environ.get('MEGAPROG_MONITOR_TOKEN')
    tick = 0
    previous_signature = None
    # A TTY alone does not guarantee Windows VT escape support. Probe/enable
    # it even with NO_COLOR, because alternate-screen controls also need VT.
    screen = interactive()
    use_color = screen and color_enabled()
    if screen:
        print('\033[?1049h\033[?25l', end='', flush=True)
    try:
        while True:
            parent = os.environ.get('MEGAPROG_MONITOR_PARENT_PID', '')
            if sys.platform == 'win32' and parent.isdigit() and not windows_process_alive(int(parent)):
                return 0
            projects = registered_projects() if overview else [root]
            result = dashboard.dashboard(list(dict.fromkeys(projects)))
            current_signature = dashboard.signature(result)
            if screen:
                print('\033[H\033[2J' + styled(dashboard.render(result, overview=overview), enabled=use_color), end='\n', flush=True)
            elif current_signature != previous_signature:
                print('\n' + styled(dashboard.render(result, overview=overview), enabled=False), flush=True)
            else:
                print(dashboard.heartbeat(result, tick), end='\r', flush=True)
            if tick == 0:
                # This happens only after stdout accepted the first status,
                # which is the earliest honest readiness confirmation.
                mark_ready(root, token)
            previous_signature = current_signature
            tick += 1
            if should_auto_close(result, overview):
                message = ('Нет активных задач. Общий монитор закроется через %d с.' if overview else
                           'Проверки пройдены. Это окно закроется через %d с.') % close_delay
                print('\n' + message, flush=True)
                time.sleep(close_delay)
                return 0
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        if screen:
            try:
                print('\033[?25h\033[?1049l', end='', flush=True)
            except OSError:
                pass  # the user already closed this native console


def serve(root, auto_close=False):
    from .cli import main
    try:
        with lock(monitor_root(root)):
            if auto_close:
                return _native_watch(root)
            if Path(root).resolve() == registry_root().resolve():
                return main(['dashboard', '--all', '--watch'])
            return main(['dashboard', '--root', str(root), '--watch'])
    except ValueError as exc:
        print(str(exc), flush=True)
        return 1
