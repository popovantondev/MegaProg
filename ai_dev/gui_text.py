"""User-facing strings and plan prompt, independent of Qt."""

import json
import locale
from pathlib import Path

TEXT = {
    "ru": {
        "title": "MegaProg — утверждённый план",
        "tagline": "Сначала согласуйте план. MegaProg выполнит задачи и проверит результат.",
        "language": "Язык",
        "project": "Папка проекта",
        "choose_project": "Выбрать…",
        "plan": "Файл плана",
        "choose_plan": "Открыть план…",
        "copy_prompt": "Скопировать инструкцию для ChatGPT",
        "load": "Проверить и показать план",
        "approve": "Утвердить и сохранить",
        "run": "Выполнить план",
        "one_task": "Выполнить одну задачу и остановиться",
        "resume": "Продолжить сохранённый план",
        "doctor": "Проверить Codex",
        "handoff": "Экспортировать handoff…",
        "stop": "Остановить",
        "yes": "Да",
        "no": "Нет",
        "ok_button": "ОК",
        "preview": "План и команды проверок будут показаны здесь. Их чтение ничего не запускает.",
        "status": "Готово",
        "busy": "Проверка или выполнение…",
        "ok": "План сохранён. Проверьте команды ниже перед запуском.",
        "invalid": "План не подходит",
        "need_project": "Сначала выберите корневую папку Git-проекта.",
        "need_plan": "Сначала откройте и проверьте JSON-план.",
        "confirm": "Утвердить этот план и сохранить его в выбранном проекте?",
        "confirm_run": "Начать выполнять утверждённый план? Будут запущены Codex и показанные команды проверок.",
        "copied": "Инструкция скопирована. Вставьте её в ChatGPT, согласуйте план и сохраните ответ как JSON.",
        "doctor_ok": "Codex подключён. Можно открывать утверждённый план.",
        "doctor_bad": "MegaProg пока не может подключиться к Codex. Установите Codex CLI по официальной инструкции OpenAI: developers.openai.com/codex/cli. Затем откройте Терминал, выполните codex и выберите вход через ChatGPT. После входа нажмите «Проверить Codex» ещё раз. Отдельный API-ключ не нужен. Подробности показаны в журнале ниже.",
        "handoff_done": "Handoff сохранён в выбранной папке. Проверьте его перед прикреплением к чату.",
        "no_plan": "В проекте пока нет сохранённых планов.",
        "stopped": "Остановлено. Сохранённое состояние можно продолжить.",
        "done": "Действие завершено.",
        "failed": "Действие остановлено. Причина указана в журнале ниже.",
        "features": "Функции",
        "tasks": "Задачи",
        "budget": "Лимит ходов модели",
        "checks": "Проверки",
        "paths": "Разрешённые файлы",
        "invalid_git": "Выбранная папка должна быть корнем Git-проекта.",
        "file": "План",
        "open_handoff": "Выберите новую папку для файлов handoff.",
        "safety": "Команды проверок запускаются на этом Mac от вашей учётной записи. Одобряйте планы и команды только из источников, которым доверяете.",
    },
    "de": {
        "title": "MegaProg — freigegebener Plan",
        "tagline": "Plan prüfen. MegaProg führt Aufgaben aus und kontrolliert das Ergebnis.",
        "language": "Sprache",
        "project": "Projektordner",
        "choose_project": "Auswählen…",
        "plan": "Plan-Datei",
        "choose_plan": "Plan öffnen…",
        "copy_prompt": "Anleitung für ChatGPT kopieren",
        "load": "Plan prüfen und anzeigen",
        "approve": "Freigeben und speichern",
        "run": "Plan ausführen",
        "one_task": "Eine Aufgabe ausführen und anhalten",
        "resume": "Gespeicherten Plan fortsetzen",
        "doctor": "Codex prüfen",
        "handoff": "Handoff exportieren…",
        "stop": "Anhalten",
        "yes": "Ja",
        "no": "Nein",
        "ok_button": "OK",
        "preview": "Plan und Prüfbefehle erscheinen hier. Das Öffnen startet nichts.",
        "status": "Bereit",
        "busy": "Prüfung oder Ausführung läuft…",
        "ok": "Plan gespeichert. Prüfen Sie die Befehle vor dem Start.",
        "invalid": "Ungültiger Plan",
        "need_project": "Wählen Sie zuerst den Git-Projektstammordner.",
        "need_plan": "Öffnen und prüfen Sie zuerst einen JSON-Plan.",
        "confirm": "Diesen Plan freigeben und im Projekt speichern?",
        "confirm_run": "Freigegebenen Plan starten? Codex und die angezeigten Prüfungen werden ausgeführt.",
        "copied": "Anleitung kopiert. In ChatGPT einfügen, Plan abstimmen und die Antwort als JSON speichern.",
        "doctor_ok": "Codex ist verbunden. Sie können den freigegebenen Plan öffnen.",
        "doctor_bad": "MegaProg kann Codex noch nicht erreichen. Installieren Sie die Codex CLI nach der offiziellen OpenAI-Anleitung: developers.openai.com/codex/cli. Öffnen Sie dann das Terminal, starten Sie codex und wählen Sie die Anmeldung mit ChatGPT. Klicken Sie danach erneut auf „Codex prüfen“. Ein separater API-Schlüssel ist nicht nötig. Details stehen im Protokoll unten.",
        "handoff_done": "Handoff gespeichert. Vor dem Anhängen an einen Chat bitte prüfen.",
        "no_plan": "In diesem Projekt sind noch keine Pläne gespeichert.",
        "stopped": "Angehalten. Der gespeicherte Plan kann fortgesetzt werden.",
        "done": "Aktion abgeschlossen.",
        "failed": "Aktion angehalten. Die Ursache steht im Protokoll.",
        "features": "Funktionen",
        "tasks": "Aufgaben",
        "budget": "Modellrunden-Limit",
        "checks": "Prüfungen",
        "paths": "Freigegebene Dateien",
        "invalid_git": "Der ausgewählte Ordner muss der Git-Projektstamm sein.",
        "file": "Plan",
        "open_handoff": "Wählen Sie einen neuen Ordner für die Handoff-Dateien.",
        "safety": "Prüfbefehle laufen auf diesem Mac mit Ihrem Benutzerkonto. Geben Sie nur Pläne und Befehle aus vertrauenswürdigen Quellen frei.",
    },
    "en": {
        "title": "MegaProg — approved plan",
        "tagline": "Review a plan first. MegaProg runs tasks and checks the results.",
        "language": "Language",
        "project": "Project folder",
        "choose_project": "Choose…",
        "plan": "Plan file",
        "choose_plan": "Open plan…",
        "copy_prompt": "Copy ChatGPT instructions",
        "load": "Check and preview plan",
        "approve": "Approve and save",
        "run": "Run plan",
        "one_task": "Run one task and pause",
        "resume": "Continue saved plan",
        "doctor": "Check Codex",
        "handoff": "Export handoff…",
        "stop": "Stop",
        "yes": "Yes",
        "no": "No",
        "ok_button": "OK",
        "preview": "The plan and check commands appear here. Opening a file does not run anything.",
        "status": "Ready",
        "busy": "Checking or running…",
        "ok": "Plan saved. Review the commands below before running.",
        "invalid": "Invalid plan",
        "need_project": "Choose the Git project root folder first.",
        "need_plan": "Open and review a JSON plan first.",
        "confirm": "Approve this plan and save it in the selected project?",
        "confirm_run": "Run the approved plan? This starts Codex and the displayed check commands.",
        "copied": "Instructions copied. Paste them into ChatGPT, agree on a plan, and save its reply as JSON.",
        "doctor_ok": "Codex is connected. You can open an approved plan.",
        "doctor_bad": "MegaProg cannot reach Codex yet. Install Codex CLI using the official OpenAI instructions at developers.openai.com/codex/cli. Then open Terminal, run codex, and choose Sign in with ChatGPT. After signing in, select “Check Codex” again. No separate API key is needed. Check details are shown in the log below.",
        "handoff_done": "Handoff saved. Review it before attaching it to a chat.",
        "no_plan": "This project has no saved plans yet.",
        "stopped": "Paused. The saved plan can be continued.",
        "done": "Action completed.",
        "failed": "Action stopped. The reason is shown in the log below.",
        "features": "Features",
        "tasks": "Tasks",
        "budget": "Model-turn limit",
        "checks": "Checks",
        "paths": "Allowed files",
        "invalid_git": "The selected folder must be the Git project root.",
        "file": "Plan",
        "open_handoff": "Choose a new folder for the handoff files.",
        "safety": "Check commands run on this Mac as your user. Approve plans and commands only from sources you trust.",
    },
}

LANGUAGE_LABELS = {"ru": "Русский", "de": "Deutsch", "en": "English"}


def _doctor_ready(output):
    """Read the JSON readiness flag printed by the local, non-model doctor."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(output[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("ready"), bool):
            return value["ready"]
    return None


def preferred_language():
    try:
        value = json.loads(_preferences_path().read_text(encoding="utf-8")).get(
            "language"
        )
        if value in TEXT:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    value = (locale.getlocale()[0] or "").lower()
    return (
        "de" if value.startswith("de") else ("en" if value.startswith("en") else "ru")
    )


def _preferences_path():
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "MegaProg"
        / "preferences.json"
    )


def chatgpt_plan_prompt(language, objective):
    language = language if language in TEXT else "en"
    objective = (
        objective.strip()
        or {
            "ru": "<опишите желаемый результат>",
            "de": "<gewünschtes Ergebnis beschreiben>",
            "en": "<describe the desired result>",
        }[language]
    )
    instructions = {
        "en": """Create a plan for MegaProg approved-plan mode. You cannot read my local project unless I attach files or paste repository details into this chat. If the files/context are insufficient to choose real paths and safe checks, ask me for them instead of guessing.

Goal: %s

Use schema_version=1, a short unique plan_id, objective, budget.max_model_turns, features, and tasks. Every feature needs id, title, concrete acceptance criteria, and checks (each check is an argv array). Every task needs id, feature_id, title, instructions, depends_on, allowed_paths, and checks (argv arrays). Use only repository-relative source paths, never .git or .ai-dev. Include only explicit verification commands appropriate to this project. Use gpt-6-luna and medium reasoning by default. First summarize the proposed scope, checks, allowed paths, and turn budget in ordinary language and ask me to approve. After I approve, return the final plan as raw JSON without Markdown fences. Keep each feature small enough to verify.

The plan contains commands that will run on my computer. I will review every check before I approve the plan in MegaProg.""",
        "ru": """Подготовь план для режима MegaProg «Выполнять утверждённый план». Ты не видишь файлы на моём компьютере, если я не приложу их или не вставлю сведения о проекте в чат. Если данных недостаточно, чтобы выбрать реальные пути и безопасные проверки, сначала спроси меня, не угадывай.

Цель: %s

Используй schema_version=1, короткий уникальный plan_id, objective, budget.max_model_turns, features и tasks. У каждой функции должны быть id, title, конкретные критерии приёмки и checks (каждая проверка — массив argv). У каждой задачи должны быть id, feature_id, title, instructions, depends_on, allowed_paths и checks (массивы argv). Используй только относительные пути к файлам проекта; запрещены .git и .ai-dev. Добавляй только явные команды проверок, подходящие проекту. По умолчанию используй gpt-6-luna и reasoning medium. Сначала простыми словами перечисли объём, проверки, разрешённые пути и лимит ходов и попроси меня утвердить. После моего согласия верни план как чистый JSON без Markdown. Каждая функция должна быть достаточно небольшой для проверки.

Команды из плана будут запускаться на моём компьютере. Я проверю каждую команду до утверждения плана в MegaProg.""",
        "de": """Erstelle einen Plan für den MegaProg-Modus „Freigegebenen Plan ausführen“. Du kannst meine lokalen Dateien nicht sehen, sofern ich sie nicht anhänge oder Projektdetails hier einfüge. Wenn Angaben für echte Pfade und sichere Prüfungen fehlen, frage nach, statt etwas zu erraten.

Ziel: %s

Verwende schema_version=1, eine kurze eindeutige plan_id, objective, budget.max_model_turns, features und tasks. Jede Funktion braucht id, title, konkrete Abnahmekriterien und checks (jede Prüfung als argv-Array). Jede Aufgabe braucht id, feature_id, title, instructions, depends_on, allowed_paths und checks (argv-Arrays). Verwende nur relative Projektpfade; .git und .ai-dev sind verboten. Füge nur ausdrücklich passende Prüfkommandos für dieses Projekt ein. Standardmodell ist gpt-6-luna mit reasoning medium. Fasse zuerst Umfang, Prüfungen, erlaubte Pfade und Rundenlimit verständlich zusammen und bitte mich um Freigabe. Erst danach gib den endgültigen Plan als reines JSON ohne Markdown zurück. Jede Funktion soll klein genug zum Überprüfen sein.

Die Planbefehle laufen auf meinem Computer. Ich prüfe jede Prüfung, bevor ich den Plan in MegaProg freigebe.""",
    }
    return instructions[language] % objective


# Each row is one complete UI concept in RU, DE and EN.
_ADDITIONS = [
    (
        "app_tagline",
        "От плана к проверенному результату",
        "Vom Plan zum geprüften Ergebnis",
        "From plan to verified result",
    ),
    ("step_project", "Проект", "Projekt", "Project"),
    ("step_connection", "Подключение", "Verbindung", "Connection"),
    ("step_plan", "План", "Plan", "Plan"),
    ("step_run", "Выполнение", "Ausführung", "Run"),
    ("step_result", "Результат", "Ergebnis", "Result"),
    (
        "project_heading",
        "Над чем будем работать?",
        "Woran arbeiten wir?",
        "What are we building?",
    ),
    (
        "project_hint",
        "Выберите папку программы, над которой будет работать MegaProg.",
        "Wählen Sie den Ordner der Anwendung, an der MegaProg arbeiten soll.",
        "Choose the folder of the program you want MegaProg to work on.",
    ),
    ("demo_title", "Первый запуск", "Erster Start", "First steps"),
    (
        "demo_hint",
        "Можно сначала попробовать на отдельном учебном проекте. Ваши файлы не затрагиваются.",
        "Probieren Sie MegaProg zuerst an einem eigenen Übungsprojekt aus. Ihre Dateien bleiben unberührt.",
        "Try MegaProg with a separate tutorial project first. Your files stay untouched.",
    ),
    ("demo_try", "Попробовать на примере", "Mit Beispiel ausprobieren", "Try the tutorial"),
    (
        "demo_location",
        "Выберите место для новой учебной папки",
        "Speicherort für den neuen Übungsordner wählen",
        "Choose where to create the new tutorial folder",
    ),
    (
        "demo_preparing",
        "Создаём отдельный учебный проект…",
        "Eigenständiges Übungsprojekt wird erstellt…",
        "Creating a separate tutorial project…",
    ),
    (
        "error_demo_location",
        "Не удалось открыть выбранное место. Выберите существующую доступную папку.",
        "Der Speicherort ist nicht verfügbar. Wählen Sie einen vorhandenen Ordner mit Zugriff.",
        "The selected location is unavailable. Choose an existing folder you can access.",
    ),
    (
        "error_demo_git",
        "Для безопасного учебного проекта нужен Git. Установите инструменты разработчика Apple и повторите попытку.",
        "Für das sichere Übungsprojekt wird Git benötigt. Installieren Sie die Apple-Entwicklerwerkzeuge und versuchen Sie es erneut.",
        "Git is needed to prepare the safe tutorial project. Install Apple's developer tools and try again.",
    ),
    (
        "error_demo_python",
        "Для проверок примера нужен Python 3. Установите его и повторите попытку.",
        "Für die Beispieltests wird Python 3 benötigt. Installieren Sie es und versuchen Sie es erneut.",
        "Python 3 is needed to run the tutorial checks. Install it and try again.",
    ),
    (
        "error_demo_source",
        "Учебные файлы не найдены или повреждены. Сохраните сообщение из технического журнала.",
        "Die Übungsdateien fehlen oder sind beschädigt. Bewahren Sie die Meldung aus dem technischen Protokoll auf.",
        "Tutorial files are missing or damaged. Keep the details from the technical log.",
    ),
    (
        "error_demo_create",
        "Не удалось подготовить учебную папку. Сохраните сообщение из технического журнала.",
        "Der Übungsordner konnte nicht erstellt werden. Bewahren Sie die Meldung aus dem technischen Protokoll auf.",
        "The tutorial folder could not be prepared. Keep the details from the technical log.",
    ),
    ("choose_folder", "Выбрать папку", "Ordner wählen", "Choose folder"),
    ("recent", "Недавние проекты", "Letzte Projekte", "Recent projects"),
    ("forget", "Убрать из списка", "Aus Liste entfernen", "Remove from list"),
    ("saved_plans", "Сохранённые планы", "Gespeicherte Pläne", "Saved plans"),
    ("open_saved", "Открыть план", "Plan öffnen", "Open plan"),
    (
        "saved_empty",
        "Здесь появятся утверждённые планы выбранного проекта.",
        "Hier erscheinen die freigegebenen Pläne des Projekts.",
        "Approved plans for this project will appear here.",
    ),
    (
        "recent_empty",
        "Пока нет недавних проектов",
        "Noch keine letzten Projekte",
        "No recent projects yet",
    ),
    ("next", "Далее", "Weiter", "Next"),
    ("back", "Назад", "Zurück", "Back"),
    ("cancel", "Отмена", "Abbrechen", "Cancel"),
    ("settings", "Настройки", "Einstellungen", "Settings"),
    ("about", "О программе", "Über MegaProg", "About MegaProg"),
    ("theme", "Оформление", "Darstellung", "Appearance"),
    ("system", "Системное", "System", "System"),
    ("light", "Светлое", "Hell", "Light"),
    ("dark", "Тёмное", "Dunkel", "Dark"),
    (
        "connect_heading",
        "Подключите своего исполнителя",
        "Codex verbinden",
        "Connect your executor",
    ),
    (
        "connect_hint",
        "MegaProg использует ваш вход в Codex через ChatGPT. Проверка подключения не расходует ходы модели.",
        "MegaProg nutzt Ihre Codex-Anmeldung über ChatGPT. Diese Prüfung verbraucht keine Modellrunden.",
        "MegaProg uses your Codex sign-in through ChatGPT. This check uses no model turns.",
    ),
    (
        "not_checked",
        "Подключение ещё не проверено",
        "Verbindung noch nicht geprüft",
        "Connection not checked yet",
    ),
    (
        "check_connection",
        "Проверить подключение",
        "Verbindung prüfen",
        "Check connection",
    ),
    ("connected", "Codex готов к работе", "Codex ist bereit", "Codex is ready"),
    ("not_connected", "Нужно настроить Codex", "Codex einrichten", "Set up Codex"),
    (
        "connection_help",
        "Установите Codex CLI по официальной инструкции. Откройте Терминал, выполните codex и выберите вход через ChatGPT. Затем повторите проверку.",
        "Codex CLI nach der offiziellen Anleitung installieren. Im Terminal codex starten und mit ChatGPT anmelden. Danach erneut prüfen.",
        "Install Codex CLI using the official guide. Open Terminal, run codex and sign in with ChatGPT. Then check again.",
    ),
    ("official_guide", "Открыть инструкцию", "Anleitung öffnen", "Open guide"),
    (
        "plan_heading",
        "Проверьте, что будет сделано",
        "Prüfen, was umgesetzt wird",
        "Review what will be built",
    ),
    (
        "plan_hint",
        "Функции, разрешённые изменения и проверки остаются в сохранённом плане.",
        "Funktionen, erlaubte Änderungen und Prüfungen bleiben im gespeicherten Plan.",
        "Features, allowed changes and checks stay in the saved plan.",
    ),
    (
        "prepare_plan",
        "Подготовить в ChatGPT",
        "In ChatGPT vorbereiten",
        "Prepare in ChatGPT",
    ),
    ("open_plan", "Открыть готовый план", "Plan-Datei öffnen", "Open a plan file"),
    (
        "objective",
        "Что хотите получить?",
        "Was soll entstehen?",
        "What would you like to build?",
    ),
    (
        "objective_hint",
        "Например: добавить поиск и экспорт отчёта",
        "Zum Beispiel: Suche und Berichtsexport ergänzen",
        "For example: add search and report export",
    ),
    (
        "plan_empty",
        "Откройте JSON-план, чтобы увидеть функции, задачи и команды проверок.",
        "JSON-Plan öffnen, um Funktionen, Aufgaben und Prüfbefehle zu sehen.",
        "Open a JSON plan to see its features, tasks and check commands.",
    ),
    (
        "reviewed",
        "План проверен. Выполнение ещё не запущено.",
        "Plan geprüft. Noch nichts gestartet.",
        "Plan checked. Execution has not started.",
    ),
    ("saved", "План сохранён", "Plan gespeichert", "Plan saved"),
    (
        "approve_run",
        "Утвердить и выполнить",
        "Freigeben und starten",
        "Approve and run",
    ),
    ("save_only", "Сохранить без запуска", "Nur speichern", "Save without running"),
    (
        "one_short",
        "Одна задача и пауза",
        "Eine Aufgabe, dann Pause",
        "One task, then pause",
    ),
    ("commands", "Команды проверок", "Prüfbefehle", "Check commands"),
    ("acceptance", "Критерии готовности", "Abnahmekriterien", "Acceptance criteria"),
    ("dependencies", "Зависимости", "Abhängigkeiten", "Dependencies"),
    ("model", "Исполнитель", "Modell", "Model"),
    ("medium", "среднее", "mittel", "medium"),
    (
        "review_notice",
        "Утверждение разрешает запуск Codex и перечисленных команд в выбранном проекте.",
        "Die Freigabe erlaubt Codex und die aufgeführten Befehle im gewählten Projekt.",
        "Approval allows Codex and the listed commands to run in the selected project.",
    ),
    ("confirm_title", "Подтвердите запуск", "Start bestätigen", "Confirm execution"),
    (
        "confirm_details",
        "Проект: {project}\nПлан: {plan}\nЛимит ходов: {budget}\n\nВы просмотрели функции, разрешённые файлы и все команды проверок?",
        "Projekt: {project}\nPlan: {plan}\nRundenlimit: {budget}\n\nHaben Sie Funktionen, freigegebene Dateien und alle Prüfbefehle geprüft?",
        "Project: {project}\nPlan: {plan}\nTurn limit: {budget}\n\nHave you reviewed the features, allowed files and all check commands?",
    ),
    (
        "run_heading",
        "Работа по вашему плану",
        "Ihr Plan wird ausgeführt",
        "Working through your plan",
    ),
    (
        "run_hint",
        "Здесь видны текущая задача и результаты её проверок.",
        "Hier sehen Sie die aktuelle Aufgabe und ihre Prüfergebnisse.",
        "Follow the current task and its checks here.",
    ),
    ("current_task", "Текущая задача", "Aktuelle Aufgabe", "Current task"),
    (
        "completed_count",
        "Завершено задач: {done} из {total}",
        "Aufgaben erledigt: {done} von {total}",
        "Tasks complete: {done} of {total}",
    ),
    (
        "turn_count",
        "Ходы модели: {used} из {limit}",
        "Modellrunden: {used} von {limit}",
        "Model turns: {used} of {limit}",
    ),
    ("elapsed", "Время запуска", "Laufzeit", "Run time"),
    ("last_event", "Последнее событие", "Letztes Ereignis", "Last event"),
    ("unknown", "Нет данных", "Keine Daten", "Unavailable"),
    ("never", "Ещё нет событий", "Noch keine Ereignisse", "No events yet"),
    ("queue", "Задачи плана", "Aufgaben im Plan", "Plan tasks"),
    ("log", "Технический журнал", "Technisches Protokoll", "Technical log"),
    ("hide_log", "Скрыть журнал", "Protokoll ausblenden", "Hide log"),
    ("show_details", "Подробнее", "Details", "Details"),
    ("stopping", "Останавливаем…", "Wird angehalten…", "Stopping…"),
    (
        "stopped_actual",
        "Процесс остановлен. Сохранённые результаты доступны ниже.",
        "Prozess angehalten. Gespeicherte Ergebnisse stehen unten.",
        "Process stopped. Saved results are available below.",
    ),
    (
        "interrupted",
        "Предыдущий запуск прерван",
        "Vorheriger Lauf unterbrochen",
        "Previous run interrupted",
    ),
    (
        "interrupted_hint",
        "Завершённые задачи сохранены. Перед продолжением MegaProg проверит точку остановки.",
        "Erledigte Aufgaben sind gespeichert. Vor dem Fortsetzen prüft MegaProg den letzten Stand.",
        "Completed tasks are saved. MegaProg will check the checkpoint before continuing.",
    ),
    (
        "observing",
        "Выполнение открыто в другом процессе",
        "Ausführung läuft in einem anderen Prozess",
        "Execution is running in another process",
    ),
    (
        "observing_hint",
        "Можно наблюдать за состоянием. Управляйте остановкой в окне, запустившем работу.",
        "Der Status ist sichtbar. Zum Anhalten das Fenster verwenden, das den Lauf gestartet hat.",
        "You can watch progress. Stop the run in the window that started it.",
    ),
    (
        "result_heading",
        "Результат и точка продолжения",
        "Ergebnis und nächster Schritt",
        "Results and where to continue",
    ),
    (
        "result_hint",
        "Проверенные функции отмечены только при наличии сохранённых доказательств.",
        "Funktionen gelten nur mit gespeicherten Nachweisen als geprüft.",
        "Features are marked verified only when saved evidence is available.",
    ),
    ("verified", "Проверено", "Geprüft", "Verified"),
    ("not_verified", "Ещё не проверено", "Noch nicht geprüft", "Not verified yet"),
    ("proof", "Изменения и проверки", "Änderungen und Prüfungen", "Changes and checks"),
    ("check_pass", "Пройдена", "Bestanden", "Passed"),
    ("check_fail", "Не пройдена", "Fehlgeschlagen", "Failed"),
    ("memory", "Что сохранено", "Was gespeichert ist", "What is saved"),
    (
        "memory_hint",
        "Требования и критерии, завершённые задачи, результаты проверок и точка продолжения хранятся в проекте. Существующие решения войдут в пакет для чата.",
        "Anforderungen, Kriterien, erledigte Aufgaben, Prüfungen und Fortsetzungspunkt bleiben im Projekt. Vorhandene Entscheidungen werden mit exportiert.",
        "Requirements, criteria, completed tasks, checks and the continuation point stay in the project. Existing decisions are included in the chat package.",
    ),
    ("export", "Сохранить пакет для чата", "Chat-Paket speichern", "Save chat package"),
    (
        "export_help",
        "Прикрепите пакет вручную в новый ChatGPT-чат. Обычный чат не видит файлы на вашем компьютере. Перед отправкой просмотрите содержимое.",
        "Paket manuell im neuen ChatGPT-Chat anhängen. Dieser kann lokale Dateien nicht sehen. Inhalt vor dem Senden prüfen.",
        "Attach the package manually in a new ChatGPT chat. A regular chat cannot see files on your computer. Review the contents before sharing.",
    ),
    (
        "export_success",
        "Пакет сохранён: {path}",
        "Paket gespeichert: {path}",
        "Package saved: {path}",
    ),
    ("continue", "Продолжить", "Fortsetzen", "Continue"),
    ("new_plan", "Другой план", "Anderer Plan", "Another plan"),
    ("reason", "Причина остановки", "Grund für den Stopp", "Stop reason"),
    ("next_action", "Следующее действие", "Nächster Schritt", "Next action"),
    (
        "fix_then_resume",
        "Устраните причину. Затем просмотрите план и явно подтвердите продолжение.",
        "Ursache beheben. Dann den Plan prüfen und das Fortsetzen bestätigen.",
        "Resolve the cause, then review the plan and explicitly confirm continuation.",
    ),
    (
        "usage",
        "Доступные данные расхода",
        "Verfügbare Verbrauchsdaten",
        "Available usage data",
    ),
    (
        "usage_partial",
        "Данные неполные; отсутствие записи не означает нулевой расход.",
        "Daten unvollständig; ein fehlender Eintrag bedeutet nicht null Verbrauch.",
        "Data is incomplete; a missing record does not mean zero usage.",
    ),
    (
        "tokens",
        "Вход: {input} · Кэш: {cached} · Выход: {output}",
        "Eingabe: {input} · Cache: {cached} · Ausgabe: {output}",
        "Input: {input} · Cached: {cached} · Output: {output}",
    ),
    (
        "close_running",
        "Работа ещё выполняется. Для закрытия сначала остановим её и дождёмся завершения процесса.",
        "Die Arbeit läuft noch. Vor dem Schließen wird der Prozess angehalten.",
        "Work is still running. Closing will first stop it and wait for the process to exit.",
    ),
    ("stay", "Остаться", "Hier bleiben", "Stay"),
    ("stop_close", "Остановить и закрыть", "Anhalten und schließen", "Stop and close"),
    ("all_files", "Все файлы", "Alle Dateien", "All files"),
    ("open", "Открыть", "Öffnen", "Open"),
    ("save", "Сохранить", "Speichern", "Save"),
    ("copy_path", "Скопировать путь", "Pfad kopieren", "Copy path"),
    ("copy_commands", "Скопировать команды", "Befehle kopieren", "Copy commands"),
    ("copied_short", "Скопировано", "Kopiert", "Copied"),
    ("refresh", "Обновить", "Aktualisieren", "Refresh"),
    (
        "error_project",
        "Не удалось открыть корневую папку Git-проекта. Проверьте путь и доступ.",
        "Git-Projektstamm konnte nicht geöffnet werden. Pfad und Zugriff prüfen.",
        "Could not open the Git project root. Check the path and access.",
    ),
    (
        "error_plan",
        "Файл плана не подходит. Проверьте JSON, обязательные поля, пути и зависимости.",
        "Plan-Datei ungültig. JSON, Pflichtfelder, Pfade und Abhängigkeiten prüfen.",
        "Invalid plan file. Check the JSON, required fields, paths and dependencies.",
    ),
    (
        "error_changed",
        "План или проект изменился после просмотра. Откройте и проверьте план заново.",
        "Plan oder Projekt hat sich seit der Prüfung geändert. Plan erneut öffnen und prüfen.",
        "The plan or project changed after review. Open and review the plan again.",
    ),
    (
        "error_saved",
        "Не удалось прочитать сохранённый план. Его файлы не изменены.",
        "Gespeicherter Plan konnte nicht gelesen werden. Seine Dateien bleiben erhalten.",
        "Could not read the saved plan. Its files are unchanged.",
    ),
    (
        "error_launch",
        "Не удалось запустить действие. Подробности доступны в журнале.",
        "Aktion konnte nicht gestartet werden. Details stehen im Protokoll.",
        "Could not start the action. Details are available in the log.",
    ),
    (
        "error_export",
        "Пакет не сохранён. Проверьте журнал и выберите новую папку вне проекта.",
        "Paket nicht gespeichert. Protokoll prüfen und neuen Ordner außerhalb des Projekts wählen.",
        "Package not saved. Check the log and choose a new folder outside the project.",
    ),
    (
        "error_budget",
        "Достигнут лимит ходов или попыток. Автоматический повтор остановлен.",
        "Runden- oder Versuchslimit erreicht. Kein automatischer Neustart.",
        "Turn or attempt limit reached. Automatic retries are stopped.",
    ),
    (
        "error_auth",
        "Codex недоступен или требуется вход. Повторите проверку подключения.",
        "Codex ist nicht verfügbar oder eine Anmeldung fehlt. Verbindung erneut prüfen.",
        "Codex is unavailable or requires sign-in. Check the connection again.",
    ),
    (
        "error_model",
        "Запрошенный исполнитель недоступен с текущими настройками.",
        "Das angeforderte Modell ist mit diesen Einstellungen nicht verfügbar.",
        "The requested model is unavailable with the current settings.",
    ),
    (
        "error_drift",
        "Обнаружены изменения файлов вне разрешённой работы. Требуется их проверка.",
        "Dateien wurden außerhalb der freigegebenen Arbeit geändert. Änderungen prüfen.",
        "Files changed outside the approved work. These changes need review.",
    ),
    (
        "error_check",
        "Проверка результата не пройдена. Изменения и журнал проверки сохранены.",
        "Ergebnisprüfung fehlgeschlagen. Änderungen und Prüfprotokoll sind gespeichert.",
        "Result verification failed. Changes and the check log are saved.",
    ),
    (
        "error_generic",
        "Работа остановлена. Точная причина сохранена в технических подробностях.",
        "Arbeit angehalten. Der genaue Grund steht in den technischen Details.",
        "Work stopped. The exact reason is saved in the technical details.",
    ),
    (
        "error_preferences",
        "Не удалось сохранить настройки интерфейса.",
        "Oberflächeneinstellungen konnten nicht gespeichert werden.",
        "Could not save interface preferences.",
    ),
    ("status_READY", "Готов к запуску", "Startbereit", "Ready to run"),
    (
        "error_project_folder",
        "Папка не найдена. Выберите существующую папку проекта.",
        "Ordner nicht gefunden. Wählen Sie einen vorhandenen Projektordner.",
        "Folder not found. Choose an existing project folder.",
    ),
    (
        "error_project_not_git",
        "В этой папке ещё нет истории проекта (Git). Выберите подготовленный проект. Распакованные исходники из ZIP сами по себе таким проектом не являются.",
        "Dieser Ordner hat noch keine Git-Projekthistorie. Wählen Sie ein vorbereitetes Projekt. Entpackter ZIP-Quellcode ist allein noch kein Git-Projekt.",
        "This folder has no Git project history yet. Choose a prepared project. Unpacking a source ZIP does not create a Git project.",
    ),
    (
        "error_project_nested",
        "Выбрана вложенная папка. Откройте главную папку этого проекта: {root}",
        "Ein Unterordner wurde gewählt. Öffnen Sie den Projektordner: {root}",
        "You selected a subfolder. Open this project's root folder: {root}",
    ),
    (
        "error_project_permission",
        "Нет доступа к выбранной папке. Проверьте запрос macOS на доступ к ней и повторите выбор.",
        "Kein Zugriff auf den gewählten Ordner. Prüfen Sie die macOS-Zugriffsanfrage und wählen Sie ihn erneut.",
        "The selected folder cannot be accessed. Check the macOS access request and select it again.",
    ),
    (
        "error_project_git_missing",
        "Не найден Git — компонент для сохранения истории проекта. Установите инструменты разработчика Apple, затем повторите выбор папки.",
        "Git zum Speichern der Projekthistorie fehlt. Installieren Sie die Apple-Entwicklerwerkzeuge und wählen Sie den Ordner erneut.",
        "Git, needed for project history, was not found. Install Apple's developer tools, then select the folder again.",
    ),
    (
        "error_project_git",
        "Git не смог проверить выбранную папку. Точная причина и путь сохранены в техническом журнале.",
        "Git konnte den Ordner nicht prüfen. Grund und Pfad stehen im technischen Protokoll.",
        "Git could not check the selected folder. The exact reason and path are in the technical log.",
    ),
    (
        "recent_choose",
        "Выберите недавний проект…",
        "Letztes Projekt wählen…",
        "Choose a recent project…",
    ),
    (
        "error_project_timeout",
        "Проверка папки не ответила вовремя. Проверьте запрос macOS на доступ к выбранной папке и повторите выбор.",
        "Ordnerprüfung hat zu lange gedauert. Prüfen Sie die macOS-Zugriffsanfrage für den gewählten Ordner und wählen Sie ihn erneut.",
        "Folder check timed out. Check the macOS access request for the selected folder, then select it again.",
    ),
    (
        "preparing",
        "Подготовка запуска…",
        "Start wird vorbereitet…",
        "Preparing to run…",
    ),
    ("status_PENDING", "В очереди", "In Warteschlange", "Queued"),
    ("status_RUNNING", "Работа исполнителя", "Modell arbeitet", "Executor working"),
    ("status_VERIFYING", "Проверка результата", "Ergebnisprüfung", "Checking results"),
    ("status_COMPLETED", "Завершено", "Abgeschlossen", "Completed"),
    ("status_PAUSED", "На паузе", "Pausiert", "Paused"),
    ("status_BLOCKED", "Требуется действие", "Aktion erforderlich", "Action needed"),
    ("status_VERIFIED", "Проверено", "Geprüft", "Verified"),
    (
        "status_REGRESSION",
        "Повторная проверка не пройдена",
        "Erneute Prüfung fehlgeschlagen",
        "Regression check failed",
    ),
    ("loading", "Читаем состояние…", "Status wird gelesen…", "Reading state…"),
    (
        "about_body",
        "MegaProg выполняет утверждённые планы через ваш Codex, проверяет результаты и сохраняет точку продолжения.\nПредварительный выпуск · GPLv3",
        "MegaProg führt freigegebene Pläne über Ihr Codex aus, prüft Ergebnisse und speichert den Fortsetzungspunkt.\nVorabversion · GPLv3",
        "MegaProg runs approved plans through your Codex, checks results and saves where to continue.\nPreview · GPLv3",
    ),
]
for _row in _ADDITIONS:
    for _index, _language in enumerate(("ru", "de", "en"), 1):
        TEXT[_language][_row[0]] = _row[_index]
for _language in TEXT:
    TEXT[_language]["ok"] = TEXT[_language]["reviewed"]


def tr(language, key, **values):
    return TEXT.get(language, TEXT["en"])[key].format(**values)


for _lang, _message in {
    "ru": "Для переноса контекста в проекте нужны docs/PRODUCT_ROADMAP.md и docs/CONTINUITY.md. Подготовьте эти документы и повторите экспорт. Планы и результаты сохранены.",
    "de": "Für die Kontextübergabe braucht das Projekt docs/PRODUCT_ROADMAP.md und docs/CONTINUITY.md. Dokumente vorbereiten und erneut exportieren. Pläne und Ergebnisse bleiben gespeichert.",
    "en": "Context export requires docs/PRODUCT_ROADMAP.md and docs/CONTINUITY.md in the project. Prepare these documents and export again. Plans and results are saved.",
}.items():
    TEXT[_lang]["error_handoff_docs"] = _message
