"""Small local execution signal; contains no prompts or command output."""
import json
import time
import shlex
import subprocess
from pathlib import Path
from .storage import write
from .process import WorkerStalled


class LiveSignal:
    def __init__(self, root, log=None, phase='RUNNING', activity='Ожидаем первый ответ Codex', verification_commands=None):
        self.root, self.log = Path(root), Path(log) if log is not None else None
        self.offset = 0
        self.pending = b''
        self.last_write = 0
        self.started = time.time()
        self.verification_commands = verification_commands or []
        self.failures = {}
        self.seen_checks = set()
        self.stalled = False
        self.check_evidence = {}
        self.discard_line = False
        try:
            state = json.loads((self.root / '.ai-dev/state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        self.data = {'task_id': state.get('id'), 'started_at': self.started, 'phase': phase,
                     'last_event_at': None, 'activity': activity, 'observed_operation': None,
                     'last_result': None, 'events': 0}

    def _command(self, value, depth=0):
        if not isinstance(value, str) or depth >= 4:
            return None
        for known in self.verification_commands:
            if value.strip() in (shlex.join(known), subprocess.list2cmdline(known)):
                return known
        try:
            argv = shlex.split(value)
            if len(argv) == 3 and argv[1] in ('-lc', '-c') and Path(argv[0]).name in ('sh', 'bash', 'zsh'):
                return self._command(argv[2], depth + 1) if argv[2] != value else None
            # A common CLI wrapper is cd <this repository> && <one check>.
            # Never match a command running in some other directory or a pipeline.
            if len(argv) > 3 and argv[0] == 'cd' and argv[2] == '&&':
                if (self.root / argv[1]).resolve() != self.root.resolve():
                    return None
                argv = argv[3:]
            return argv if argv in self.verification_commands else None
        except (ValueError, TypeError, OSError):
            return None

    def observe_check(self, item, defer=False):
        if self.stalled or not self.verification_commands or not isinstance(item.get('id'), str) or not item['id']:
            return
        identity = item['id']
        if identity in self.seen_checks:
            return
        self.seen_checks.add(identity)
        argv = self._command(item.get('command'))
        if argv is None:
            return
        code = item.get('exit_code')
        if type(code) is not int:
            return
        key = tuple(argv)
        if code == 0:
            self.failures[key] = 0
            self.check_evidence.pop(key, None)
            return
        output = str(item.get('aggregated_output', '')).lower()
        if code in (126, 127, 137, 143) or any(word in output for word in (
                'operation not permitted', 'permission denied', 'connection reset',
                'network is unreachable', 'usage limit', 'rate limit', 'command not found')):
            return
        self.failures[key] = self.failures.get(key, 0) + 1
        self.check_evidence[key] = {'command': argv, 'exit_code': code,
                                    'tail': str(item.get('aggregated_output', ''))[-3000:]}
        if self.failures[key] >= 2 and not defer:
            self.stalled = True
            raise WorkerStalled('Два провала настроенной проверки в одном model turn.')

    def stall_verification(self):
        return {'ok': False, 'checks': [self.check_evidence[key] for key, count in self.failures.items()
                                        if count >= 2 and key in self.check_evidence]}

    def update(self, pid, returncode=None, force=False):
        now = time.time()
        if not force and now - self.last_write < 2:
            return
        try:
            chunk = b''
            caught_up = True
            if self.log is not None:
                with self.log.open('rb') as stream:
                    stream.seek(self.offset)
                    chunk = stream.read(4 * 1024 * 1024)
                    self.offset += len(chunk)
                    caught_up = not stream.read(1)
            # Skip truly oversized records as whole records, never parse their
            # arbitrary tails as new JSON. Typical large test output is retained.
            if self.discard_line:
                if b'\n' not in chunk:
                    chunk = b''
                else:
                    chunk = chunk.split(b'\n', 1)[1]
                    self.discard_line = False
            lines = (self.pending + chunk).split(b'\n')
            self.pending = lines.pop()
            if len(self.pending) > 2 * 1024 * 1024:
                self.pending = b''
                self.discard_line = True
            for line in lines:
                if len(line) > 2 * 1024 * 1024:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get('type', '')
                if not isinstance(kind, str):
                    continue
                item = event.get('item') or {}
                item_type = item.get('type') if isinstance(item, dict) else None
                if not isinstance(item_type, str):
                    item_type = None
                if kind == 'item.completed' and item_type == 'command_execution':
                    self.observe_check(item, defer=True)
                label = {'command_execution': 'Выполняет команду проекта',
                         'file_change': 'Изменяет файлы', 'agent_message': 'Формирует ответ',
                         'reasoning': 'Анализирует задачу', 'mcp_tool_call': 'Вызывает инструмент',
                         'web_search': 'Ищет информацию'}.get(item_type)
                label = label or {'thread.started': 'Сессия Codex создана',
                                  'turn.started': 'Codex начал работу',
                                  'turn.completed': 'Ответ получен; далее проверка результата',
                                  'turn.failed': 'Codex сообщил об ошибке',
                                  'error': 'Codex сообщил об ошибке'}.get(kind)
                if label:
                    # CLI events are observations, not proof that a command or
                    # edit succeeded.  Keep the operation type only: commands,
                    # prompts and output are intentionally never copied here.
                    result = None
                    if kind == 'item.completed':
                        label += ' — событие завершено'
                        result = 'event_completed'
                    observed_file = None
                    if item_type == 'file_change' and isinstance(item.get('changes'), list):
                        for change in item['changes']:
                            candidate = change.get('path') if isinstance(change, dict) else None
                            if not isinstance(candidate, str):
                                continue
                            try:
                                relative = (self.root / candidate).resolve().relative_to(self.root.resolve())
                            except (OSError, ValueError):
                                continue
                            if relative.parts and relative.parts[0] != '.ai-dev':
                                observed_file = relative.as_posix()[:120]
                                break
                    self.data.update(last_event_at=now, activity=label,
                                     observed_operation=item_type or kind,
                                     observed_file=observed_file,
                                     last_result=result, events=self.data['events'] + 1)
            self.data.update(pid=pid, heartbeat_at=now, returncode=returncode,
                             process_state='running' if returncode is None else 'exited')
            write(self.root / '.ai-dev/live.json', self.data)
            self.last_write = now
            # A later PASS in the same delivered batch supersedes earlier
            # failures. Do not kill on a stale event while a log backlog remains.
            if not self.stalled and caught_up and not self.pending and not self.discard_line and any(n >= 2 for n in self.failures.values()):
                self.stalled = True
                raise WorkerStalled('Два провала настроенной проверки в одном model turn.')
        except OSError:
            # Monitoring must not interrupt a worker (e.g. a read-only disk).
            pass
