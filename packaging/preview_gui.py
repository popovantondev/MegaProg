"""Render synthetic wizard states without invoking the CLI, Codex or model APIs."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from ai_dev.gui_window import Window


class PreviewRunner(QObject):
    line = Signal(str)
    finished = Signal(str, int, str)
    started = Signal(str)
    busy = False
    kind = None
    stopping = False

    def start(self, *_):
        raise RuntimeError("Synthetic preview cannot execute commands")

    def stop(self):
        raise RuntimeError("Synthetic preview cannot execute commands")


def fixture(language="ru"):
    titles = {
        "ru": (
            "Поиск и экспорт отчёта",
            "Поиск записей",
            "Экспорт отчёта",
            "Поиск находит запись по названию",
            "Отчёт сохраняется в выбранную папку",
            "Добавить поиск по названию",
            "Добавить экспорт отчёта",
        ),
        "de": (
            "Suche und Berichtsexport",
            "Einträge durchsuchen",
            "Bericht exportieren",
            "Die Suche findet einen Eintrag anhand seines Namens",
            "Der Bericht wird im gewählten Ordner gespeichert",
            "Suche nach Eintragsnamen ergänzen",
            "Export des vollständigen Berichts ergänzen",
        ),
        "en": (
            "Search and report export",
            "Find records",
            "Export report",
            "Search finds a record by its name",
            "The report is saved to the selected folder",
            "Add search by record name",
            "Add report export",
        ),
    }[language]
    objective, f1, f2, a1, a2, t1, t2 = titles
    return {
        "schema_version": 1,
        "plan_id": "demo-plan",
        "objective": objective,
        "budget": {"max_model_turns": 2},
        "features": [
            {
                "id": "search",
                "title": f1,
                "acceptance": [a1],
                "checks": [["python3", "-m", "unittest", "tests.test_search"]],
            },
            {
                "id": "export",
                "title": f2,
                "acceptance": [a2],
                "checks": [["python3", "-m", "unittest", "tests.test_export"]],
            },
        ],
        "tasks": [
            {
                "id": "search-task",
                "feature_id": "search",
                "title": t1,
                "instructions": a1,
                "depends_on": [],
                "allowed_paths": ["src/search.py", "tests/test_search.py"],
                "checks": [["python3", "-m", "unittest", "tests.test_search"]],
            },
            {
                "id": "export-task",
                "feature_id": "export",
                "title": t2,
                "instructions": a2,
                "depends_on": ["search-task"],
                "allowed_paths": ["src/export.py", "tests/test_export.py"],
                "checks": [["python3", "-m", "unittest", "tests.test_export"]],
            },
        ],
    }


def snapshot(plan, state="PAUSED"):
    complete = state == "COMPLETED"
    check = lambda task: {
        "argv": task["checks"][0],
        "exit_code": 0,
        "log": ".ai-dev/approved-plans/demo-plan/logs/" + task["id"] + ".log",
    }
    return {
        "plan_id": plan["plan_id"],
        "plan": plan,
        "status": state,
        "reason": "Model turn budget exhausted" if state == "BLOCKED" else None,
        "current_task": None if complete else "export-task",
        "remaining_tasks": 0 if complete else 1,
        "tasks": [
            {
                "id": t["id"],
                "feature_id": t["feature_id"],
                "status": (
                    "COMPLETED"
                    if complete or i == 0
                    else ("RUNNING" if state == "RUNNING" else "PENDING")
                ),
                "attempts": 1 if i == 0 or complete else 0,
                "session_id": None,
                "changed_paths": t["allowed_paths"] if complete or i == 0 else [],
                "checks": [check(t)] if complete or i == 0 else [],
            }
            for i, t in enumerate(plan["tasks"])
        ],
        "features": [
            {
                "id": f["id"],
                "title": f["title"],
                "status": "VERIFIED" if complete or i == 0 else "PENDING",
                "proof": (
                    {"checks": [check(plan["tasks"][i])]}
                    if complete or i == 0
                    else None
                ),
            }
            for i, f in enumerate(plan["features"])
        ],
        "model_turns": 2 if complete else 1,
        "model_turn_budget": 2,
        "usage": [
            {
                "task_id": "search-task",
                "usage": {
                    "input_tokens": 1240,
                    "cached_input_tokens": 800,
                    "output_tokens": 240,
                },
            }
        ],
        "usage_totals": {
            "input_tokens": 1240,
            "cached_input_tokens": 800,
            "output_tokens": 240,
            "reasoning_output_tokens": 0,
        },
        "missing_usage_records": 0,
        "updated_at": time.time(),
        "last_event_at": time.time(),
        "project_busy": state == "RUNNING",
    }


def render(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    font = app.font()
    font.setPointSizeF(14)
    app.setFont(font)
    entries = []
    with tempfile.TemporaryDirectory() as temp:
        for lang in ("ru", "de", "en"):
            w = Window(Path(temp) / "preferences.json", runner=PreviewRunner())
            w.timer.stop()
            w.set_language(lang)
            w.root = Path("/Example Projects/Notes")
            w.plan = fixture(lang)
            w.saved_id = "demo-plan"
            w.saved_rows = [
                {
                    "id": "demo-plan",
                    "title": w.plan["objective"],
                    "status": "PAUSED",
                    "error": None,
                }
            ]
            w.doctor = {"ready": True}
            w.objective = w.plan["objective"]
            w.review_budget = 2
            for theme in ("light", "dark"):
                w.set_theme(theme)
                for width, height in ((1120, 780), (860, 620)):
                    w.resize(width, height)
                    w.show()
                    for name, step, state in (
                        ("project", 0, "READY"),
                        ("connection", 1, "READY"),
                        ("plan", 2, "READY"),
                        ("run", 3, "RUNNING"),
                        ("result", 4, "COMPLETED"),
                        ("blocked", 4, "BLOCKED"),
                        ("stopped", 4, "PAUSED"),
                    ):
                        w.runner.busy = name == "run"
                        w.runner.kind = "run" if name == "run" else None
                        w.snapshot = snapshot(w.plan, state)
                        w.was_stopped = name == "stopped"
                        w.step = step
                        w.message_key = None
                        w._render()
                        app.processEvents()
                        filename = f"{lang}-{theme}-{width}-{name}.png"
                        w.grab().save(str(output / filename))
                        entries.append(
                            {
                                "file": filename,
                                "language": lang,
                                "theme": theme,
                                "width": width,
                                "state": name,
                            }
                        )
            w.runner.busy = False
            w.close()
            app.processEvents()
    (output / "index.html").write_text(
        """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MegaProg — мастер</title><style>body{font:16px/1.6 system-ui;background:#f3f6fb;color:#14253d;padding:24px}header{max-width:1000px;margin:0 auto;padding:30px 36px;border-radius:24px;background:linear-gradient(125deg,#102c55,#07172e);color:#fff;box-shadow:0 16px 38px #0b2b551f}header p{color:#e2edff}nav{position:sticky;top:0;z-index:2;max-width:1000px;margin:0 auto;background:#f3f6fb;padding:14px 0}button{padding:8px 14px;margin:4px;border:1px solid #dbe4f0;border-radius:999px;background:#fff;color:#14253d;cursor:pointer}button:hover{background:#e6f0ff;border-color:#b8d3ff;color:#075dd1}article{max-width:1000px;margin:16px auto;padding:26px 30px;background:#fff;border:1px solid #dbe4f0;border-radius:18px;box-shadow:0 5px 16px #0b2b5507}img{max-width:100%;height:auto;border-radius:12px;border:1px solid #dbe4f0}small{color:#607087}</style><header><h1>MegaProg — новый мастер</h1><p>Пять шагов, обе темы и три языка. Все данные демонстрационные; исполнители не запускались.</p></header><nav id="filters"></nav><main id="main"></main><script>const entries="""
        + json.dumps(entries)
        + """;let lang='ru',theme='dark',width=1120;function draw(){document.getElementById('filters').innerHTML=['ru','de','en'].map(x=>`<button onclick="lang='${x}';draw()">${x.toUpperCase()}</button>`).join('')+['dark','light'].map(x=>`<button onclick="theme='${x}';draw()">${x==='dark'?'Тёмная':'Светлая'}</button>`).join('')+[1120,860].map(x=>`<button onclick="width=${x};draw()">${x} px</button>`).join('');document.getElementById('main').innerHTML=entries.filter(x=>x.language===lang&&x.theme===theme&&x.width===width).map(x=>`<article><h2>${x.state}</h2><img src="${x.file}"><br><small>${x.file}</small></article>`).join('')}draw()</script></html>""",
        encoding="utf-8",
    )
    print(
        json.dumps({"screenshots": len(entries), "index": str(output / "index.html")})
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    render(parser.parse_args().output)
