import copy
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
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest
from ai_dev.gui_window import Window
from ai_dev.gui_process import CliRunner
from ai_dev.gui_support import (
    ReviewedPlan,
    GuiError,
    save_preferences,
    read_preferences,
    project_root,
    saved_plans,
    read_snapshot,
    feature_verified,
    reason_key,
    create_demo_project,
)
from ai_dev import approved_plan


class FakeRunner(QObject):
    line = Signal(str)
    finished = Signal(str, int, str)
    started = Signal(str)

    def __init__(self):
        super().__init__()
        self.busy = False
        self.kind = None
        self.stopping = False
        self.calls = []

    def start(self, kind, root, args):
        self.calls.append((kind, root, args))
        self.kind = kind
        self.busy = True
        self.stopping = False
        self.started.emit(kind)

    def stop(self):
        self.stopping = True

    def finish(self, code=0, data=None):
        kind = self.kind
        self.kind = None
        self.busy = False
        self.finished.emit(kind, code, json.dumps(data or {}))


def plan_fixture():
    return {
        "schema_version": 1,
        "plan_id": "small-plan",
        "objective": "A small example",
        "budget": {"max_model_turns": 2},
        "features": [
            {
                "id": "one",
                "title": "A feature",
                "acceptance": ["A concrete result"],
                "checks": [[sys.executable, "-c", 'print("check")']],
            }
        ],
        "tasks": [
            {
                "id": "task-one",
                "feature_id": "one",
                "title": "A small task",
                "instructions": "Create one file",
                "depends_on": [],
                "allowed_paths": ["feature.py"],
                "checks": [[sys.executable, "-c", 'print("task check")']],
            }
        ],
    }


class WizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        font = cls.app.font()
        font.setPointSizeF(14)
        cls.app.setFont(font)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.source = self.base / "plan.json"
        self.plan = plan_fixture()
        self.source.write_text(json.dumps(self.plan))
        self.runner = FakeRunner()
        self.window = Window(self.base / "preferences.json", self.runner)
        self.window.timer.stop()
        self.window.root = self.root
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.runner.busy = False
        self.window.close()
        self.app.processEvents()

    def wait(self, predicate, seconds=8):
        limit = time.monotonic() + seconds
        while time.monotonic() < limit:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail("Timed out waiting for Qt operation")

    def load(self):
        self.window.load_plan(self.source)
        self.app.processEvents()

    def imported(self):
        self.load()
        approved_plan.import_plan(self.root, self.source)
        self.window.saved_id = self.plan["plan_id"]
        self.window.review = None
        self.window.snapshot = read_snapshot(self.root, self.plan["plan_id"])
        self.window._render()

    def test_reading_plan_does_not_create_state_or_execute(self):
        self.load()
        self.assertFalse((self.root / ".ai-dev").exists())
        self.assertFalse(self.runner.calls)
        self.assertEqual(self.window.message_key, "reviewed")
        self.assertIn("print", self.window.plan_view.toPlainText())

    def test_invalid_replacement_cannot_run_previously_viewed_plan(self):
        self.load()
        invalid = self.base / "invalid.json"
        invalid.write_text("{")
        self.window.load_plan(invalid)
        self.assertIsNone(self.window.review)
        self.assertIsNone(self.window.plan)
        self.assertIsNone(self.window.saved_id)
        self.window.approve(True)
        self.assertFalse(self.runner.calls)

    def test_start_preparation_is_not_shown_as_ready(self):
        self.imported()
        self.runner.busy = True
        self.runner.kind = "run"
        self.window._render_snapshot()
        self.assertEqual(self.window.phase.text(), self.window.t("preparing"))
        self.runner.stopping = True
        self.window._render_snapshot()
        self.assertEqual(self.window.phase.text(), self.window.t("stopping"))

    def test_recent_projects_refresh_without_restart_and_forget_keeps_files(self):
        self.window.select_project(str(self.root))
        self.wait(lambda: not self.window.operation)
        resolved = str(self.root.resolve())
        self.assertGreater(self.window.recent.findData(resolved), 0)
        self.window.recent.setCurrentIndex(self.window.recent.findData(resolved))
        self.window._forget()
        self.assertEqual(self.window.recent.findData(resolved), -1)
        self.assertTrue(self.root.is_dir())

    def test_failed_folder_keeps_path_and_drops_previous_review(self):
        self.load()
        plain = self.base / "plain-folder"
        plain.mkdir()
        self.window.select_project(str(plain))
        self.wait(lambda: not self.window.operation)
        self.assertEqual(self.window.message_key, "error_project_not_git")
        self.assertFalse(self.window.notice.isVisible())
        self.assertEqual(self.window.project_path.full, str(plain.absolute()))
        self.assertTrue(self.window.copy_project_button.isEnabled())
        self.assertIsNone(self.window.root)
        self.assertIsNone(self.window.review)
        self.assertIsNone(self.window.plan)
        self.assertFalse(self.window.primary.isEnabled())
        self.assertIn(str(plain.resolve()), self.window.log_text)
        self.window.set_language("de")
        self.assertIn("Git-Projekthistorie", self.window.project_error.text())

    def test_demo_button_cancel_does_not_create_a_project(self):
        button = next(
            item for item in self.window.findChildren(QPushButton)
            if item.text() == self.window.t("demo_try")
        )
        self.assertTrue(button.isVisible())
        with patch.object(self.window, "_file_dialog", return_value=None):
            self.window._choose_demo()
        self.assertEqual(set(self.base.iterdir()), {self.root, self.source})
        self.assertIsNone(self.window.operation)
        self.assertFalse(self.runner.calls)

    def test_demo_button_creates_and_previews_plan_without_running(self):
        # The expected folder and plan text are Russian; runner locale varies.
        self.window.set_language("ru")
        parent = self.base / "tutorials"
        parent.mkdir()
        with patch.object(self.window, "_file_dialog", return_value=str(parent)):
            self.window._choose_demo()
        self.wait(lambda: not self.window.operation)
        root = parent / "MegaProg-Учебный-проект"
        self.assertEqual(self.window.root, root.resolve())
        self.assertEqual(self.window.plan["objective"],
                         "Учебный проект: сложение и умножение с автоматическими проверками.")
        self.assertEqual(self.window.step, 2)
        self.assertIsNone(self.window.saved_id)
        self.assertTrue(self.window.one_task)
        self.assertTrue(self.window.one_check.isChecked())
        self.assertFalse((root / ".ai-dev").exists())
        self.assertFalse(self.runner.calls)
        self.assertEqual(subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True), "")

    def test_demo_strings_exist_for_all_languages_and_preview_contains_example(self):
        for language in ("ru", "de", "en"):
            self.window.set_language(language)
            self.assertTrue(self.window.demo_button.text())
            self.assertTrue(self.window.demo_hint.text())


    def test_nested_folder_message_includes_existing_root(self):
        nested = self.root / "nested"
        nested.mkdir()
        self.window.select_project(str(nested))
        self.wait(lambda: not self.window.operation)
        self.assertEqual(self.window.message_key, "error_project_nested")
        self.assertIn(str(self.root.resolve()), self.window.project_error.text())

    def test_modified_source_invalidates_approval(self):
        self.load()
        self.source.write_text("{}")
        self.window.approve(False)
        self.assertFalse(self.runner.calls)
        self.assertEqual(self.window.message_key, "error_changed")

    def test_change_during_confirmation_cannot_run(self):
        self.load()

        def confirm(*_):
            self.source.write_text("{}")
            return True

        with patch.object(self.window, "_dialog", side_effect=confirm):
            self.window.approve(True)
        self.assertFalse(self.runner.calls)
        self.assertEqual(self.window.message_key, "error_changed")

    def test_import_uses_reviewed_copy_and_save_never_runs(self):
        self.load()
        raw = self.source.read_bytes()
        self.window.approve(False)
        kind, root, args = self.runner.calls[-1]
        self.assertEqual(kind, "import")
        frozen = Path(args[2])
        self.assertNotEqual(frozen, self.source)
        self.assertEqual(frozen.read_bytes(), raw)
        self.source.write_text("{}")
        self.assertEqual(frozen.read_bytes(), raw)
        approved_plan.import_plan(self.root, frozen)
        data = approved_plan.summary(self.root, self.plan["plan_id"])
        self.runner.finish(data=data)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.window.message_key, "saved")
        self.assertFalse(frozen.exists())

    def test_failed_import_does_not_launch_executor(self):
        self.load()
        with patch.object(self.window, "_dialog", return_value=True):
            self.window.approve(True)
        self.runner.finish(1, {"error": "failure"})
        self.assertEqual(len(self.runner.calls), 1)
        self.assertFalse(self.window.pending_run)

    def test_stop_waits_for_process_exit(self):
        self.imported()
        self.window._run_saved()
        self.window._stop()
        self.assertTrue(self.runner.busy)
        self.assertEqual(self.window.message_key, "stopping")
        self.assertFalse(self.window.primary.isEnabled())
        self.runner.finish(130)
        self.assertEqual(self.window.message_key, "stopped_actual")
        self.assertEqual(self.window.step, 4)

    def test_one_task_option_applies_to_resume(self):
        self.imported()
        self.window.snapshot["status"] = "PAUSED"
        self.window.one_task = True
        self.window._run_saved()
        args = self.runner.calls[-1][2]
        self.assertEqual(args[1], "resume")
        self.assertEqual(args[-2:], ["--max-tasks", "1"])

    def test_completed_plan_cannot_be_reexecuted(self):
        self.imported()
        self.window.snapshot["status"] = "COMPLETED"
        self.window._run_saved()
        self.assertFalse(self.runner.calls)
        self.assertEqual(self.window.step, 4)

    def test_language_and_theme_keep_review_identity(self):
        self.load()
        identity = self.window.review
        self.window.one_task = True
        for language in ("de", "en", "ru"):
            self.window.set_language(language)
            self.window.set_theme("dark")
            self.assertIs(self.window.review, identity)
            self.assertEqual(self.window.root, self.root)
            self.assertTrue(self.window.one_check.isChecked())
            self.assertIn("A small example", self.window.plan_view.toPlainText())

    def test_old_snapshot_cannot_replace_new_review(self):
        self.imported()
        old = read_snapshot(self.root, self.plan["plan_id"])
        generation = self.window.generation
        self.load()
        review = self.window.review
        self.window._query_done("snapshot:" + str(generation), old, None)
        self.assertIs(self.window.review, review)
        self.assertIsNone(self.window.saved_id)

    def test_multiple_saved_plans_require_explicit_selection(self):
        approved_plan.import_plan(self.root, self.source)
        second = copy.deepcopy(self.plan)
        second["plan_id"] = "second"
        self.source.write_text(json.dumps(second))
        approved_plan.import_plan(self.root, self.source)
        self.window.saved_rows = saved_plans(self.root)
        self.window._render()
        self.assertEqual(len(self.window.saved_rows), 2)
        self.assertIsNone(self.window.saved_combo.currentData())
        self.assertIsNone(self.window.saved_id)
        self.window.saved_combo.setCurrentIndex(2)
        selected = self.window.saved_combo.currentData()
        self.window._render()
        self.assertEqual(self.window.saved_combo.currentData(), selected)

    def test_project_switch_drops_review(self):
        self.load()
        other = self.base / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        self.window.select_project(str(other))
        self.wait(lambda: self.window.operation is None)
        self.assertEqual(self.window.root, other.resolve())
        self.assertIsNone(self.window.review)
        self.assertIsNone(self.window.plan)

    def test_corrupt_saved_state_does_not_auto_run(self):
        approved_plan.import_plan(self.root, self.source)
        (self.root / ".ai-dev/approved-plans/small-plan/state.json").write_text("{}")
        rows = saved_plans(self.root)
        self.assertTrue(rows[0]["error"])
        self.assertFalse(self.runner.calls)

    def test_html_in_plan_is_plain_user_data(self):
        self.plan["objective"] = '<img src="https://example.invalid/x">'
        self.source.write_text(json.dumps(self.plan))
        self.load()
        self.assertIn(self.plan["objective"], self.window.plan_view.toPlainText())
        self.assertFalse(self.runner.calls)

    def test_snapshot_does_not_claim_missing_usage_is_zero(self):
        self.imported()
        self.window.snapshot["missing_usage_records"] = 1
        self.window.snapshot["usage"] = []
        self.window._render()
        self.assertIn(self.window.t("unknown"), self.window.usage_label.text())
        self.assertIn(self.window.t("usage_partial"), self.window.usage_label.text())

    def test_buttons_and_footer_fit_languages_and_large_font(self):
        self.load()
        original = self.app.font()
        try:
            for scale in (1, 1.25):
                font = QFont(original)
                font.setPointSizeF(original.pointSizeF() * scale)
                self.app.setFont(font)
                for language in ("ru", "de", "en"):
                    self.window.set_language(language)
                    self.window.resize(860, 620)
                    self.app.processEvents()
                    QTest.qWait(30)
                    self.app.processEvents()
                    self.assertEqual(self.window.width(), 860, (language, scale))
                    for button in (
                        self.window.primary,
                        self.window.secondary,
                        self.window.back_button,
                        self.window.log_button,
                        *self.window.steps,
                    ):
                        if button.isVisible():
                            self.assertGreaterEqual(
                                button.width(),
                                button.minimumSizeHint().width(),
                                (language, scale, button.text()),
                            )
                            point = button.mapTo(
                                self.window, button.rect().bottomRight()
                            )
                            self.assertLess(point.y(), self.window.height())
                            self.assertLess(point.x(), self.window.width())
        finally:
            self.app.setFont(original)

    def test_demo_action_is_localized_focusable_and_fits_large_font(self):
        original = self.app.font()
        try:
            for language in ("ru", "de", "en"):
                font = QFont(original)
                font.setPointSizeF(original.pointSizeF() * 1.25)
                self.app.setFont(font)
                self.window.set_language(language)
                self.window.step = 0
                self.window._render()
                self.app.processEvents()
                self.assertEqual(
                    self.window.demo_button.accessibleName(),
                    self.window.t("demo_try"),
                )
                self.assertNotEqual(
                    self.window.demo_button.focusPolicy(),
                    Qt.FocusPolicy.NoFocus,
                )
                self.assertGreaterEqual(
                    self.window.demo_button.width(),
                    self.window.demo_button.minimumSizeHint().width(),
                    (language, self.window.demo_button.text()),
                )
                self.window.demo_button.setFocus()
                self.assertTrue(self.window.demo_button.hasFocus())
        finally:
            self.app.setFont(original)

    def test_stop_close_waits_and_does_not_close_early(self):
        self.imported()
        self.window._run_saved()
        with patch.object(self.window, "_dialog", return_value=True):
            self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window.close_after)
        self.runner.finish(130)
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())


from PySide6.QtGui import QFont


class SupportTests(unittest.TestCase):
    def test_demo_creation_uses_local_git_identity_preserves_existing_folder_and_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            occupied = parent / "MegaProg-Lernprojekt"
            occupied.mkdir()
            marker = occupied / "keep.txt"
            marker.write_text("user data", encoding="utf-8")
            global_name = subprocess.run(
                ["git", "config", "--global", "user.name"], capture_output=True, text=True
            ).stdout
            root, plan_path = create_demo_project(parent, "de")
            self.assertEqual(root.name, "MegaProg-Lernprojekt 2")
            self.assertEqual(marker.read_text(encoding="utf-8"), "user data")
            self.assertTrue(plan_path.is_file())
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["budget"]["max_model_turns"], 2)
            self.assertEqual({(task["model"], task["reasoning"]) for task in plan["tasks"]},
                             {("gpt-6-luna", "medium")})
            self.assertIn("Übungsprojekt", plan["objective"])
            self.assertIn("megaprog-tutorial", (
                root / "docs" / "CONTINUITY.md"
            ).read_text(encoding="utf-8"))
            self.assertIn("freigegebene Plan", (
                root / "docs" / "PRODUCT_ROADMAP.md"
            ).read_text(encoding="utf-8"))
            self.assertFalse((root / ".ai-dev").exists())
            self.assertEqual(subprocess.check_output(
                ["git", "-C", str(root), "status", "--porcelain"], text=True), "")
            self.assertEqual(subprocess.check_output(
                ["git", "-C", str(root), "remote"], text=True), "")
            self.assertEqual(subprocess.run(
                ["git", "config", "--global", "user.name"], capture_output=True, text=True
            ).stdout, global_name)

    def test_demo_creation_reports_missing_git_without_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("ai_dev.gui_support.shutil.which", return_value=None):
                with self.assertRaises(GuiError) as caught:
                    create_demo_project(folder, "en")
            self.assertEqual(caught.exception.key, "error_demo_git")
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_plain_folder_is_not_a_git_project_and_is_not_initialized(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(GuiError) as caught:
                project_root(d)
            self.assertEqual(caught.exception.key, "error_project_not_git")
            self.assertIn(str(Path(d).resolve()), str(caught.exception))
            self.assertIn("not a git repository", str(caught.exception))
            self.assertFalse((Path(d) / ".git").exists())

    def test_missing_folder_is_distinct_from_missing_git(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(GuiError) as caught:
                project_root(Path(d) / "missing")
            self.assertEqual(caught.exception.key, "error_project_folder")
            with patch("ai_dev.gui_support.shutil.which", return_value=None):
                with self.assertRaises(GuiError) as caught:
                    project_root(d)
            self.assertEqual(caught.exception.key, "error_project_git_missing")

    def test_git_error_keeps_stderr_and_permission_category(self):
        with tempfile.TemporaryDirectory() as d:
            for stderr, key in (
                ("fatal: Permission denied", "error_project_permission"),
                ("fatal: detected dubious ownership", "error_project_git"),
            ):
                result = subprocess.CompletedProcess("git", 128, "", stderr)
                with patch("ai_dev.gui_support.subprocess.run", return_value=result):
                    with self.assertRaises(GuiError) as caught:
                        project_root(d)
                self.assertEqual(caught.exception.key, key)
                self.assertIn(stderr, str(caught.exception))

    def test_project_timeout_explains_os_access_request(self):
        with tempfile.TemporaryDirectory() as d:
            with patch(
                "ai_dev.gui_support.subprocess.run",
                side_effect=subprocess.TimeoutExpired("git", 10),
            ):
                with self.assertRaises(GuiError) as caught:
                    project_root(d)
        self.assertEqual(caught.exception.key, "error_project_timeout")

    def test_preferences_preserve_unknown_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "prefs.json"
            p.write_text('{"future":42,"language":"ru"}')
            save_preferences(p, {"theme": "dark"})
            self.assertEqual(
                read_preferences(p), {"future": 42, "language": "ru", "theme": "dark"}
            )

    def test_status_needs_successful_proof(self):
        self.assertFalse(feature_verified({"status": "VERIFIED", "proof": None}))
        self.assertFalse(
            feature_verified(
                {"status": "VERIFIED", "proof": {"checks": [{"exit_code": 1}]}}
            )
        )
        self.assertTrue(
            feature_verified(
                {"status": "VERIFIED", "proof": {"checks": [{"exit_code": 0}]}}
            )
        )

    def test_known_failure_categories(self):
        for text, key in [
            ("Model turn budget exhausted", "error_budget"),
            ("Source changed outside the interrupted task", "error_drift"),
            ("Feature verification failed", "error_check"),
            ("Codex/ChatGPT login unavailable", "error_auth"),
            ("unsupported model", "error_model"),
        ]:
            self.assertEqual(reason_key(text), key)


class ProcessTests(unittest.TestCase):
    def test_cancellation_awaits_child_and_clears_busy(self):
        app = QApplication.instance() or QApplication([])

        class SleepRunner(CliRunner):
            def command(self, root, args):
                return [
                    sys.executable,
                    "-u",
                    "-c",
                    'import time; print("started",flush=True); time.sleep(30)',
                ]

        with tempfile.TemporaryDirectory() as d:
            runner = SleepRunner()
            done = []
            runner.finished.connect(lambda *args: done.append(args))
            runner.start("synthetic", Path(d), [])
            deadline = time.monotonic() + 5
            while not runner.output and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(10)
            self.assertIn("started", runner.output)
            runner.stop()
            self.assertTrue(runner.busy)
            while not done and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(10)
            self.assertTrue(done)
            self.assertFalse(runner.busy)
            self.assertNotEqual(done[0][1], 0)

    def test_failed_executable_clears_busy(self):
        app = QApplication.instance() or QApplication([])

        class MissingRunner(CliRunner):
            def command(self, root, args):
                return ["/definitely-missing-megaprog-test-executable"]

        with tempfile.TemporaryDirectory() as d:
            runner = MissingRunner()
            done = []
            runner.finished.connect(lambda *args: done.append(args))
            runner.start("synthetic", Path(d), [])
            deadline = time.monotonic() + 3
            while not done and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(10)
            self.assertTrue(done)
            self.assertFalse(runner.busy)


if __name__ == "__main__":
    unittest.main()
