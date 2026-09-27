"""Bounded subprocesses; no shell interpolation."""
import os
import signal
import subprocess
import time
from pathlib import Path


class WorkerStalled(RuntimeError):
    """Two observed failures of an explicitly configured verification command."""


def run(argv, cwd, timeout=30, input_text=None, env=None, output_path=None, live=None):
    log = open(output_path, 'w', encoding='utf-8') if output_path else None
    try:
        p = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE,
                             stdout=log if log else subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=env,
                             start_new_session=(os.name != "nt"))
    except BaseException:
        if log:
            log.close()
        raise
    try:
        deadline = time.monotonic() + timeout if timeout else None
        first = True
        while True:
            if live:
                live.update(p.pid, p.poll())
            cancel = os.environ.get('AI_DEV_CANCEL_FILE')
            if cancel and Path(cancel).exists():
                raise KeyboardInterrupt()
            remaining = deadline - time.monotonic() if deadline is not None else .25
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                output, _ = p.communicate(input_text if first else None, timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
        if log:
            log.flush()
            output = output_path.read_text(encoding='utf-8', errors='replace')
        return p.returncode, output
    except (subprocess.TimeoutExpired, KeyboardInterrupt, WorkerStalled):
        if os.name != "nt":
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            # Stop the command/test children too before handing this worktree
            # to another model. A surviving child could still modify files.
            subprocess.run([os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                            'System32', 'taskkill.exe'), '/PID', str(p.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if p.poll() is None:
                p.terminate()
        try:
            p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                p.kill()
            p.communicate()
        raise

    finally:
        try:
            if live:
                live.update(p.pid, p.poll(), force=True)
        finally:
            if log:
                log.close()
