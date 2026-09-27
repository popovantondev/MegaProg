"""Qt adapters around the unchanged CLI and background read-only queries."""

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
    QProcess,
    QProcessEnvironment,
)


class _Signals(QObject):
    complete = Signal(str, object, object)


class _Job(QRunnable):
    def __init__(self, key, fn, args):
        super().__init__()
        self.key = key
        self.fn = fn
        self.args = args
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.complete.emit(self.key, self.fn(*self.args), None)
        except Exception as exc:
            self.signals.complete.emit(self.key, None, exc)


class Queries(QObject):
    complete = Signal(str, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs = {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)

    def submit(self, key, fn, *args):
        if key in self.jobs:
            return False
        job = _Job(key, fn, args)
        self.jobs[key] = job
        job.signals.complete.connect(self._finished)
        self.pool.start(job)
        return True

    def _finished(self, key, result, error):
        self.jobs.pop(key, None)
        self.complete.emit(key, result, error)


def last_json(output):
    decoder = json.JSONDecoder()
    result = None
    for i, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[i:])
        except ValueError:
            continue
        if isinstance(value, dict):
            # Ignore nested objects in the following scan.
            if any(k in value for k in ("ready", "plan_id", "snapshot", "output")):
                result = value
    return result


class CliRunner(QObject):
    line = Signal(str)
    finished = Signal(str, int, str)
    started = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.process.started.connect(self._started)
        self.kind = None
        self.output = ""
        self._tmp = None
        self.log = None
        self.stopping = False

    @property
    def busy(self):
        return self.kind is not None

    def start(self, kind, root, args):
        if self.busy:
            raise RuntimeError("A command is already running")
        self.output = ""
        self.stopping = False
        self._tmp = tempfile.TemporaryDirectory(prefix="megaprog-desktop-")
        self.cancel_path = Path(self._tmp.name) / "cancel"
        env = QProcessEnvironment.systemEnvironment()
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
            env.remove(key)
        env.insert("AI_DEV_CANCEL_FILE", str(self.cancel_path))
        env.insert("PYTHONUNBUFFERED", "1")
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(root))
        directory = Path(root) / ".ai-dev/gui-logs"
        directory.mkdir(parents=True, exist_ok=True)
        self.log = (directory / ("%s-%d.log" % (kind, time.time_ns()))).open(
            "w", encoding="utf-8"
        )
        command = self.command(root, args)
        self.kind = kind
        self.process.start(command[0], command[1:])

    def command(self, root, args):
        prefix = (
            [sys.executable, "--cli"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-u", "-m", "ai_dev.cli"]
        )
        return prefix + ["-C", str(root)] + list(args)

    def _started(self):
        self.started.emit(self.kind)
        if self.stopping:
            self.stop()

    def _read(self):
        chunk = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if not chunk:
            return
        self.output = (self.output + chunk)[-2_000_000:]
        if self.log:
            self.log.write(chunk)
            self.log.flush()
        self.line.emit(chunk)

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.output += self.process.errorString()
            self._finish(-1)

    def _finished(self, code, _status):
        self._read()
        self._finish(code)

    def _finish(self, code):
        if self.kind is None:
            return
        kind = self.kind
        output = self.output
        self.kind = None
        if self.log:
            self.log.close()
            self.log = None
        if self._tmp:
            self._tmp.cleanup()
            self._tmp = None
        self.finished.emit(kind, code, output)

    def stop(self):
        if not self.busy:
            return
        self.stopping = True
        self.cancel_path.touch()
        pid = self.process.processId()
        if pid:
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
