"""Five-step desktop wizard over MegaProg's existing approved-plan engine."""

import html
import json
from pathlib import Path
import tempfile
import time
from PySide6.QtCore import Qt, QTimer, QTranslator, QLibraryInfo, QUrl
from PySide6.QtGui import QIcon, QDesktopServices, QFont, QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFrame,
    QLabel,
    QPushButton,
    QComboBox,
    QLineEdit,
    QCheckBox,
    QStackedWidget,
    QScrollArea,
    QPlainTextEdit,
    QTextBrowser,
    QProgressBar,
    QDialog,
    QFileDialog,
    QSizePolicy,
)
from .gui_text import (
    TEXT,
    LANGUAGE_LABELS,
    tr,
    preferred_language,
    _preferences_path,
    chatgpt_plan_prompt,
)
from .gui_support import (
    GuiError,
    ReviewedPlan,
    read_preferences,
    save_preferences,
    project_root,
    saved_plans,
    read_snapshot,
    effective_budget,
    reason_key,
    feature_verified,
    canonical_digest,
    project_busy,
    create_demo_project,
)
from .gui_process import CliRunner, Queries, last_json
from .gui_style import apply_theme, asset
from .version import __version__

STEP_KEYS = ("step_project", "step_connection", "step_plan", "step_run", "step_result")


class PathLabel(QLabel):
    """Elided display with complete, selectable value available by tooltip/copy."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.full = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_path(self, value):
        self.full = str(value)
        self.setToolTip(self.full)
        self.setAccessibleName(self.full)
        self._elide()

    def _elide(self):
        self.setText(
            self.fontMetrics().elidedText(
                self.full, Qt.TextElideMode.ElideMiddle, max(20, self.width())
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class DocumentView(QTextBrowser):
    """Let the page own scrolling; retain text selection and keyboard access."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setOpenLinks(False)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)

    def _fit(self, *_):
        height = max(70, int(self.document().size().height()) + 28)
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.document().setTextWidth(max(100, self.viewport().width()))
        self._fit()


class Window(QMainWindow):
    def __init__(self, preferences_path=None, runner=None):
        super().__init__()
        self.preferences_path = Path(preferences_path or _preferences_path())
        self.preferences = read_preferences(self.preferences_path)
        self.language = self.preferences.get("language", preferred_language())
        if self.language not in TEXT:
            self.language = "en"
        self.theme = self.preferences.get("theme", "system")
        if self.theme not in ("system", "dark", "light"):
            self.theme = "system"
        self.root = None
        self.project_selection = None
        self.review = None
        self.plan = None
        self.snapshot = None
        self.saved_id = None
        self.saved_rows = []
        self.step = 0
        self.doctor = None
        self.one_task = False
        self.objective = ""
        self.message_key = None
        self.message_values = {}
        self.failure_detail = ""
        self.log_text = ""
        self.started_at = None
        self.finished_elapsed = None
        self.last_line_at = None
        self.pending_run = False
        self.was_stopped = False
        self.close_after = False
        self.operation = None
        self.review_budget = None
        self.generation = 0
        self._temp = None
        self._snapshot_signature = None
        self.runner = runner or CliRunner(self)
        self.runner.line.connect(self._log)
        self.runner.finished.connect(self._command_done)
        self.runner.started.connect(self._command_started)
        self.queries = Queries(self)
        self.queries.complete.connect(self._query_done)
        self.translator = None
        self._qt_language()
        self._build()
        self.setMinimumSize(860, 620)
        size = self.preferences.get("window_size", [1120, 780])
        if (
            not isinstance(size, list)
            or len(size) != 2
            or any(type(n) is not int for n in size)
        ):
            size = [1120, 780]
        self.resize(max(860, min(size[0], 1800)), max(620, min(size[1], 1200)))
        self.setWindowTitle("MegaProg · " + __version__)
        self.setWindowIcon(QIcon(str(asset("megaprog.svg"))))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        QApplication.styleHints().colorSchemeChanged.connect(self._system_theme_changed)
        self._render()

    def t(self, key, **values):
        return tr(self.language, key, **values)

    def _label(self, text="", name=None):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if name:
            label.setObjectName(name)
        return label

    def _button(self, key, action, primary=False):
        button = QPushButton(self.t(key))
        button.clicked.connect(action)
        button.setProperty("primary", primary)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(self.t(key))
        return button

    def _card(self, title):
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(self._label(title, "cardTitle"))
        return card, layout

    def _page(self, title, hint):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 12, 16)
        layout.setSpacing(16)
        layout.addWidget(self._label(self.t(title), "heading"))
        layout.addWidget(self._label(self.t(hint), "muted"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack.addWidget(scroll)
        return layout

    def _build(self):
        old = self.takeCentralWidget()
        if old:
            old.deleteLater()
        shell = QWidget()
        shell.setObjectName("shell")
        self.setCentralWidget(shell)
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(28, 22, 28, 20)
        outer.setSpacing(16)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QIcon(str(asset("megaprog.svg"))).pixmap(48, 48))
        header.addWidget(icon)
        brand = QVBoxLayout()
        brand.setSpacing(2)
        brand.addWidget(self._label("MegaProg", "brand"))
        brand.addWidget(self._label(self.t("app_tagline"), "eyebrow"))
        header.addLayout(brand, 1)
        self.settings_button = self._button("settings", self._settings)
        header.addWidget(self.settings_button)
        header.addWidget(self._button("about", self._about))
        outer.addLayout(header)
        steps = QHBoxLayout()
        steps.setSpacing(8)
        self.steps = []
        for i, key in enumerate(STEP_KEYS):
            button = QPushButton(str(i + 1) + "  " + self.t(key))
            button.setObjectName("step")
            button.clicked.connect(
                lambda _checked=False, index=i: self._navigate(index)
            )
            self.steps.append(button)
            steps.addWidget(button, 1)
        outer.addLayout(steps)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self._project_page()
        self._connection_page()
        self._plan_page()
        self._run_page()
        self._result_page()
        self.notice = self._label()
        self.notice.setObjectName("notice")
        outer.addWidget(self.notice)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(150)
        self.log_box.document().setMaximumBlockCount(1200)
        self.log_box.setPlainText(self.log_text)
        self.log_box.hide()
        self.log_box.setAccessibleName(self.t("log"))
        outer.addWidget(self.log_box)
        self.footer = QGridLayout()
        self.footer.setSpacing(10)
        self._footer_compact = None
        self.back_button = self._button(
            "back", lambda: self._navigate(max(0, self.step - 1))
        )
        self.log_button = self._button("log", self._toggle_log)
        self.secondary = self._button("save_only", self._secondary)
        self.primary = self._button("next", self._primary, True)
        outer.addLayout(self.footer)
        self._layout_footer()
        apply_theme(QApplication.instance(), self.theme)

    def _layout_footer(self):
        compact = self.width() < 1050
        if self._footer_compact == compact:
            return
        self._footer_compact = compact
        for i, button in enumerate(self.steps):
            button.setText(
                str(i + 1) + ("\n" if compact else "  ") + self.t(STEP_KEYS[i])
            )
            button.setAccessibleName(str(i + 1) + " " + self.t(STEP_KEYS[i]))
        widgets = (self.back_button, self.log_button, self.secondary, self.primary)
        for widget in widgets:
            self.footer.removeWidget(widget)
        for col in range(5):
            self.footer.setColumnStretch(col, 0)
        if compact:
            self.footer.addWidget(self.log_button, 0, 0)
            self.footer.addWidget(self.secondary, 0, 2)
            self.footer.addWidget(self.back_button, 1, 0, Qt.AlignmentFlag.AlignLeft)
            self.footer.addWidget(self.primary, 1, 2)
            self.footer.setColumnStretch(1, 1)
        else:
            self.footer.addWidget(self.back_button, 0, 0)
            self.footer.addWidget(self.log_button, 0, 1)
            self.footer.addWidget(self.secondary, 0, 3)
            self.footer.addWidget(self.primary, 0, 4)
            self.footer.setColumnStretch(2, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "footer"):
            self._layout_footer()

    def _project_page(self):
        layout = self._page("project_heading", "project_hint")
        card, body = self._card(self.t("project"))
        self.project_path = PathLabel()
        body.addWidget(self.project_path)
        row = QHBoxLayout()
        self.choose_project_button = self._button("choose_folder", self._choose_project)
        row.addWidget(self.choose_project_button)
        self.copy_project_button = self._button(
            "copy_path",
            lambda: QApplication.clipboard().setText(
                str(self.project_selection or self.root or "")
            ),
        )
        row.addWidget(self.copy_project_button)
        row.addStretch()
        body.addLayout(row)
        self.project_error = self._label("", "error")
        body.addWidget(self.project_error)
        layout.addWidget(card)
        card, body = self._card(self.t("demo_title"))
        self.demo_hint = self._label(self.t("demo_hint"), "muted")
        body.addWidget(self.demo_hint)
        self.demo_button = self._button("demo_try", self._choose_demo)
        body.addWidget(self.demo_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        card, body = self._card(self.t("recent"))
        self.recent_card = card
        row = QHBoxLayout()
        self.recent = QComboBox()
        self.recent.setMinimumWidth(0)
        self.recent.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.recent.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.recent.setMinimumContentsLength(12)
        self.recent.activated.connect(
            lambda i: (
                self.select_project(self.recent.itemData(i))
                if self.recent.itemData(i)
                else None
            )
        )
        row.addWidget(self.recent, 1)
        row.addWidget(self._button("forget", self._forget))
        body.addLayout(row)
        layout.addWidget(card)
        card, body = self._card(self.t("saved_plans"))
        self.saved_combo = QComboBox()
        self.saved_combo.setMinimumContentsLength(12)
        self.saved_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.saved_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        body.addWidget(self.saved_combo)
        self.saved_note = self._label(self.t("saved_empty"), "muted")
        body.addWidget(self.saved_note)
        self.open_saved_button = self._button("open_saved", self._open_saved)
        body.addWidget(self.open_saved_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.saved_card = card
        layout.addWidget(card)
        layout.addStretch()

    def _connection_page(self):
        layout = self._page("connect_heading", "connect_hint")
        card, body = self._card("Codex")
        self.connection_status = self._label()
        body.addWidget(self.connection_status)
        self.connection_help = self._label(self.t("connection_help"), "muted")
        body.addWidget(self.connection_help)
        self.guide_button = self._button(
            "official_guide",
            lambda: QDesktopServices.openUrl(
                QUrl("https://developers.openai.com/codex/cli/")
            ),
        )
        body.addWidget(self.guide_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        layout.addStretch()

    def _plan_page(self):
        layout = self._page("plan_heading", "plan_hint")
        card, body = self._card(self.t("prepare_plan"))
        self.prepare_card = card
        self.objective_edit = QLineEdit(self.objective)
        self.objective_edit.setPlaceholderText(self.t("objective_hint"))
        self.objective_edit.setAccessibleName(self.t("objective"))
        self.objective_edit.textChanged.connect(
            lambda value: setattr(self, "objective", value)
        )
        body.addWidget(self.objective_edit)
        row = QHBoxLayout()
        self.prompt_button = self._button("copy_prompt", self._copy_prompt)
        row.addWidget(self.prompt_button)
        row.addStretch()
        body.addLayout(row)
        layout.addWidget(card)
        self.open_plan_button = self._button("open_plan", self._choose_plan)
        layout.addWidget(self.open_plan_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.plan_path = PathLabel()
        layout.addWidget(self.plan_path)
        self.plan_view = DocumentView()
        self.plan_view.setOpenExternalLinks(False)
        self.plan_view.setOpenLinks(False)
        self.plan_view.setAccessibleName(self.t("plan"))
        layout.addWidget(self.plan_view, 1)
        self.one_check = QCheckBox(self.t("one_short"))
        self.one_check.setChecked(self.one_task)
        self.one_check.toggled.connect(lambda v: setattr(self, "one_task", v))
        layout.addWidget(self.one_check)
        self.copy_commands = self._button("copy_commands", self._copy_checks)
        layout.addWidget(self.copy_commands, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._label(self.t("review_notice"), "muted"))

    def _run_page(self):
        layout = self._page("run_heading", "run_hint")
        card, body = self._card(self.t("current_task"))
        self.current_task = self._label()
        self.current_task.setStyleSheet("font-size: 22px; font-weight: 600;")
        body.addWidget(self.current_task)
        self.phase = self._label("", "muted")
        body.addWidget(self.phase)
        self.activity = QProgressBar()
        self.activity.setTextVisible(False)
        body.addWidget(self.activity)
        layout.addWidget(card)
        row = QHBoxLayout()
        self.turn_count = self._label()
        row.addWidget(self.turn_count, 1)
        self.elapsed_label = self._label("", "muted")
        self.elapsed_label.setWordWrap(False)
        row.addWidget(self.elapsed_label)
        layout.addLayout(row)
        self.last_event = self._label("", "muted")
        layout.addWidget(self.last_event)
        card, body = self._card(self.t("queue"))
        self.task_count = self._label()
        body.addWidget(self.task_count)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        body.addWidget(self.progress)
        self.queue_view = DocumentView()
        self.queue_view.setOpenLinks(False)
        body.addWidget(self.queue_view)
        layout.addWidget(card)
        layout.addStretch()

    def _result_page(self):
        layout = self._page("result_heading", "result_hint")
        self.result_banner = self._label("", "notice")
        layout.addWidget(self.result_banner)
        self.result_view = DocumentView()
        self.result_view.setOpenLinks(False)
        layout.addWidget(self.result_view, 1)
        self.usage_label = self._label("", "muted")
        layout.addWidget(self.usage_label)
        card, body = self._card(self.t("memory"))
        body.addWidget(self._label(self.t("memory_hint")))
        body.addWidget(self._label(self.t("export_help"), "muted"))
        layout.addWidget(card)

    def _qt_language(self):
        app = QApplication.instance()
        if self.translator:
            app.removeTranslator(self.translator)
        self.translator = QTranslator(self)
        self.translator.load(
            "qtbase_" + self.language,
            QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath),
        )
        app.installTranslator(self.translator)

    def set_language(self, value):
        if value not in TEXT:
            return
        self.language = value
        self._qt_language()
        self._preferences(language=value)
        self._build()
        self._render()

    def set_theme(self, value):
        self.theme = value
        self._preferences(theme=value)
        apply_theme(QApplication.instance(), value)

    def _system_theme_changed(self, *_):
        if self.theme == "system":
            apply_theme(QApplication.instance(), self.theme)

    def _preferences(self, **updates):
        self.preferences.update(updates)
        try:
            save_preferences(self.preferences_path, updates)
        except OSError as exc:
            self._message("error_preferences")
            self._log(str(exc))

    def _settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(self.t("settings"))
        box = QVBoxLayout(dialog)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(14)
        box.addWidget(self._label(self.t("language")))
        language = QComboBox()
        language.addItems(LANGUAGE_LABELS.values())
        language.setCurrentIndex(list(LANGUAGE_LABELS).index(self.language))
        box.addWidget(language)
        box.addWidget(self._label(self.t("theme")))
        theme = QComboBox()
        theme.addItems([self.t(k) for k in ("system", "light", "dark")])
        theme.setCurrentIndex(("system", "light", "dark").index(self.theme))
        box.addWidget(theme)
        row = QHBoxLayout()
        cancel = QPushButton(self.t("cancel"))
        cancel.clicked.connect(dialog.reject)
        row.addWidget(cancel)
        accept = QPushButton(self.t("save"))
        accept.setProperty("primary", True)
        accept.clicked.connect(dialog.accept)
        row.addWidget(accept)
        box.addLayout(row)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_theme(("system", "light", "dark")[theme.currentIndex()])
            self.set_language(list(LANGUAGE_LABELS)[language.currentIndex()])

    def _about(self):
        self._dialog(
            self.t("about"), "MegaProg " + __version__ + "\n\n" + self.t("about_body")
        )

    def _dialog(self, title, text, accept_key="ok_button", cancel_key=None):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(20)
        label = self._label(text)
        label.setMinimumWidth(410)
        label.setMaximumWidth(620)
        box.addWidget(label)
        row = QHBoxLayout()
        row.addStretch()
        if cancel_key:
            cancel = QPushButton(self.t(cancel_key))
            cancel.clicked.connect(dialog.reject)
            row.addWidget(cancel)
            cancel.setDefault(True)
        accept = QPushButton(self.t(accept_key))
        accept.setProperty("primary", True)
        accept.clicked.connect(dialog.accept)
        row.addWidget(accept)
        box.addLayout(row)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _file_dialog(self, title, directory=False, save=False):
        dialog = QFileDialog(self, title)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if directory:
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        elif save:
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        else:
            dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
            dialog.setNameFilters(["JSON (*.json)", self.t("all_files") + " (*)"])
        dialog.setLabelText(
            QFileDialog.DialogLabel.Accept, self.t("save" if save else "open")
        )
        dialog.setLabelText(QFileDialog.DialogLabel.Reject, self.t("cancel"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.selectedFiles()[0]
        return None

    def _choose_project(self):
        value = self._file_dialog(self.t("choose_folder"), directory=True)
        if value:
            self.select_project(value)

    def _choose_demo(self):
        if self.runner.busy or self.operation:
            return
        value = self._file_dialog(self.t("demo_location"), directory=True)
        if not value:
            return
        self.generation += 1
        self.operation = "demo"
        self._message("demo_preparing")
        self._render()
        self.queries.submit(
            "demo:" + str(self.generation), create_demo_project, value, self.language
        )

    def select_project(self, value):
        if self.runner.busy or self.operation:
            return
        self.project_selection = str(Path(value).expanduser().absolute())
        self.root = self.review = self.plan = self.snapshot = self.saved_id = None
        self.saved_rows = []
        self.doctor = None
        self.step = 0
        self.generation += 1
        self.operation = "project"
        self._message("loading")
        self._render()

        def read_project(path):
            root = project_root(path)
            return root, saved_plans(root)

        self.queries.submit(
            "project:" + str(self.generation),
            read_project,
            value,
        )

    def _forget(self):
        value = self.recent.currentData()
        if not value:
            return
        recent = [p for p in self.preferences.get("recent_projects", []) if p != value]
        self._preferences(recent_projects=recent)
        self._render()

    def _open_saved(self):
        value = self.saved_combo.currentData()
        if value and not self.runner.busy:
            self.generation += 1
            self.operation = "saved"
            self._render()
            self.queries.submit(
                "saved:" + str(self.generation), read_snapshot, self.root, value
            )

    def _choose_plan(self):
        value = self._file_dialog(self.t("open_plan"))
        if value:
            self.load_plan(value)

    def load_plan(self, path):
        if self.runner.busy or not self.root:
            return
        try:
            review = ReviewedPlan.read(self.root, path)
            budget = effective_budget(self.root, review.plan)
        except GuiError as exc:
            self.generation += 1
            self.review = self.plan = self.snapshot = self.saved_id = None
            self.review_budget = None
            self.step = 2
            self._failure(exc)
            return
        self.generation += 1
        self.review = review
        self.review_budget = budget
        self.plan = review.plan
        self.snapshot = None
        self.saved_id = None
        self.was_stopped = False
        self.step = 2
        self._message("reviewed")
        self._render()

    def _copy_prompt(self):
        QApplication.clipboard().setText(
            chatgpt_plan_prompt(
                self.language, self.objective or (self.plan or {}).get("objective", "")
            )
        )
        self._message("copied")

    def _copy_checks(self):
        if not self.plan:
            return
        groups = [
            {"feature": x["id"], "checks": x["checks"]} for x in self.plan["features"]
        ]
        groups += [{"task": x["id"], "checks": x["checks"]} for x in self.plan["tasks"]]
        QApplication.clipboard().setText(
            json.dumps(groups, ensure_ascii=False, indent=2)
        )
        self._message("copied_short")

    def _navigate(self, step):
        if self.runner.busy:
            if step not in (2, 3):
                return
        elif self.operation:
            return
        if step >= 1 and not self.root:
            return
        if step >= 3 and not self.saved_id:
            return
        self.step = step
        self._render()

    def _primary(self):
        if self.step == 0:
            self._navigate(1)
        elif self.step == 1:
            if self.doctor and self.doctor.get("ready"):
                self._navigate(2)
            else:
                self._start("doctor", ["doctor"])
        elif self.step == 2:
            self.approve(True)
        elif self.step == 3:
            if self.runner.busy:
                self._stop()
            else:
                self.step = 4
                self._render()
        elif self.step == 4:
            self._export()

    def _secondary(self):
        if self.step == 2:
            self.approve(False)
        elif self.step == 4:
            if self.snapshot and self.snapshot["status"] != "COMPLETED":
                self.step = 2
                self._render()
            else:
                self.review = None
                self.saved_id = None
                self.plan = None
                self.snapshot = None
                self.step = 2
                self._render()

    def approve(self, run):
        if self.runner.busy or self.operation or not self.plan:
            return
        try:
            current_budget = effective_budget(self.root, self.plan)
            if self.review:
                self.review.assert_current(self.root)
                if current_budget != self.review_budget:
                    raise GuiError("error_changed", "Configured budget changed")
            elif self.saved_id:
                fresh = read_snapshot(self.root, self.saved_id)
                if canonical_digest(fresh["plan"]) != canonical_digest(self.plan):
                    raise GuiError("error_changed")
            else:
                raise GuiError("error_plan")
            if run and not self._dialog(
                self.t("confirm_title"),
                self.t(
                    "confirm_details",
                    project=str(self.root),
                    plan=self.plan["objective"],
                    budget=current_budget,
                ),
                "approve_run",
                "cancel",
            ):
                return
            # Recheck after the modal review, then import the immutable bytes, not the mutable original path.
            if self.review:
                self.review.assert_current(self.root)
            elif self.saved_id:
                if canonical_digest(
                    read_snapshot(self.root, self.saved_id)["plan"]
                ) != canonical_digest(self.plan):
                    raise GuiError("error_changed")
            if effective_budget(self.root, self.plan) != current_budget:
                raise GuiError("error_changed", "Configured budget changed")
            self.pending_run = run
            self.was_stopped = False
            if self.review:
                self._temp = tempfile.TemporaryDirectory(prefix="megaprog-reviewed-")
                frozen = Path(self._temp.name) / "plan.json"
                frozen.write_bytes(self.review.raw)
                self._start(
                    "import", ["approved-plan", "import", str(frozen), "--json"]
                )
            elif run:
                self._run_saved()
        except (GuiError, OSError, ValueError) as exc:
            self._failure(
                exc if isinstance(exc, GuiError) else GuiError("error_launch", str(exc))
            )

    def _run_saved(self):
        if not self.saved_id:
            return
        state = (self.snapshot or {}).get("status", "READY")
        if state == "COMPLETED":
            self.step = 4
            self._render()
            return
        args = [
            "approved-plan",
            "run" if state == "READY" else "resume",
            self.saved_id,
            "--json",
        ]
        if self.one_task:
            args += ["--max-tasks", "1"]
        self.step = 3
        self.started_at = time.monotonic()
        self.finished_elapsed = None
        self.last_line_at = None
        self._start("run", args)

    def _start(self, kind, args):
        if self.runner.busy or not self.root:
            return
        self.message_key = None
        self.failure_detail = ""
        try:
            self.runner.start(kind, self.root, args)
        except (OSError, ValueError, RuntimeError) as exc:
            self.pending_run = False
            self._failure(GuiError("error_launch", str(exc)))
        self._render()

    def _command_started(self, kind):
        self._render()

    def _stop(self):
        self.pending_run = False
        self.was_stopped = True
        self.runner.stop()
        self._message("stopping")
        self._render()

    def _command_done(self, kind, code, output):
        data = last_json(output)
        if kind == "doctor":
            self.doctor = data if data and "ready" in data else {"ready": False}
            self._message("connected" if self.doctor.get("ready") else "not_connected")
        elif kind == "import":
            if code == 0 and data and data.get("plan_id") == self.plan["plan_id"]:
                self.saved_id = data["plan_id"]
                self.review = None
                self.snapshot = dict(data, plan=self.plan)
                self._message("saved")
                if self.pending_run and not self.was_stopped:
                    self.pending_run = False
                    self._run_saved()
            else:
                self.pending_run = False
                self._failure(GuiError("error_launch", output))
            if self._temp:
                self._temp.cleanup()
                self._temp = None
            self._refresh_saved_rows()
        elif kind == "run":
            self.finished_elapsed = (
                self.finished_elapsed
                if self.finished_elapsed is not None
                else (
                    int(time.monotonic() - self.started_at) if self.started_at else None
                )
            )
            self.pending_run = False
            self.step = 4
            self._message(
                "stopped_actual"
                if self.was_stopped
                else ("done" if code == 0 else "error_generic")
            )
            self._refresh_snapshot()
        elif kind == "handoff":
            if code == 0 and data and data.get("snapshot"):
                self._message("export_success", path=data.get("output", ""))
            else:
                self._failure(
                    GuiError(
                        (
                            "error_handoff_docs"
                            if "Required continuity document missing:" in output
                            else "error_export"
                        ),
                        output,
                    )
                )
        self._render()
        if self.close_after and not self.runner.busy:
            self.close()

    def _refresh_saved_rows(self):
        if self.root:
            self.queries.submit("rows:" + str(self.generation), saved_plans, self.root)

    def _refresh_snapshot(self):
        if self.root and self.saved_id:

            def read(root, pid):
                snapshot = read_snapshot(root, pid)
                snapshot["project_busy"] = project_busy(root)
                return snapshot

            self.queries.submit(
                "snapshot:" + str(self.generation), read, self.root, self.saved_id
            )

    def _query_done(self, key, value, error):
        kind, generation = key.split(":")
        if int(generation) != self.generation:
            return
        if kind in ("project", "saved", "demo"):
            self.operation = None
        if error:
            self._failure(
                error
                if isinstance(error, GuiError)
                else GuiError("error_saved", str(error))
            )
            return
        if kind == "project":
            self.root, self.saved_rows = value
            self.project_selection = str(self.root)
            self.review = None
            self.plan = None
            self.saved_id = None
            self.snapshot = None
            self.doctor = None
            self.review_budget = None
            self.failure_detail = ""
            self.message_key = None
            self.step = 0
            recent = [str(self.root)] + [
                p
                for p in self.preferences.get("recent_projects", [])
                if p != str(self.root)
            ]
            self._preferences(recent_projects=recent[:12])
            self._build()
        elif kind == "demo":
            self.root, plan_path = value
            self.project_selection = str(self.root)
            self.saved_rows = saved_plans(self.root)
            self.one_task = True
            self.review = self.snapshot = self.saved_id = None
            self.plan = None
            self.doctor = None
            self.review_budget = None
            self.failure_detail = ""
            self.message_key = None
            self.step = 0
            recent = [str(self.root)] + [
                p for p in self.preferences.get("recent_projects", [])
                if p != str(self.root)
            ]
            self._preferences(recent_projects=recent[:12])
            self._build()
            self.load_plan(plan_path)
        elif kind == "rows":
            self.saved_rows = value
        elif kind in ("saved", "snapshot"):
            if kind == "snapshot" and value.get("plan_id") != self.saved_id:
                return
            self.snapshot = value
            self.plan = value["plan"]
            self.saved_id = value["plan_id"]
            self.review = None
            if kind == "saved":
                self.was_stopped = False
                self.message_key = None
                self.failure_detail = ""
                self.step = (
                    2
                    if value["status"] == "READY"
                    else (3 if value.get("project_busy") else 4)
                )
            elif (
                not self.runner.busy
                and self.step == 3
                and value["status"] not in ("RUNNING", "VERIFYING")
            ):
                self.step = 4
        self._render()

    def _export(self):
        parent = self._file_dialog(self.t("export"), directory=True)
        if not parent:
            return
        destination = Path(parent) / (
            "MegaProg-handoff-" + time.strftime("%Y%m%d-%H%M%S")
        )
        self._start("handoff", ["handoff", "--output", str(destination)])

    def _log(self, text):
        self.log_text = (self.log_text + text)[-200000:]
        self.last_line_at = time.time()
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)
        self.log_box.insertPlainText(text)
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)

    def _toggle_log(self):
        visible = not self.log_box.isVisible()
        self.log_box.setVisible(visible)
        self.log_button.setText(self.t("hide_log" if visible else "log"))

    def _message(self, key, **values):
        self.message_key = key
        self.message_values = values
        if hasattr(self, "notice"):
            self.notice.setText(self.t(key, **values))
            self.notice.show()

    def _failure(self, exc):
        self.failure_detail = str(exc)
        self._message(exc.key, **exc.values)
        self._log(str(exc) + "\n")
        self._render()

    def _tick(self):
        if self.review and not self.runner.busy:
            try:
                self.review.assert_current(self.root)
            except GuiError as exc:
                self.review = None
                self.plan = None
                self._failure(exc)
        if self.saved_id and not self.operation:
            self._refresh_snapshot()
        self._render_time()

    def _render_time(self):
        elapsed = (
            self.finished_elapsed
            if self.finished_elapsed is not None
            else (int(time.monotonic() - self.started_at) if self.started_at else None)
        )
        value = (
            ("%02d:%02d" % (elapsed // 60, elapsed % 60))
            if elapsed is not None
            else self.t("unknown")
        )
        self.elapsed_label.setText(self.t("elapsed") + ": " + value)
        event = max(
            (self.snapshot or {}).get("last_event_at", 0), self.last_line_at or 0
        )
        self.last_event.setText(
            self.t("last_event")
            + ": "
            + (
                time.strftime("%H:%M:%S", time.localtime(event))
                if event
                else self.t("never")
            )
        )

    def _status(self, value):
        return (
            self.t("status_" + value)
            if "status_" + value in TEXT[self.language]
            else self.t("unknown")
        )

    def _plan_html(self):
        if not self.plan:
            return "<p>" + html.escape(self.t("plan_empty")) + "</p>"
        esc = html.escape
        p = self.plan
        try:
            budget = effective_budget(self.root, p)
        except GuiError:
            budget = p["budget"]["max_model_turns"]
        rows = [
            "<h2>" + esc(p["objective"]) + "</h2>",
            "<p>%s: %d · %s: %d · %s: %d</p>"
            % (
                self.t("features"),
                len(p["features"]),
                self.t("tasks"),
                len(p["tasks"]),
                self.t("budget"),
                budget,
            ),
        ]
        for feature in p["features"]:
            rows += [
                "<h3>" + esc(feature["title"]) + "</h3>",
                "<p><b>" + self.t("acceptance") + "</b></p>",
                "<ul>"
                + "".join("<li>" + esc(c) + "</li>" for c in feature["acceptance"])
                + "</ul>",
            ]
            rows += ["<p><b>" + self.t("commands") + "</b></p>"]
            rows += [
                '<p style="font-family:monospace">'
                + esc(json.dumps(c, ensure_ascii=False))
                + "</p>"
                for c in feature["checks"]
            ]
            for task in [t for t in p["tasks"] if t["feature_id"] == feature["id"]]:
                reasoning = task.get("reasoning", "medium")
                reasoning = self.t("medium") if reasoning == "medium" else reasoning
                rows += [
                    "<h4>" + esc(task["title"]) + "</h4>",
                    "<p>" + esc(task["instructions"]).replace("\n", "<br>") + "</p>",
                    "<p><b>"
                    + self.t("model")
                    + ":</b> "
                    + esc(task.get("model", "gpt-6-luna"))
                    + " · "
                    + esc(reasoning)
                    + "</p>",
                    "<p><b>"
                    + self.t("paths")
                    + ":</b> "
                    + esc(", ".join(task["allowed_paths"]))
                    + "</p>",
                    "<p><b>"
                    + self.t("dependencies")
                    + ":</b> "
                    + esc(", ".join(task.get("depends_on", [])) or "—")
                    + "</p>",
                ]
                rows += [
                    '<p style="font-family:monospace">'
                    + esc(json.dumps(c, ensure_ascii=False))
                    + "</p>"
                    for c in task["checks"]
                ]
        return "".join(rows)

    def _render_snapshot(self):
        data = self.snapshot or {}
        tasks = data.get("tasks", [])
        plan = self.plan or {"tasks": [], "features": []}
        titles = {t["id"]: t["title"] for t in plan["tasks"]}
        done = sum(t["status"] == "COMPLETED" for t in tasks)
        self.task_count.setText(self.t("completed_count", done=done, total=len(tasks)))
        self.progress.setRange(0, max(1, len(tasks)))
        self.progress.setValue(done)
        self.turn_count.setText(
            self.t(
                "turn_count",
                used=data.get("model_turns", 0),
                limit=data.get("model_turn_budget", self.review_budget or 0),
            )
        )
        active = data.get("current_task")
        self.current_task.setText(titles.get(active, self.t("unknown")))
        status = data.get("status", "READY")
        running = self.runner.busy and self.runner.kind == "run"
        interrupted = (
            status in ("RUNNING", "VERIFYING")
            and not running
            and not data.get("project_busy")
        )
        phase = (
            self.t("stopping")
            if self.runner.stopping and self.runner.busy
            else (
                self.t("preparing")
                if running and status in ("READY", "PAUSED", "PENDING")
                else (self.t("interrupted") if interrupted else self._status(status))
            )
        )
        if data.get("project_busy") and not running:
            phase = self.t("observing")
        self.phase.setText(phase)
        self.activity.setRange(0, 0 if running or data.get("project_busy") else 1)
        self.activity.setValue(0)
        esc = html.escape
        queue_html = "".join(
            "<p><b>"
            + esc(titles.get(t["id"], t["id"]))
            + "</b><br>"
            + esc(self._status(t["status"]))
            + "</p>"
            for t in tasks
        )
        if self.queue_view.property("source_html") != queue_html:
            self.queue_view.setHtml(queue_html)
            self.queue_view.setProperty("source_html", queue_html)
        rows = []
        for feature in data.get("features", []):
            rows += [
                "<h3>" + esc(feature["title"]) + "</h3>",
                "<p>"
                + esc(
                    self.t("verified")
                    if feature_verified(feature)
                    else self.t("not_verified")
                )
                + "</p>",
            ]
            for task in [t for t in tasks if t["feature_id"] == feature["id"]]:
                if task.get("changed_paths"):
                    rows += ["<p>" + esc(", ".join(task["changed_paths"])) + "</p>"]
                for check in task.get("checks", []):
                    rows += [
                        "<p>"
                        + esc(
                            self.t("check_pass")
                            if check.get("exit_code") == 0
                            else self.t("check_fail")
                        )
                        + ": "
                        + esc(json.dumps(check.get("argv", []), ensure_ascii=False))
                        + "<br>"
                        + esc(check.get("log", ""))
                        + "</p>"
                    ]
        result_html = "".join(rows) or "<p>" + self.t("no_plan") + "</p>"
        if self.result_view.property("source_html") != result_html:
            self.result_view.setHtml(result_html)
            self.result_view.setProperty("source_html", result_html)
        if data.get("reason") and status == "BLOCKED":
            banner = (
                self.t("reason")
                + ": "
                + self.t(reason_key(data["reason"]))
                + "\n"
                + self.t("next_action")
                + ": "
                + self.t("fix_then_resume")
            )
            self.result_banner.setToolTip(data["reason"])
            if getattr(self, "_logged_reason", None) != data["reason"]:
                self._log(data["reason"] + "\n")
                self._logged_reason = data["reason"]
        elif interrupted:
            banner = self.t("interrupted") + "\n" + self.t("interrupted_hint")
        elif data.get("project_busy") and not running:
            banner = self.t("observing") + "\n" + self.t("observing_hint")
        else:
            banner = (
                self.t("stopped_actual") if self.was_stopped else self._status(status)
            )
        self.result_banner.setText(banner)
        usage = data.get("usage_totals")
        self.usage_label.setText(
            self.t("usage")
            + ": "
            + (
                self.t(
                    "tokens",
                    input=usage["input_tokens"],
                    cached=usage["cached_input_tokens"],
                    output=usage["output_tokens"],
                )
                if usage and data.get("usage")
                else self.t("unknown")
            )
            + (
                "\n" + self.t("usage_partial")
                if data.get("missing_usage_records")
                else ""
            )
        )
        self._render_time()

    def _render(self):
        busy = self.runner.busy or bool(self.operation)
        data = self.snapshot or {}
        observing = data.get("project_busy") and not self.runner.busy
        self.stack.setCurrentIndex(self.step)
        for i, button in enumerate(self.steps):
            button.setProperty("active", i == self.step)
            button.style().unpolish(button)
            button.style().polish(button)
            button.setEnabled(
                not self.operation
                and (
                    (
                        not self.runner.busy
                        and (i == 0 or self.root is not None)
                        and (i < 3 or bool(self.saved_id))
                    )
                    or (self.runner.busy and i in (2, 3))
                )
            )
        self.project_path.set_path(self.project_selection or self.root or "—")
        self.copy_project_button.setEnabled(bool(self.project_selection or self.root))
        project_error = (self.message_key or "").startswith("error_project")
        self.project_error.setText(
            self.t(self.message_key, **self.message_values) if project_error else ""
        )
        self.project_error.setVisible(project_error)
        self.saved_card.setVisible(bool(self.root))
        self.recent_card.setVisible(bool(self.preferences.get("recent_projects")))
        recent_values = [
            p
            for p in self.preferences.get("recent_projects", [])[:12]
            if isinstance(p, str)
        ]
        recent_signature = (self.language, tuple(recent_values))
        if self.recent.property("signature") != recent_signature:
            selected = self.recent.currentData()
            self.recent.clear()
            self.recent.addItem(
                self.t("recent_choose" if recent_values else "recent_empty"), None
            )
            for value in recent_values:
                self.recent.addItem(Path(value).name, value)
                self.recent.setItemData(
                    self.recent.count() - 1, value, Qt.ItemDataRole.ToolTipRole
                )
            self.recent.setCurrentIndex(max(0, self.recent.findData(selected)))
            self.recent.setProperty("signature", recent_signature)
        self.choose_project_button.setEnabled(not busy)
        self.demo_button.setEnabled(not busy)
        self.recent.setEnabled(not busy)
        saved_signature = (self.language, json.dumps(self.saved_rows, sort_keys=True))
        if self.saved_combo.property("signature") != saved_signature:
            selected = self.saved_combo.currentData()
            self.saved_combo.clear()
            self.saved_combo.addItem(self.t("saved_plans") + "…", None)
            for row in self.saved_rows:
                self.saved_combo.addItem(
                    row["id"] + " · " + self._status(row["status"]), row["id"]
                )
                self.saved_combo.setItemData(
                    self.saved_combo.count() - 1,
                    row["title"],
                    Qt.ItemDataRole.ToolTipRole,
                )
            index = self.saved_combo.findData(selected)
            self.saved_combo.setCurrentIndex(max(0, index))
            self.saved_combo.setProperty("signature", saved_signature)
        self.saved_combo.setEnabled(not busy)
        self.open_saved_button.setEnabled(bool(self.saved_rows) and not busy)
        self.saved_note.setVisible(not self.saved_rows)
        connected = bool(self.doctor and self.doctor.get("ready"))
        self.connection_status.setText(
            self.t(
                "connected"
                if connected
                else ("not_connected" if self.doctor else "not_checked")
            )
        )
        self.connection_help.setVisible(not connected)
        self.guide_button.setVisible(not connected)
        self.prepare_card.setVisible(not bool(self.plan))
        self.open_plan_button.setEnabled(bool(self.root) and not busy)
        self.prompt_button.setEnabled(not busy)
        self.objective_edit.setEnabled(not busy)
        self.one_check.setEnabled(not busy and data.get("status") != "COMPLETED")
        self.plan_path.set_path(
            self.review.source
            if self.review
            else (
                self.root / ".ai-dev/approved-plans" / self.saved_id / "plan.json"
                if self.saved_id and self.root
                else ""
            )
        )
        signature = (
            self.language,
            canonical_digest(self.plan) if self.plan else None,
            self.review_budget,
            (self.snapshot or {}).get("model_turn_budget"),
        )
        if (
            getattr(self, "_plan_signature", None) != signature
            or not self.plan_view.toPlainText()
        ):
            self.plan_view.setHtml(self._plan_html())
            self._plan_signature = signature
        self.copy_commands.setEnabled(bool(self.plan))
        self.back_button.setEnabled(self.step > 0 and not busy)
        self.back_button.setVisible(self.step != 3)
        self.secondary.setVisible(self.step in (2, 4))
        self.secondary.setEnabled(bool(self.plan) and not busy and not observing)
        self.primary.setProperty("danger", self.step == 3 and self.runner.busy)
        self.primary.setProperty("primary", not (self.step == 3 and self.runner.busy))
        keys = {
            0: "next",
            1: "next" if connected else "check_connection",
            2: "approve_run",
            3: (
                "stopping"
                if self.runner.stopping and self.runner.busy
                else ("stop" if self.runner.busy else "step_result")
            ),
            4: "export",
        }
        self.primary.setText(self.t(keys[self.step]))
        self.primary.setAccessibleName(self.t(keys[self.step]))
        enabled = {
            0: bool(self.root) and not busy,
            1: bool(self.root) and not busy,
            2: bool(self.plan)
            and not busy
            and not observing
            and data.get("status") != "COMPLETED",
            3: not self.runner.stopping if self.runner.busy else bool(self.saved_id),
            4: bool(self.saved_id) and not busy and not observing,
        }
        self.primary.setEnabled(enabled[self.step])
        self.primary.style().unpolish(self.primary)
        self.primary.style().polish(self.primary)
        self.secondary.setText(
            self.t(
                "save_only"
                if self.step == 2
                else ("continue" if data.get("status") != "COMPLETED" else "new_plan")
            )
        )
        if self.step == 2 and self.saved_id:
            self.secondary.hide()
        self.notice.setVisible(bool(self.message_key) and not (self.step == 0 and project_error))
        if self.message_key:
            self.notice.setText(self.t(self.message_key, **self.message_values))
        self._render_snapshot()

    def closeEvent(self, event: QCloseEvent):
        if self.runner.busy:
            event.ignore()
            if not self.close_after and self._dialog(
                self.t("stop"), self.t("close_running"), "stop_close", "stay"
            ):
                self.close_after = True
                self._stop()
            return
        self.timer.stop()
        self.queries.pool.waitForDone(12000)
        self._preferences(window_size=[self.width(), self.height()])
        event.accept()


def main():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("MegaProg")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    font = app.font()
    font.setPointSizeF(14)
    app.setFont(font)
    window = Window()
    window.show()
    return app.exec()
