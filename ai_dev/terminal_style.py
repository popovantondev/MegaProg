"""Human terminal styling only; persisted reports and JSON stay plain."""
import os
import re
import sys


def color_enabled(enabled=None):
    """Return whether human terminal output may contain ANSI styling.

    ``NO_COLOR`` is deliberately checked before the MegaProg opt-in.  This
    keeps a user's explicit no-color choice safe when a parent process also
    supplies ``MEGAPROG_COLOR=always``.  Callers that already made a local
    rendering decision can still pass ``enabled`` explicitly.
    """
    if enabled is not None:
        return bool(enabled)
    if 'NO_COLOR' in os.environ:
        return False
    override = os.environ.get('MEGAPROG_COLOR', '').lower()
    if override in ('never', 'off', '0', 'false'):
        return False
    if override in ('always', 'on', '1', 'true'):
        return True
    return interactive()


def theme():
    """Choose a readable palette; users may force light or dark terminals."""
    value = os.environ.get('MEGAPROG_THEME', 'auto').lower()
    if value in ('light', 'dark'):
        return value
    # Windows has no reliable portable API for the user's background color.
    # Keep an explicit override and use a conservative light fallback there.
    return 'light' if os.name == 'nt' else 'dark'


def _code(kind):
    palettes = {
        'dark': {'heading': '1;36', 'current': '1;93', 'status': '1;36',
                 'model': '1;35', 'error': '1;31', 'success': '32',
                 'warning': '33', 'running': '36'},
        # Bright yellow and bright cyan disappear on many white CMD themes;
        # use darker ANSI foregrounds for the light palette.
        'light': {'heading': '1;34', 'current': '1;35', 'status': '1;34',
                  'model': '35', 'error': '31', 'success': '32',
                  'warning': '33', 'running': '34'},
    }
    return palettes[theme()][kind]


def interactive():
    if not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb':
        return False
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.GetStdHandle(-11 & 0xffffffff)
            mode = wintypes.DWORD()
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel.SetConsoleMode(handle, mode.value | 4))
        except (OSError, AttributeError, ImportError):
            return False
    return True


def styled(text, enabled=None):
    if not color_enabled(enabled):
        return text
    lines = []
    for line in text.split('\n'):
        code = None
        if line.startswith('MEGAPROG'):
            code = _code('heading')
        elif line.startswith('══ ТЕКУЩАЯ ЗАДАЧА:') or line.startswith('══ ПОСЛЕДНЯЯ ЗАДАЧА:'):
            code = _code('current')
        elif line.startswith('  Статус:'):
            code = _code('status')
        elif line.startswith('  Модель:') or line.startswith('MODEL DECISION:'):
            code = _code('model')
        elif line.startswith('  Задача:'):
            code = '1'
        elif line.startswith('  Папка:') or line.startswith('  Состояние обновлено:'):
            code = '2'
        elif any(word in line for word in ('BLOCKED', 'ERROR', 'FAIL', 'ВНИМАНИЕ', 'Остановлено', 'Нет свежего')):
            code = _code('error')
        elif 'PASS' in line or 'COMPLETED' in line or 'Завершено' in line or 'процесс работает' in line.lower():
            code = _code('success')
        elif any(word in line for word in ('VERIFY', 'Проверка', 'Ждём', 'пока не подтверждена')):
            code = _code('warning')
        elif 'RUNNING' in line or line.startswith('  Сейчас:'):
            code = _code('running')
        if code:
            line = '\033[' + code + 'm' + line + '\033[0m'
        # Emphasize model IDs in compact overview too.
        if code != '1;35':
            line = re.sub(r'\bgpt-[\w.-]+', lambda m: '\033[1;35m' + m.group() + '\033[0m', line)
        lines.append(line)
    return '\n'.join(lines)
