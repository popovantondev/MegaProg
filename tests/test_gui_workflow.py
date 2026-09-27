"""Two real CLI processes and a restarted window; all model transport is mocked."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from ai_dev.gui_process import CliRunner
from ai_dev.gui_window import Window
from ai_dev.gui_support import read_snapshot

WORKER = """
import contextlib,json,sys
from pathlib import Path
from unittest.mock import patch
from ai_dev import approved_plan,cli
COUNTER=Path(sys.argv.pop(1))
def execute(root,config,doctor,prompt,log,session=None):
    name='first' if log.name.startswith('first-task') else 'second'
    with COUNTER.open('a') as f:f.write(name+'\\n')
    (root/(name+'.py')).write_text('value = '+('1' if name=='first' else '2')+'\\n')
    usage={'input_tokens':20,'cached_input_tokens':0,'output_tokens':5}
    log.write_text(json.dumps({'type':'thread.started','thread_id':name+'-session'})+'\\n'+json.dumps({'type':'turn.completed','usage':usage})+'\\n')
    return {'ok':True,'session_id':name+'-session','usage':usage,'telemetry':{'duration_seconds':0.01}}
catalog={'models':[{'model':'gpt-6-luna','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}],'limits':None,'errors':{}}
with patch.object(approved_plan.codex,'doctor',return_value={'ready':True,'executable':'synthetic'}),patch.object(approved_plan,'discover',return_value=catalog),patch.object(approved_plan,'model_turn',return_value=contextlib.nullcontext()),patch.object(approved_plan.codex,'execute',side_effect=execute):
    sys.exit(cli.main(sys.argv[1:]))
"""


class GuiWorkflowTests(unittest.TestCase):
    def test_two_tasks_pause_restart_continue_and_export(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / "project"
            root.mkdir()
            counter = base / "calls.txt"
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "README.md").write_text("Synthetic fixture\n")
            (root / "docs").mkdir()
            (root / "docs/PRODUCT_ROADMAP.md").write_text(
                "# Product roadmap\nTwo synthetic features.\n"
            )
            (root / "docs/CONTINUITY.md").write_text(
                "# Continuity\nResume the saved approved plan.\n"
            )
            subprocess.run(["git", "add", "README.md", "docs"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "Fixture",
                ],
                cwd=root,
                check=True,
            )
            code_root = str(Path(__file__).resolve().parent.parent)
            worker = base / "synthetic_worker.py"
            worker.write_text(
                "import sys\nsys.path.insert(0," + repr(code_root) + ")\n" + WORKER
            )

            class OfflineRunner(CliRunner):
                def command(self, project, args):
                    return [
                        sys.executable,
                        "-u",
                        str(worker),
                        str(counter),
                        "-C",
                        str(project),
                    ] + list(args)

            def wait(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    app.processEvents()
                    if predicate():
                        return
                    QTest.qWait(20)
                self.fail("Offline CLI workflow did not finish")

            plan = {
                "schema_version": 1,
                "plan_id": "offline-two",
                "objective": "Two verified values",
                "budget": {"max_model_turns": 2},
                "features": [],
                "tasks": [],
            }
            for i, name in enumerate(("first", "second"), 1):
                check = [
                    sys.executable,
                    "-B",
                    "-c",
                    "from " + name + " import value; assert value == " + str(i),
                ]
                plan["features"].append(
                    {
                        "id": name,
                        "title": name,
                        "acceptance": ["value is " + str(i)],
                        "checks": [check],
                    }
                )
                plan["tasks"].append(
                    {
                        "id": name + "-task",
                        "feature_id": name,
                        "title": name,
                        "instructions": "Create " + name + ".py",
                        "depends_on": ["first-task"] if i == 2 else [],
                        "allowed_paths": [name + ".py"],
                        "checks": [check],
                    }
                )
            source = base / "plan.json"
            source.write_text(json.dumps(plan))
            windows = []
            try:
                first = Window(base / "preferences.json", OfflineRunner())
                windows.append(first)
                first.timer.setInterval(50)
                first.root = root
                first.show()
                first.load_plan(source)
                first.one_task = True
                with patch.object(first, "_dialog", return_value=True):
                    first.approve(True)
                wait(
                    lambda: not first.runner.busy
                    and first.snapshot
                    and first.snapshot["status"] == "PAUSED"
                )
                self.assertEqual(counter.read_text().splitlines(), ["first"])
                self.assertTrue(
                    read_snapshot(root, "offline-two")["features"][0]["proof"]["checks"]
                )
                first.close()
                app.processEvents()
                second = Window(base / "preferences.json", OfflineRunner())
                windows.append(second)
                second.timer.setInterval(50)
                second.root = root
                second.show()
                second._query_done(
                    "saved:" + str(second.generation),
                    read_snapshot(root, "offline-two"),
                    None,
                )
                self.assertEqual(second.snapshot["tasks"][0]["status"], "COMPLETED")
                with patch.object(second, "_dialog", return_value=True):
                    second.approve(True)
                wait(
                    lambda: not second.runner.busy
                    and second.snapshot
                    and second.snapshot["status"] == "COMPLETED"
                )
                self.assertEqual(counter.read_text().splitlines(), ["first", "second"])
                self.assertEqual(second.snapshot["model_turns"], 2)
                self.assertTrue(
                    all(f["proof"]["checks"] for f in second.snapshot["features"])
                )
                self.assertIn(second.t("verified"), second.result_view.toPlainText())
                second._start("handoff", ["handoff", "--output", str(base / "handoff")])
                wait(lambda: not second.runner.busy)
                self.assertTrue(
                    (base / "handoff/snapshot.json").is_file(), second.runner.output
                )
                self.assertEqual(second.message_key, "export_success")
            finally:
                for window in windows:
                    if window.runner.busy:
                        window.runner.stop()
                        wait(lambda: not window.runner.busy)
                    window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
