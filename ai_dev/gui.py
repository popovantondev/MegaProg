"""Local desktop interface. All development actions use the same CLI backend."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

BASE = Path(__file__).resolve().parent.parent


def cli_command(project, args):
    return [sys.executable, '-u', '-m', 'ai_dev', '-C', str(project)] + args


def make_prompt(task, context):
    if not task.strip():
        raise ValueError('Напишите, что нужно сделать.')
    return task.strip() + ('\n\nКонтекст и договорённости из чата:\n' + context.strip() if context.strip() else '')


def inspect_project(project, git_state=None):
    """Readiness facts for the first-run checklist.

    Selecting a folder must remain side-effect free.  Consequently Git facts are
    supplied by the explicit refresh action; filesystem/config facts are safe to
    inspect immediately.
    """
    root = Path(project).expanduser().resolve()
    config_path = root / '.ai-dev' / 'config.json'
    configured = config_path.is_file()
    verification = False
    if configured:
        try:
            value = json.loads(config_path.read_text(encoding='utf-8'))
            verification = bool(value.get('verify'))
        except (OSError, ValueError, AttributeError):
            configured = False
    facts = {
        'project_selected': root.is_dir(),
        'git_repository': (root / '.git').exists() if root.is_dir() else False,
        'ai_dev_configured': configured,
        'verification_configured': verification,
        'working_tree_clean': None,
        'baseline_exists': None,
        'git_checked': False,
    }
    if git_state:
        facts.update(git_state)
        facts['git_checked'] = True
    return facts


def checklist_lines(facts):
    """Return stable (title, state, detail) rows for the visible checklist."""
    selected = facts['project_selected']
    unknown = 'проверьте кнопкой «Обновить состояние»'
    return [
        ('1. Выбрать или создать проект', 'done' if selected else 'todo',
         'Папка выбрана' if selected else 'Выберите существующую папку или создайте новую'),
        ('2. Убедиться, что это Git-репозиторий',
         'done' if facts['git_repository'] else 'todo',
         'Git-репозиторий найден' if facts['git_repository'] else 'Git-репозиторий ещё не создан'),
        ('3. Задать или подтвердить команду проверки',
         'done' if facts['verification_configured'] else 'todo',
         'Команда проверки сохранена' if facts['verification_configured'] else 'Введите команду ниже'),
        ('4. Подготовить проект', 'done' if facts['ai_dev_configured'] else 'todo',
         'Настройки .ai-dev сохранены' if facts['ai_dev_configured'] else 'Нажмите «Подготовить»'),
        ('5. Сохранить чистую исходную версию',
         'done' if facts['baseline_exists'] else ('todo' if facts['git_checked'] else 'check'),
         'Исходный коммит найден' if facts['baseline_exists'] else
         ('Рабочая папка не чиста' if facts['working_tree_clean'] is False else unknown)),
        ('6. Запустить задачу',
         'done' if facts['baseline_exists'] and facts['working_tree_clean'] else 'todo',
         'Можно описать задачу и нажать «Выполнить задачу»' if facts['baseline_exists'] and facts['working_tree_clean']
         else 'После исходной версии рабочая папка должна быть чистой'),
    ]


def task_readiness_message(facts):
    """Return the next user action required before a task may be started."""
    if not facts.get('project_selected'):
        return 'Сначала выберите существующую папку проекта.'
    if not facts.get('verification_configured'):
        return 'Введите команду проверки и нажмите «Подготовить».'
    if not facts.get('ai_dev_configured'):
        return 'Нажмите «Подготовить», чтобы сохранить настройки проекта.'
    if not facts.get('git_repository'):
        return 'Нажмите «Сохранить исходную версию», чтобы создать Git-репозиторий и baseline-коммит.'
    if facts.get('baseline_exists') is not True:
        return 'Нажмите «Сохранить исходную версию», чтобы создать baseline-коммит.'
    if facts.get('working_tree_clean') is not True:
        return 'Сохраните или отмените текущие изменения: перед задачей рабочая папка должна быть чистой.'
    return None


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.project = tk.StringVar()
        self.check = tk.StringVar(value='python3 -m unittest discover -s tests -v')
        self.kind = tk.StringVar(value='auto')
        self.status = tk.StringVar(value='Выберите папку проекта или создайте новую.')
        self.cancel_path = None
        root.title('MegaProg — помощник разработки')
        root.geometry('960x710')
        root.minsize(800, 630)
        root.configure(bg='#f3f5f9')
        style = ttk.Style()
        style.configure('TFrame', background='#f3f5f9')
        style.configure('TLabel', background='#f3f5f9', foreground='#1f2937', font=('Arial', 12))
        style.configure('Title.TLabel', font=('Arial', 23, 'bold'))
        style.configure('TButton', padding=(10, 7))
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='MegaProg', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, text='Опишите результат. Модель, выполнение и проверки — внутри.').pack(anchor='w', pady=(3, 16))
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='1. Проект').pack(side='left', padx=(0, 12))
        ttk.Entry(row, textvariable=self.project).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Выбрать папку…', command=self.pick).pack(side='left', padx=(8, 0))
        ttk.Button(row, text='Новый…', command=self.new_project).pack(side='left', padx=(8, 0))
        checklist = ttk.LabelFrame(frame, text='Безопасный порядок первого запуска', padding=8)
        checklist.pack(fill='x', pady=(12, 0))
        ttk.Label(checklist, text='Проект не изменяется при выборе папки. Git проверяется только по кнопке обновления.',
                  wraplength=900).pack(anchor='w', pady=(0, 5))
        self.checklist_rows = []
        for _ in range(6):
            row = ttk.Frame(checklist)
            row.pack(fill='x', pady=1)
            state = tk.StringVar()
            title = tk.StringVar()
            detail = tk.StringVar()
            ttk.Label(row, textvariable=state, width=3, anchor='center').pack(side='left')
            ttk.Label(row, textvariable=title).pack(side='left')
            ttk.Label(row, textvariable=detail, foreground='#52606d').pack(side='left', padx=(8, 0))
            self.checklist_rows.append((state, title, detail))
        ttk.Button(checklist, text='Обновить состояние', command=self.refresh_project_state).pack(anchor='w', pady=(6, 0))
        self.update_checklist()
        ttk.Label(frame, text='2. Что нужно сделать?').pack(anchor='w', pady=(16, 6))
        self.task = tk.Text(frame, height=3, wrap='word', font=('Arial', 13), relief='solid', bd=1)
        self.task.pack(fill='x')
        self.task.insert('1.0', '')
        ttk.Label(frame, text='Контекст из соседнего чата — вставьте краткое описание или договорённости (необязательно)').pack(anchor='w', pady=(10, 5))
        self.context = tk.Text(frame, height=2, wrap='word', font=('Arial', 11), relief='solid', bd=1)
        self.context.pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x', pady=(12, 0))
        ttk.Label(row, text='Проверка проекта').pack(side='left', padx=(0, 10))
        ttk.Entry(row, textvariable=self.check).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Подготовить', command=self.prepare).pack(side='left', padx=(8, 0))
        ttk.Label(frame, text='Команду проверки можно попросить определить в чате проекта. Для нового проекта попросите в задаче создать код и тесты.',
                  wraplength=900).pack(anchor='w', pady=(3, 8))
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Button(row, text='Сохранить исходную версию', command=self.baseline).pack(side='left')
        ttk.Button(row, text='Проверить подключение', command=lambda: self.start(['doctor'])).pack(side='left', padx=6)
        ttk.Button(row, text='Лимиты', command=lambda: self.start(['limits'])).pack(side='left')
        row = ttk.Frame(frame); row.pack(fill='x', pady=(14, 8))
        self.run_button = ttk.Button(row, text='▶ Выполнить задачу', command=self.execute)
        self.run_button.pack(side='left')
        ttk.Button(row, text='Продолжить', command=lambda: self.start(['resume'])).pack(side='left', padx=6)
        ttk.Button(row, text='Остановить', command=self.stop).pack(side='left')
        ttk.Button(row, text='Сохранить результат', command=self.commit).pack(side='left', padx=6)
        ttk.Button(row, text='Закрыть задачу', command=lambda: self.start(['cancel'])).pack(side='left')
        ttk.Label(frame, textvariable=self.status, wraplength=920).pack(anchor='w', pady=(2, 6))
        footer = ttk.Frame(frame); footer.pack(side='bottom', fill='x', pady=(10, 0))
        ttk.Button(footer, text='Скопировать инструкцию для другого чата', command=self.copy_instruction).pack(side='left')
        ttk.Button(footer, text='Состояние', command=lambda: self.start(['status'])).pack(side='right')
        self.log = tk.Text(frame, height=6, wrap='word', bg='#172033', fg='#e5edf9',
                           insertbackground='white', font=('Menlo', 11), state='disabled')
        self.log.pack(fill='both', expand=True)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def project_path(self):
        text = self.project.get().strip()
        path = Path(text).expanduser().resolve()
        if not text or not path.is_dir():
            raise ValueError('Сначала выберите существующую папку проекта.')
        return path

    def pick(self):
        if self.busy:
            return
        selected = filedialog.askdirectory(title='Папка программы, над которой работаем')
        if selected:
            self.project.set(selected)
            config = Path(selected) / '.ai-dev/config.json'
            if config.exists():
                try:
                    import shlex
                    checks = json.loads(config.read_text())['verify']
                    if checks:
                        self.check.set(shlex.join(checks[0]))
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            self.update_checklist()
            self.status.set('Проект выбран. Опишите задачу; при первом запуске нажмите «Подготовить».')

    def new_project(self):
        if self.busy:
            return
        selected = filedialog.asksaveasfilename(title='Имя новой папки проекта', initialfile='Моя программа')
        if selected:
            try:
                Path(selected).mkdir(exist_ok=False)
                self.project.set(selected)
                self.update_checklist()
                self.status.set('Папка создана. Нажмите «Подготовить», затем «Сохранить исходную версию» и опишите новую программу.')
            except OSError as exc:
                messagebox.showerror('Папка не создана', str(exc))

    def update_checklist(self, git_state=None):
        text = self.project.get().strip()
        facts = inspect_project(text, git_state) if text else inspect_project(Path('.').joinpath('__no_project__'))
        for (state_var, title_var, detail_var), (title, state, detail) in zip(self.checklist_rows, checklist_lines(facts)):
            state_var.set({'done': '✓', 'todo': '○', 'check': '?'}.get(state, '○'))
            title_var.set(title)
            detail_var.set('— ' + detail)

    def refresh_project_state(self):
        """Explicitly query Git; this is never called just by selecting a folder."""
        try:
            project = self.project_path()
            git_state = self.read_git_state(project)
            self.update_checklist(git_state)
            self.status.set('Состояние проекта обновлено.')
        except (OSError, ValueError) as exc:
            messagebox.showerror('Состояние проекта', str(exc))

    @staticmethod
    def read_git_state(project):
        """Read the Git facts used by both the checklist and task start."""
        top = subprocess.run(['git', '-C', str(project), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True)
        is_root = top.returncode == 0 and Path(top.stdout.strip()).resolve() == project
        if not is_root:
            return {'git_repository': False, 'working_tree_clean': None,
                    'baseline_exists': False, 'git_checked': True}
        status = subprocess.run(['git', '-C', str(project), 'status', '--porcelain'],
                                capture_output=True, text=True)
        head = subprocess.run(['git', '-C', str(project), 'rev-parse', '--verify', 'HEAD'],
                              capture_output=True, text=True)
        return {'git_repository': True,
                'working_tree_clean': status.returncode == 0 and not status.stdout,
                'baseline_exists': head.returncode == 0, 'git_checked': True}

    def write_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text)
        self.log.see('end')
        self.log.configure(state='disabled')

    def start(self, args, sequence=None):
        if self.busy:
            messagebox.showinfo('Работа идёт', 'Дождитесь результата или нажмите «Остановить».')
            return
        try:
            project = self.project_path()
        except ValueError as exc:
            messagebox.showinfo('Выберите проект', str(exc)); return
        self.busy = True
        self.run_button.configure(state='disabled')
        fd, name = tempfile.mkstemp(prefix='megaprog-stop-')
        os.close(fd)
        os.unlink(name)
        self.cancel_path = Path(name)
        env = dict(os.environ, AI_DEV_CANCEL_FILE=name)
        self.status.set('Работаю… Ход выполнения появится ниже.')
        self.write_log('\n— ' + ' '.join(args[:1]) + ' —\n')
        commands = sequence if sequence is not None else [cli_command(project, args)]
        def worker():
            code = 0
            try:
                for command in commands:
                    if self.cancel_path.exists():
                        code = 130; break
                    process = subprocess.Popen(command, cwd=str(BASE), env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
                    for line in process.stdout:
                        self.events.put(('output', line))
                    process.stdout.close()
                    code = process.wait()
                    if code:
                        break
            except Exception as exc:
                code = 1
                self.events.put(('output', str(exc) + '\n'))
            self.events.put(('done', code))
        threading.Thread(target=worker, daemon=True).start()

    def prepare(self):
        import shlex
        try:
            project = self.project_path()
            command = shlex.split(self.check.get(), posix=True)
            if not command:
                raise ValueError('Укажите команду проверки.')
            self.start(['init'], [cli_command(project, ['init', '--auto']), cli_command(project, ['check'] + command)])
        except ValueError as exc:
            messagebox.showinfo('Подготовка', str(exc))

    def execute(self):
        try:
            project = self.project_path()
            prompt = make_prompt(self.task.get('1.0', 'end'), self.context.get('1.0', 'end'))
            facts = inspect_project(project, self.read_git_state(project))
            message = task_readiness_message(facts)
            if message:
                messagebox.showinfo('Задача пока не готова', message)
                return
            self.start(['task', prompt])
        except (OSError, ValueError) as exc:
            messagebox.showinfo('Задача', str(exc))

    def baseline(self):
        if self.busy:
            return
        try:
            project = self.project_path()
            if not (project / '.ai-dev/config.json').exists():
                raise ValueError('Сначала нажмите «Подготовить».')
            state_path = project / '.ai-dev/state.json'
            if state_path.exists() and json.loads(state_path.read_text())['status'] not in ('COMPLETED', 'CANCELLED', 'IDLE'):
                raise ValueError('Сначала завершите или закройте текущую задачу.')
            code = subprocess.run(['git', '-C', str(project), 'rev-parse', '--show-toplevel'],
                                  capture_output=True, text=True)
            if code.returncode == 0 and Path(code.stdout.strip()).resolve() != project:
                raise ValueError('Выберите корень Git-проекта, а не вложенную папку.')
            result = subprocess.run(['git', '-C', str(project), 'status', '--short'], capture_output=True, text=True)
            detail = result.stdout[:2500] if code.returncode == 0 else 'Новый Git-проект; текущие файлы будут включены в исходную версию.'
            if not messagebox.askokcancel('Сохранить текущие файлы в Git?',
                    'Это сохранит все текущие изменения выбранного проекта, кроме исключённых в .gitignore.\n\n' + detail):
                return
            commands = []
            if code.returncode != 0:
                commands.append(['git', '-C', str(project), 'init'])
            commands += [['git', '-C', str(project), 'add', '-A'],
                         ['git', '-C', str(project), '-c', 'user.name=MegaProg local', '-c', 'user.email=local@megaprog.invalid',
                          'commit', '--allow-empty', '-m', 'Save project baseline before ai-dev']]
            self.start(['baseline'], commands)
        except (ValueError, OSError) as exc:
            messagebox.showerror('Сохранение', str(exc))

    def commit(self):
        self.start(['commit', '-m', 'Complete ai-dev task'])

    def stop(self):
        if self.busy and self.cancel_path:
            self.cancel_path.touch()
            self.status.set('Останавливаю выполнение и сохраняю состояние…')

    def copy_instruction(self):
        path = BASE / 'ДЛЯ_ДРУГОГО_ЧАТА.txt'
        text = path.read_text(encoding='utf-8')
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set('Инструкция скопирована. Вставьте её в нужный чат и приложите архив MegaProg.')

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'output':
                    self.write_log(value)
                else:
                    self.busy = False
                    self.run_button.configure(state='normal')
                    stopped = self.cancel_path and self.cancel_path.exists()
                    self.status.set('Остановлено. Файлы сохранены; можно продолжить задачу.' if stopped else
                                    'Готово. Результат — в журнале ниже.' if value == 0 else
                                    'Не удалось завершить действие. Причина — в журнале; его можно скопировать в чат.')
                    if self.cancel_path:
                        self.cancel_path.unlink(missing_ok=True)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            self.stop()
            self.root.after(300, self.close)
        else:
            self.root.destroy()


def main():
    from .plan_gui import main as approved_plan_main
    approved_plan_main()


if __name__ == '__main__':
    main()
