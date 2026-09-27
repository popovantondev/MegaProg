"""Read-only documented app-server JSON-RPC, without UI or model requests."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from . import __version__
from .codex import environment


class Server:
    def __init__(self, executable, root, timeout=20):
        self.timeout = timeout
        self.serial = 0
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [executable, 'app-server', '-c', 'model_provider="openai"',
             '-c', 'forced_login_method="chatgpt"'], cwd=str(root), env=environment(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace')
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def call(self, method, params=None):
        self.serial += 1
        ident = self.serial
        self.send({'id': ident, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            cancel = os.environ.get('AI_DEV_CANCEL_FILE')
            if cancel and Path(cancel).exists():
                raise KeyboardInterrupt()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('Codex app-server: таймаут ' + method)
            try:
                msg = self.messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if msg is None:
                raise ValueError('Codex app-server завершился: ' + method)
            if msg.get('id') == ident and ('result' in msg or 'error' in msg):
                if 'error' in msg:
                    raise ValueError(str(msg['error']))
                return msg['result']
            if 'id' in msg and 'method' in msg:
                self.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Read-only client'}})

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()


def discover(root, executable):
    result = {'models': [], 'limits': None, 'errors': {}, 'fetched_at': time.time()}
    server = None
    try:
        server = Server(executable, root)
        server.call('initialize', {'clientInfo': {'name': 'ai_dev', 'version': __version__}})
        server.send({'method': 'initialized'})
        cursor = None
        seen = set()
        for _ in range(20):
            page = server.call('model/list', {'limit': 100, 'includeHidden': False, 'cursor': cursor})
            if not isinstance(page.get('data'), list):
                raise ValueError('Неверный формат model/list')
            result['models'].extend(page['data'])
            cursor = page.get('nextCursor')
            if cursor is None:
                break
            if cursor in seen:
                raise ValueError('Повторяющийся cursor model/list')
            seen.add(cursor)
        else:
            raise ValueError('Слишком много страниц model/list')
        try:
            result['limits'] = server.call('account/rateLimits/read')
        except (ValueError, OSError) as exc:
            result['errors']['limits'] = str(exc)
    except (ValueError, OSError) as exc:
        result['errors']['models'] = str(exc)
        result['models'] = []
    finally:
        if server:
            server.close()
    return result
