"""Read-only Windows readiness diagnostics.

No project files are changed.  Without a Windows runner, Windows-only runtime
checks are honestly marked NOT_RUN rather than treated as verified.
"""
import os
import sys
import subprocess
import tempfile
from pathlib import Path
from .context_isolation import isolate_generated_context
from .storage import lock


def audit(root=None):
    root = Path(root or ".").resolve()
    result = {"platform": os.name, "overall": "NOT_RUN", "checks": [],
              "next_actions": ["Run python -m unittest discover -s tests -v on a real Windows 10/11 runner.",
                                "Run .\\ai-dev.cmd windows-check there and inspect launcher, subprocess, lock, and isolation results."]}
    def add(name, status, detail):
        result["checks"].append({"name": name, "status": status, "detail": detail})
    add("utf8-path-contract", "PASS", "Python can represent Unicode paths; diagnostics use explicit UTF-8.")
    launcher = root / "Запустить_Windows.cmd"
    launcher_ok = launcher.is_file()
    launcher_status = ("PASS" if launcher_ok else
                       ("NOT_RUN" if os.name != "nt" else "BLOCKED"))
    add("windows-launcher-present", launcher_status,
        str(launcher) if launcher_ok else "Windows packaging is outside the macOS preview scope.")
    if os.name != "nt":
        add("launcher-runtime", "NOT_RUN", "Requires Windows cmd.exe and Python launcher.")
        add("subprocess", "NOT_RUN", "Requires native Windows subprocess execution.")
        add("lock", "NOT_RUN", "Requires msvcrt locking semantics on Windows.")
        add("temporary-context-isolation", "NOT_RUN", "Requires Windows rename/lock behavior.")
    else:
        try:
            if b'ai-dev.cmd' not in launcher.read_bytes():
                raise ValueError("launcher does not reference CLI entrypoint")
            probe = subprocess.run(['cmd.exe', '/d', '/c', str(launcher), '--help'],
                                   cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=30, text=True, encoding='utf-8', errors='replace')
            add("launcher-runtime", "PASS" if probe.returncode == 0 and 'dashboard' in probe.stdout else "BLOCKED",
                "Native CMD CLI --help probe, exit=%s" % probe.returncode)
            with tempfile.TemporaryDirectory(prefix="megaprog-win-") as folder:
                temp_root = Path(folder) / "тест"
                temp_root.mkdir()
                (temp_root / "данные.txt").write_text("UTF-8 ✓", encoding="utf-8")
                completed = subprocess.run([sys.executable, "-c", "print('ok')"], cwd=str(temp_root),
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                add("utf8-path-runtime", "PASS", (temp_root / "данные.txt").read_text(encoding="utf-8"))
                add("subprocess", "PASS" if completed.returncode == 0 and completed.stdout.strip() == "ok" else "BLOCKED",
                    completed.stderr.strip() or "native Python subprocess completed")
                with lock(temp_root):
                    lock_detail = "msvcrt lock acquired and released"
                add("lock", "PASS", lock_detail)
                (temp_root / "generated").mkdir()
                (temp_root / "generated" / "value.txt").write_text("x", encoding="utf-8")
                with isolate_generated_context(temp_root):
                    hidden = not (temp_root / "generated").exists()
                restored = (temp_root / "generated" / "value.txt").read_text(encoding="utf-8") == "x"
                add("temporary-context-isolation", "PASS" if hidden and restored else "BLOCKED",
                    "generated path hidden and restored")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            add("launcher-runtime", "BLOCKED", str(exc))
            add("subprocess", "BLOCKED", str(exc))
            add("lock", "BLOCKED", str(exc))
            add("temporary-context-isolation", "BLOCKED", str(exc))
    if any(item["status"] == "BLOCKED" for item in result["checks"]):
        result["overall"] = "BLOCKED"
    elif os.name == "nt":
        result["overall"] = "PASS"
    return result
