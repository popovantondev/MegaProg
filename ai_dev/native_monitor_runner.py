"""Absolute-path entry point usable without installing the package."""
import os
from pathlib import Path
import subprocess
import sys
import time


def close_own_mac_tab(token):
    """Close only the Terminal tab carrying this monitor's unique title."""
    import json
    script = ('tell application "Terminal"\n'
              'repeat with terminalWindow in windows\n'
              'repeat with monitorTab in tabs of terminalWindow\n'
              'if custom title of monitorTab is %s then\n'
              'if (count of tabs of terminalWindow) is 1 then\n'
              'close terminalWindow\n'
              'else\n'
              'return "SHARED_WINDOW"\n'
              'end if\n'
              'return "CLOSED"\n'
              'end if\n'
              'end repeat\n'
              'end repeat\n'
              'end tell\n'
              'return "NOT_FOUND"') % json.dumps(token, ensure_ascii=False)
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script], check=False,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        print('MegaProg: не удалось закрыть окно монитора: %s' % exc, file=sys.stderr)
        return False
    if result.returncode == 0 and result.stdout.strip() == 'CLOSED':
        return True
    reason = result.stderr.strip() or result.stdout.strip() or 'нет ответа Terminal'
    print('MegaProg: монитор завершён, но его вкладка осталась: %s. '
          'Эту вкладку можно закрыть вручную.' % reason, file=sys.stderr)
    return False


def schedule_close_own_mac_tab(token):
    """Close after this Python process exits, so Terminal shows no kill dialog."""
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                      '--close-tab-after-pid', str(os.getpid()), token],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def close_tab_after_pid(pid, token):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.1)
    else:
        # A still-running monitor must never be interrupted just to close UI.
        return False
    time.sleep(.5)  # let Terminal's shell return to its prompt
    return close_own_mac_tab(token)

def prepare_terminal():
    """Prepare only the native console, preserving explicit user styling.

    The child may remove an inherited headless ``TERM=dumb`` marker because it
    has a real console, but it must not erase ``NO_COLOR``.  The monitor-only
    switch is the one intentional way to request no color for this child.
    """
    if not sys.stdout.isatty():
        return
    if sys.platform == 'win32':
        # The CMD process owns the visible window.  A detached Python child
        # must stop when that owner disappears instead of holding the monitor
        # lock forever behind an invisible REUSED result.
        os.environ['MEGAPROG_MONITOR_PARENT_PID'] = str(os.getppid())
    if os.environ.get('TERM') == 'dumb':
        os.environ.pop('TERM', None)
    if os.environ.get('MEGAPROG_MONITOR_NO_COLOR') == '1':
        os.environ['NO_COLOR'] = '1'


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--close-tab-after-pid':
        raise SystemExit(0 if close_tab_after_pid(int(sys.argv[2]), sys.argv[3]) else 1)
    prepare_terminal()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ai_dev.native_monitor import serve
    root = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ['MEGAPROG_MONITOR_ROOT'])
    status = serve(root, auto_close=True)
    # macOS Terminal normally leaves a prompt behind after a child command exits.
    # Close only the uniquely titled tab launched for this monitor, never a user tab.
    token = os.environ.get('MEGAPROG_MONITOR_TOKEN')
    if status == 0 and token and sys.platform == 'darwin':
        schedule_close_own_mac_tab(token)
    raise SystemExit(status)
