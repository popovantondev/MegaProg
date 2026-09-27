"""Read-only desktop models and atomic preferences. No Qt or executor imports."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile

from .approved_plan import MAX_PLAN_BYTES, validate_plan, load, summary


class GuiError(ValueError):
    def __init__(self, key, detail="", **values):
        super().__init__(detail)
        self.key = key
        self.values = values


def read_preferences(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(path, updates):
    path = Path(path)
    value = read_preferences(path)
    value.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".preferences-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def project_root(value):
    try:
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise GuiError(
                "error_project_folder", "Selected path is not a folder: " + str(root)
            )
        git = shutil.which("git")
        if not git:
            raise GuiError(
                "error_project_git_missing",
                "Git executable not found; selected folder: " + str(root),
            )
        result = subprocess.run(
            [git, "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            env=dict(os.environ, LC_ALL="C", GIT_TERMINAL_PROMPT="0"),
        )
        if result.returncode:
            detail = (
                "Selected folder: "
                + str(root)
                + "\nGit exit: "
                + str(result.returncode)
                + "\n"
                + result.stderr.strip()
            )
            reason = result.stderr.lower()
            key = (
                "error_project_not_git"
                if "not a git repository" in reason
                else (
                    "error_project_permission"
                    if any(
                        x in reason
                        for x in ("permission denied", "operation not permitted")
                    )
                    else "error_project_git"
                )
            )
            raise GuiError(key, detail)
        if not result.stdout.strip():
            raise GuiError(
                "error_project_git", "Git returned no root for: " + str(root)
            )
        actual = Path(result.stdout.strip()).resolve(strict=True)
        if actual != root:
            raise GuiError(
                "error_project_nested",
                "Selected folder: " + str(root) + "\nRepository root: " + str(actual),
                root=str(actual),
            )
        return root
    except GuiError:
        raise
    except PermissionError as exc:
        raise GuiError(
            "error_project_permission",
            "Selected folder: " + str(value) + "\n" + str(exc),
        ) from exc


    except (FileNotFoundError, NotADirectoryError) as exc:
        raise GuiError(
            "error_project_folder", "Selected folder: " + str(value) + "\n" + str(exc)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GuiError("error_project_timeout", str(exc)) from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise GuiError(
            "error_project_git", "Selected folder: " + str(value) + "\n" + str(exc)
        ) from exc


def _application_root():
    """Locate bundled example data both in a checkout and a frozen app."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parents[1]


def create_demo_project(destination_parent, language="en"):
    """Create an isolated, clean Git demo and return its root and plan path.

    All Git identity settings are scoped to the first commit. Existing folders
    are never selected as a destination; a numbered sibling is chosen instead.
    No model, MegaProg task, or plan-state directory is created here.
    """
    try:
        parent = Path(destination_parent).expanduser().resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise GuiError("error_demo_location", str(exc)) from exc
    if not parent.is_dir():
        raise GuiError("error_demo_location", "Destination is not a directory: " + str(parent))
    if not shutil.which("git"):
        raise GuiError("error_demo_git", "Git executable is required to create the tutorial baseline.")
    if not shutil.which("python3"):
        raise GuiError("error_demo_python", "python3 is required by the tutorial checks.")

    names = {
        "ru": "MegaProg-Учебный-проект",
        "de": "MegaProg-Lernprojekt",
        "en": "MegaProg-Learning-Project",
    }
    base_name = names.get(language, names["en"])
    destination = parent / base_name
    suffix = 2
    while destination.exists():
        destination = parent / (base_name + " " + str(suffix))
        suffix += 1

    example = _application_root() / "examples" / "two-features"
    source = example / "project"
    plan_source = example / "approved-plan.json"
    if not source.is_dir() or not plan_source.is_file():
        raise GuiError("error_demo_source", "Bundled tutorial files are incomplete: " + str(example))
    for path in source.rglob("*"):
        if path.is_symlink():
            raise GuiError("error_demo_source", "Tutorial source contains a symbolic link: " + str(path))

    localized = {
        "ru": {
            "objective": "Учебный проект: сложение и умножение с автоматическими проверками.",
            "features": ["Сложение чисел", "Умножение чисел"],
            "acceptance": ["Функция add(a, b) правильно складывает два числа.", "Функция multiply(a, b) правильно умножает два числа."],
            "tasks": ["Добавить сложение", "Добавить умножение"],
            "instructions": ["Реализуй только add(a, b) в src/calculator.py. Сохрани сигнатуру; multiply пока не меняй.", "Реализуй только multiply(a, b) в src/calculator.py. Не меняй add."],
            "readme": "# Учебный проект MegaProg\n\nНебольшой проект-калькулятор. В учебном плане две задачи: сложение и умножение. Каждая проверяется отдельным тестом.\n",
            "roadmap": "# План учебного проекта\n\n1. Реализовать add(a, b); проверить через tests/test_add.py.\n2. Реализовать multiply(a, b); проверить через tests/test_multiply.py.\n\nУтверждённый план хранит требования. Выполнение подтверждается сохранёнными результатами проверок.\n",
            "continuity": "# Продолжение учебного проекта\n\nОткройте в MegaProg сохранённый план megaprog-tutorial. Перед продолжением проверьте завершённые задачи и результаты проверок. Завершённую задачу повторно не запускайте. После остановки сохраните пакет для чата на странице результата.\n",
        },
        "de": {
            "objective": "Übungsprojekt: Addition und Multiplikation mit automatischen Tests.",
            "features": ["Zahlen addieren", "Zahlen multiplizieren"],
            "acceptance": ["add(a, b) addiert zwei Zahlen korrekt.", "multiply(a, b) multipliziert zwei Zahlen korrekt."],
            "tasks": ["Addition hinzufügen", "Multiplikation hinzufügen"],
            "instructions": ["Implementieren Sie nur add(a, b) in src/calculator.py. Signatur beibehalten; multiply nicht ändern.", "Implementieren Sie nur multiply(a, b) in src/calculator.py. add nicht ändern."],
            "readme": "# MegaProg-Übungsprojekt\n\nEin kleiner Taschenrechner. Der Übungsplan enthält zwei Aufgaben: Addition und Multiplikation. Jede wird mit einem eigenen Test geprüft.\n",
            "roadmap": "# Plan des Übungsprojekts\n\n1. add(a, b) implementieren; mit tests/test_add.py prüfen.\n2. multiply(a, b) implementieren; mit tests/test_multiply.py prüfen.\n\nDer freigegebene Plan enthält die Anforderungen. Nur gespeicherte Prüfergebnisse belegen den Abschluss.\n",
            "continuity": "# Übungsprojekt fortsetzen\n\nÖffnen Sie in MegaProg den gespeicherten Plan megaprog-tutorial. Prüfen Sie vor dem Fortsetzen abgeschlossene Aufgaben und ihre Tests. Eine abgeschlossene Aufgabe nicht erneut starten. Speichern Sie nach dem Anhalten das Paket für den Chat auf der Ergebnisseite.\n",
        },
        "en": {
            "objective": "Tutorial project: addition and multiplication with automatic checks.",
            "features": ["Add numbers", "Multiply numbers"],
            "acceptance": ["add(a, b) returns the correct sum of two numbers.", "multiply(a, b) returns the correct product of two numbers."],
            "tasks": ["Add addition", "Add multiplication"],
            "instructions": ["Implement only add(a, b) in src/calculator.py. Keep its signature; do not change multiply yet.", "Implement only multiply(a, b) in src/calculator.py. Leave add unchanged."],
            "readme": "# MegaProg tutorial project\n\nA small calculator project. The tutorial plan has two tasks: addition and multiplication. Each has its own test.\n",
            "roadmap": "# Tutorial project roadmap\n\n1. Implement add(a, b); verify with tests/test_add.py.\n2. Implement multiply(a, b); verify with tests/test_multiply.py.\n\nThe approved plan stores the requirements. Only saved verification results establish completion.\n",
            "continuity": "# Continue the tutorial\n\nOpen the saved megaprog-tutorial plan in MegaProg. Inspect completed tasks and their checks before continuing. Do not restart a completed task. After execution stops, save a chat package from the result page.\n",
        },
    }[language if language in ("ru", "de", "en") else "en"]

    try:
        with tempfile.TemporaryDirectory(prefix=".megaprog-demo-", dir=str(parent)) as temp:
            staged = Path(temp) / "project"
            shutil.copytree(source, staged)
            plan = json.loads(plan_source.read_text(encoding="utf-8"))
            plan["plan_id"] = "megaprog-tutorial"
            plan["objective"] = localized["objective"]
            for index, feature in enumerate(plan["features"]):
                feature["title"] = localized["features"][index]
                feature["acceptance"] = [localized["acceptance"][index]]
            for index, task in enumerate(plan["tasks"]):
                task["title"] = localized["tasks"][index]
                task["instructions"] = localized["instructions"][index]
                task["model"] = "gpt-6-luna"
                task["reasoning"] = "medium"
            (staged / "README.md").write_text(localized["readme"], encoding="utf-8")
            (staged / "docs" / "PRODUCT_ROADMAP.md").write_text(
                localized["roadmap"], encoding="utf-8"
            )
            (staged / "docs" / "CONTINUITY.md").write_text(
                localized["continuity"], encoding="utf-8"
            )
            (staged / "approved-plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            subprocess.run([shutil.which("git"), "init", "-q"], cwd=staged, check=True,
                           capture_output=True, text=True, timeout=20)
            subprocess.run([shutil.which("git"), "add", "--all"], cwd=staged, check=True,
                           capture_output=True, text=True, timeout=20)
            subprocess.run(
                [shutil.which("git"), "-c", "user.name=MegaProg Tutorial",
                 "-c", "user.email=tutorial@example.invalid", "commit", "-qm",
                 "Create clean MegaProg tutorial baseline"], cwd=staged, check=True,
                capture_output=True, text=True, timeout=20,
            )
            # mkdir reserves a new sibling atomically; existing user folders are never targets.
            while True:
                try:
                    destination.mkdir()
                    break
                except FileExistsError:
                    destination = parent / (base_name + " " + str(suffix))
                    suffix += 1
            try:
                for child in staged.iterdir():
                    shutil.move(str(child), str(destination / child.name))
            except Exception:
                # This path was created by this operation and was never populated before.
                shutil.rmtree(destination, ignore_errors=True)
                raise
    except GuiError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise GuiError("error_demo_create", str(exc)) from exc
    return destination.resolve(), destination / "approved-plan.json"
def canonical_digest(plan):
    return hashlib.sha256(
        json.dumps(
            plan, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ReviewedPlan:
    root: Path
    source: Path
    raw: bytes
    digest: str
    plan: dict

    @classmethod
    def read(cls, root, source):
        try:
            source = Path(source).expanduser().resolve(strict=True)
            with source.open("rb") as stream:
                raw = stream.read(MAX_PLAN_BYTES + 1)
            if len(raw) > MAX_PLAN_BYTES:
                raise ValueError("Maximum plan size is 128 KiB")
            plan = validate_plan(json.loads(raw.decode("utf-8")))
            return cls(
                Path(root).resolve(), source, raw, hashlib.sha256(raw).hexdigest(), plan
            )
        except (OSError, ValueError, UnicodeError) as exc:
            raise GuiError("error_plan", str(exc)) from exc

    def assert_current(self, root):
        try:
            with self.source.open("rb") as stream:
                raw = stream.read(MAX_PLAN_BYTES + 1)
            if (
                Path(root).resolve() != self.root
                or hashlib.sha256(raw).hexdigest() != self.digest
            ):
                raise ValueError("Reviewed plan source or project changed")
        except (OSError, ValueError) as exc:
            raise GuiError("error_changed", str(exc)) from exc


def saved_plans(root):
    base = Path(root) / ".ai-dev/approved-plans"
    rows = []
    if not base.is_dir():
        return rows
    for folder in sorted(base.iterdir(), key=lambda p: p.name):
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            plan, state = load(root, folder.name)
            rows.append(
                {
                    "id": folder.name,
                    "title": plan["objective"],
                    "status": state["status"],
                    "error": None,
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rows.append(
                {
                    "id": folder.name,
                    "title": folder.name,
                    "status": "BLOCKED",
                    "error": str(exc),
                }
            )
    return rows


def read_snapshot(root, plan_id):
    try:
        plan, state = load(root, plan_id)
        data = summary(root, plan_id)
        data["plan"] = plan
        data["project_busy"] = project_busy(root)
        # Event mtime is not substituted with polling time.
        base = Path(root) / ".ai-dev/approved-plans" / plan_id
        paths = [base / "events.jsonl"]
        for item in state.get("tasks", {}).values():
            if item.get("model_log"):
                candidate = (Path(root) / item["model_log"]).resolve()
                if candidate.is_relative_to(base.resolve()):
                    paths.append(candidate)
        data["last_event_at"] = max(
            [p.stat().st_mtime for p in paths if p.is_file()]
            + [state.get("updated_at", 0)]
        )
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GuiError("error_saved", str(exc)) from exc


def effective_budget(root, plan):
    try:
        cfg = json.loads((Path(root) / ".ai-dev/config.json").read_text())
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError) as exc:
        raise GuiError("error_saved", str(exc)) from exc
    cap = cfg.get("max_model_turns")
    if cap is not None and (type(cap) is not int or cap < 1):
        raise GuiError("error_plan", "Invalid configured turn budget")
    return min(
        plan["budget"]["max_model_turns"], cap or plan["budget"]["max_model_turns"]
    )


def reason_key(reason):
    value = (reason or "").lower()
    if any(
        s in value
        for s in (
            "budget",
            "max_attempts",
            "turn cap",
            "model turn limit",
            "model-turn budget",
        )
    ):
        return "error_budget"
    if any(
        s in value
        for s in ("source changed", "outside", "external", "uncommitted", "dirty")
    ):
        return "error_drift"
    if any(
        s in value for s in ("codex/chatgpt", "login", "not ready", "authorization")
    ):
        return "error_auth"
    if any(
        s in value
        for s in (
            "unsupported model",
            "model unavailable",
            "model not",
            "reasoning",
            "catalog",
        )
    ):
        return "error_model"
    if any(s in value for s in ("verification", "check failed", "checks failed")):
        return "error_check"
    return "error_generic"


def feature_verified(feature):
    proof = feature.get("proof")
    return (
        feature.get("status") == "VERIFIED"
        and isinstance(proof, dict)
        and bool(proof.get("checks"))
        and all(
            isinstance(c, dict) and c.get("exit_code") == 0 for c in proof["checks"]
        )
    )


def project_busy(root):
    """Probe an existing macOS lock without creating or modifying project files."""
    path = Path(root) / ".ai-dev/lock"
    if not path.exists():
        return False
    if os.name == "nt":
        return False  # This Preview targets macOS.
    import fcntl

    try:
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
    except OSError:
        return True  # Inability to check is not permission to start.
    return False
