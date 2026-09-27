import argparse
import json
import os
import shlex
import sys
import subprocess
import time
import shutil
from datetime import datetime
from pathlib import Path
from . import codex, supervisor, routing, planning
from .discovery import discover
from .storage import lock, read, write
from .lessons import (LESSONS_FILE, export_lessons, import_lessons, import_lesson,
                      lesson_summary, sync_summary)
from .release_check import check_archive
from .usage_report import aggregate
from .chatgpt_limits import report as chatgpt_limit_report, update as update_chatgpt_limits
from .review import (build_chatgpt_request, build_review_batch, import_review,
                     render_chatgpt_request, render_review_batch)
from .plan_handoff import import_plan
from .mcp_advisory import (HOST_MANIFEST_FILE, discover_mcp_tools, run_mcp_host,
                           runtime_status, diagnose_deployment, start_funnel)
from .stress import run_stress
from .windows_check import audit as windows_audit
from . import orchestration, approved_plan
from . import dashboard, concurrency
from . import native_monitor
from . import self_repair
from . import coordinator_context
from . import evidence_packet
from .terminal_style import interactive, styled


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def get_catalog(root):
    report = codex.doctor(root)
    if not report['ready']:
        raise ValueError('Codex/ChatGPT login недоступен. Запустите doctor.')
    catalog = discover(root, report['executable'])
    write(root / '.ai-dev/catalog.json', catalog)
    return report, catalog


def decision(root, catalog, prompt, args):
    config = supervisor.configure(root)
    requested_model = args.model or (config['model'] if config['model'] != 'auto' else None)
    requested_reasoning = args.reasoning or (config['reasoning'] if requested_model else None)
    default_scope = 'planner' if config.get('strong_planning', True) else 'worker'
    pin_scope = getattr(args, 'pin_scope', None) or default_scope
    if pin_scope not in ('planner', 'worker', 'task'):
        raise ValueError('pin_scope должен быть planner, worker или task.')
    worker_pin = requested_model if pin_scope in ('worker', 'task') else None
    worker_reasoning = requested_reasoning if worker_pin else None
    worker = routing.choose(catalog, prompt, args.kind, worker_pin, worker_reasoning)
    task_kind = routing.classify(prompt) if args.kind == 'auto' else args.kind
    if config.get('strong_planning', True):
        planner_pin = requested_model if pin_scope in ('planner', 'task') else None
        planner_reasoning = requested_reasoning if planner_pin else None
        if planner_pin and planner_reasoning:
            planner = routing.choose(catalog, prompt, task_kind, planner_pin, planner_reasoning)
            planner['role'] = 'planner'
            planner['reason'] = 'Явный выбор модели и режима для планирования.'
        else:
            planner = planning.strong(catalog, task_kind=task_kind,
                                      medium=pin_scope == 'planner' and args.reasoning == 'medium')
        return {'planner': planner, 'worker_estimate': worker,
                'pin_scope': pin_scope,
                'note': 'Исполнитель уточняется после read-only плана; два провала → сильная модель.',
                'recovery': planning.strong(catalog, recovery=True), 'model_turns_started': 0}
    return worker


def main(argv=None):
    parser = argparse.ArgumentParser(description='ai-dev — задачи Codex с выбором модели и проверками')
    parser.add_argument('-C', '--project', default='.', help='Корень проекта')
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('gui', help='Open the approved-plan desktop application')
    subs.add_parser('monitor', help='Открыть живой монитор в Terminal.app / CMD')
    repair = subs.add_parser('self-repair', help='Самодиагностика и ограниченный ремонт MegaProg')
    repair.add_argument('action', choices=('status', 'enable', 'disable', 'run', 'check'), default='status', nargs='?')
    init = subs.add_parser('init', help='Подготовить проект')
    init.add_argument('--auto', action='store_true', help='Включить автоматический выбор модели')
    check = subs.add_parser('check', help='Настроить проверку: check python3 -m unittest discover -v')
    check.add_argument('argv', nargs=argparse.REMAINDER)
    doc = subs.add_parser('doctor', help='Проверить доступность Codex')
    doc.add_argument('--probe', action='store_true', help='Совместимый флаг; doctor не запускает модель')
    access = subs.add_parser('worker-access', help='Windows worker: полный доступ для этого проекта')
    access.add_argument('action', choices=('status', 'enable', 'disable'))
    for name in ('task', 'plan'):
        sub = subs.add_parser(name, help='Выполнить задачу' if name == 'task' else 'Показать выбор модели без её запуска')
        sub.add_argument('prompt')
        sub.add_argument('--kind', choices=routing.KINDS, default='auto')
        sub.add_argument('--model', help='Явно выбрать модель из каталога')
        sub.add_argument('--reasoning', choices=['low', 'medium', 'high'])
        sub.add_argument('--pin-scope', choices=['planner', 'worker', 'task'],
                         help='К какой роли применять явную модель: planner (по умолчанию), worker или обе')
        if name == 'task':
            sub.add_argument('--orchestration-task-id', help='Attach the task to explicit orchestration approval gates')
            sub.add_argument('--evidence-seed', action='append', default=[], metavar='PATH',
                             help='Required exact repository-relative evidence path (repeatable)')
            sub.add_argument('--optional-evidence-seed', action='append', default=[], metavar='PATH',
                             help='Optional exact repository-relative evidence path (repeatable)')
    status = subs.add_parser('status', help='Показать состояние')
    status.add_argument('--json', action='store_true')
    handoff = subs.add_parser('handoff', help='Сохранить roadmap и контекст для Architect/Work без model turn')
    handoff.add_argument('--output', help='Новый каталог вне репозитория для трёх файлов передачи')
    handoff.add_argument('--check', metavar='SNAPSHOT_JSON', help='Проверить целостность и актуальность снимка')
    context = subs.add_parser('context', help='Компактный контекст координатора без model turn')
    context.add_argument('--task', help='Показать только связанные факты указанной задачи')
    context.add_argument('--component', help='Ограничить дополнительные факты компонентом/path')
    context.add_argument('--budget', type=int, default=coordinator_context.DEFAULT_BUDGET,
                         help='Бюджет JSON capsule в UTF-8 bytes (3072–16384)')
    context.add_argument('--since-event-id', help='Показать только события после указанного event ID')
    context.add_argument('--checkpoint', help='Использовать сохранённый checkpoint как event cursor')
    context.add_argument('--save-checkpoint', help='Сохранить указатель до последнего видимого event ID')
    context.add_argument('--check-stale', metavar='CAPSULE_JSON',
                         help='Сравнить capsule JSON с текущими HEAD/task/events')
    context.add_argument('--json', action='store_true')
    evidence = subs.add_parser('evidence', help='Собрать/проверить компактный offline Evidence Packet')
    evidence_sub = evidence.add_subparsers(dest='evidence_action', required=True)
    evidence_build = evidence_sub.add_parser('build', help='Собрать Evidence Packet без model turn')
    evidence_build.add_argument('--task-id', help='ID задачи из сохранённого state')
    evidence_build.add_argument('--goal', help='Цель, если задача ещё не сохранена в state')
    evidence_build.add_argument('--budget', type=int, default=evidence_packet.DEFAULT_BUDGET)
    evidence_build.add_argument('--max-files', type=int, default=8)
    evidence_build.add_argument('--evidence-seed', action='append', default=[], metavar='PATH')
    evidence_build.add_argument('--optional-evidence-seed', action='append', default=[], metavar='PATH')
    evidence_build.add_argument('--json', action='store_true')
    evidence_show = evidence_sub.add_parser('show', help='Показать сохранённый packet')
    evidence_show.add_argument('packet', help='Путь к JSON или packet ID')
    evidence_show.add_argument('--json', action='store_true')
    evidence_check = evidence_sub.add_parser('check', help='Проверить актуальность packet')
    evidence_check.add_argument('packet', help='Путь к JSON или packet ID')
    evidence_check.add_argument('--json', action='store_true')
    subs.add_parser('models', help='Получить доступные модели и reasoning')
    subs.add_parser('limits', help='Показать текущие лимиты подписки')
    chatgpt_limits = subs.add_parser('chatgpt-limits', help='Показать/изменить локальный ChatGPT advisory ledger')
    chatgpt_limits.add_argument('--bucket', default='chat_pro', help='ChatGPT bucket name (configurable)')
    chatgpt_limits.add_argument('--default-bucket', help='Set the bucket used by default')
    chatgpt_limits.add_argument('--status', choices=('available', 'exhausted', 'unknown'))
    chatgpt_limits.add_argument('--limit-turns', type=int)
    chatgpt_limits.add_argument('--reserved-turns', type=int)
    chatgpt_limits.add_argument('--spent-turns', type=int)
    chatgpt_limits.add_argument('--reset-at')
    chatgpt_limits.add_argument('--observed-at')
    chatgpt_limits.add_argument('--source', choices=('manual_ui', 'official_export', 'unknown'))
    chatgpt_limits.add_argument('--confidence', choices=('high', 'medium', 'low', 'unknown'))
    chatgpt_limits.add_argument('--notes')
    chatgpt_limits.add_argument('--used', type=int)
    chatgpt_limits.add_argument('--remaining', type=int)
    chatgpt_limits.add_argument('--json', action='store_true')
    usage = subs.add_parser('usage-report', help='Сводка записанного локального usage без сети')
    usage.add_argument('--json', action='store_true')
    usage.add_argument('--root', action='append', type=Path,
                       help='Дополнительный локальный корень .ai-dev/runs (можно повторять)')
    review_batch = subs.add_parser('review-batch', help='Собрать offline advisory-пакет для обычного ChatGPT')
    review_batch.add_argument('--json', action='store_true')
    review_batch.add_argument('--max-items', type=int, default=4,
                              help='4 по умолчанию; не более 8 advisory-задач')
    review_batch.add_argument('--max-bytes', type=int, default=12000)
    review_import = subs.add_parser('review-import', help='Сохранить недоверенный advisory-ответ ChatGPT')
    review_import.add_argument('path', help='Путь к JSON-ответу')
    plan_import = subs.add_parser('plan-import', help='Проверить план из обычного ChatGPT и подготовить resume')
    plan_import.add_argument('path', help='Путь к JSON-плану')
    chatgpt_request = subs.add_parser(
        'chatgpt-request', aliases=['review-prompt'],
        help='Собрать готовую read-only карточку для отдельного обычного ChatGPT')
    chatgpt_request.add_argument('--json', action='store_true')
    chatgpt_request.add_argument('--max-bytes', type=int, default=12000,
                                 help='Максимум UTF-8 bytes для компактного пакета')
    chatgpt_request.add_argument('--bucket', default='chat_pro', help='ChatGPT bucket name')
    mcp_advisory = subs.add_parser('mcp-advisory', help='Запустить или диагностировать безопасный MCP advisory host')
    mcp_advisory.add_argument('action', nargs='?', choices=('diagnose', 'health', 'serve', 'deploy-diagnose', 'funnel-start'), default='diagnose')
    mcp_advisory.add_argument('--json', action='store_true')
    mcp_advisory.add_argument('--host', default='127.0.0.1', help='Bind host for serve (default: loopback)')
    mcp_advisory.add_argument('--port', type=int, default=8000, help='Bind port for serve (default: 8000)')
    mcp_advisory.add_argument('--confirm-public', action='store_true', help='Explicitly confirm public Funnel exposure')
    subs.add_parser('verify', help='Выполнить проверки без модели')
    resume_parser = subs.add_parser('resume', help='Продолжить задачу в пределах оставшихся попыток')
    resume_parser.add_argument('--after-manual-fix', action='store_true',
        help='Явно разрешить один новый цикл после M9 no-progress stop и ручного решения')
    commit_parser = subs.add_parser('commit', help='Перепроверить и сохранить результат завершённой задачи в Git')
    commit_parser.add_argument('-m', '--message', required=True)
    subs.add_parser('cancel', help='Закрыть текущую задачу, сохранив код и журнал')
    learn = subs.add_parser('learn', help='Импортировать урок из отчёта другой задачи')
    learn.add_argument('report', help='Путь к report.json из другого проекта')
    learn.add_argument('--note', required=True, help='Короткое наблюдение пользователя')
    lessons = subs.add_parser('lessons', help='Показать безопасную сводку уроков')
    lessons.add_argument('--json', action='store_true')
    export_sync = subs.add_parser('export-lessons', help='Экспортировать bounded facts из report.json')
    export_sync.add_argument('report', help='Путь к report.json')
    export_sync.add_argument('output', help='Куда записать JSON bundle')
    export_sync.add_argument('--observation', default='', help='Короткое наблюдение без кода/logs/prompts')
    export_sync.add_argument('--json', action='store_true')
    import_sync = subs.add_parser('import-lessons', help='Проверить bundle и явно принять/отклонить урок')
    import_sync.add_argument('bundle', help='Путь к JSON bundle')
    import_choice = import_sync.add_mutually_exclusive_group()
    import_choice.add_argument('--accept', action='store_true', help='Явно добавить только bounded facts в untrusted lessons context')
    import_choice.add_argument('--reject', action='store_true', help='Явно отклонить bundle')
    import_sync.add_argument('--source-project-id', help='Ожидаемый source_project_id для защиты от подмены')
    import_sync.add_argument('--json', action='store_true')
    release_check = subs.add_parser('release-check', help='Проверить release ZIP без изменения проекта')
    release_check.add_argument('archive', help='Путь к release ZIP')
    stress = subs.add_parser('stress-test', help='Параллельный deterministic fake stress-test нескольких проектов')
    stress.add_argument('--projects', type=int, default=4)
    stress.add_argument('--live', action='store_true', help='Явно запросить live режим; по умолчанию он заблокирован')
    stress.add_argument('--json', action='store_true')
    windows = subs.add_parser('windows-check', help='Read-only Windows readiness audit')
    windows.add_argument('--json', action='store_true')
    capacity = subs.add_parser('concurrency', help='Посмотреть или изменить локальную параллельность')
    capacity.add_argument('--max-concurrent', type=int)
    capacity.add_argument('--wait-seconds', type=int)
    dash = subs.add_parser('dashboard', help='Статусы нескольких проектов без запуска моделей')
    dash.add_argument('--root', dest='dashboard_projects', action='append', default=[])
    dash.add_argument('--all', action='store_true', help='Все зарегистрированные проекты; обновляется автоматически')
    dash.add_argument('--json', action='store_true')
    dash.add_argument('--timeout', type=int, default=0, help='Срок наблюдения в секундах; 0 без ограничения')
    dash.add_argument('--stop-file', help='Завершить наблюдение при появлении файла')
    dash.add_argument('--watch', action='store_true', help='Обновление каждые 2 секунды; Ctrl+C выход')
    approved = subs.add_parser('approved-plan', help='Выполнить утверждённый план без повторного planner')
    approved.add_argument('action', choices=('import', 'run', 'resume', 'status', 'reconcile'))
    approved.add_argument('value', help='JSON-файл для import; plan ID для остальных действий')
    approved.add_argument('--max-tasks', type=int, help='Остановиться после N проверенных задач')
    approved.add_argument('--accept-external', action='append', default=[], metavar='PATH',
                          help='При reconcile: проверенный посторонний путь; повторить для каждого')
    approved.add_argument('--accept-current-state', action='store_true',
                          help='При reconcile: явно принять текущий patch старого блока без сохранённых хэшей')
    approved.add_argument('--json', action='store_true')
    orch = subs.add_parser('orchestrate', help='Durable local chief/worker/verifier protocol')
    orch.add_argument('action', choices=('plan', 'task', 'claim', 'checkpoint', 'verify', 'handoff', 'status', 'report', 'registry', 'approve', 'parity'))
    orch.add_argument('--plan-id')
    orch.add_argument('--task-id')
    orch.add_argument('--prompt')
    orch.add_argument('--objective')
    orch.add_argument('--worker-id')
    orch.add_argument('--verifier-id')
    orch.add_argument('--from-role', choices=orchestration.ROLES)
    orch.add_argument('--to-role', choices=orchestration.ROLES)
    orch.add_argument('--files', action='append', default=[])
    orch.add_argument('--verify-command', action='append', default=[],
                      help='Одна команда строкой или JSON-массивом; можно повторять')
    orch.add_argument('--summary')
    orch.add_argument('--report', default='')
    orch.add_argument('--check', action='append', default=[])
    orch.add_argument('--json', action='store_true')
    orch.add_argument('--scope', choices=orchestration.APPROVAL_SCOPES)
    orch.add_argument('--actor')
    orch.add_argument('--manifest')
    args = parser.parse_args(argv)
    root = Path(args.project).expanduser().resolve()
    if not root.is_dir():
        parser.error('Папка проекта не существует.')
    try:
        if args.command == 'gui':
            from .plan_gui import main as launch_gui
            launch_gui()
            return 0
        if args.command == 'approved-plan':
            if args.action != 'reconcile' and (args.accept_external or args.accept_current_state):
                raise ValueError('--accept-external/--accept-current-state допустимы только при reconcile')
            if args.action == 'import':
                if args.max_tasks is not None:
                    raise ValueError('--max-tasks допустим только при run/resume')
                state = approved_plan.import_plan(root, args.value)
                result = approved_plan.summary(root, state['plan_id'])
            elif args.action == 'status':
                result = approved_plan.summary(root, args.value)
            elif args.action == 'reconcile':
                if args.max_tasks is not None:
                    raise ValueError('--max-tasks недопустим при reconcile')
                approved_plan.reconcile_plan(root, args.value, args.accept_external,
                                             accept_current_state=args.accept_current_state)
                result = approved_plan.summary(root, args.value)
            else:
                approved_plan.run_plan(root, args.value, max_tasks=args.max_tasks,
                                       resume=args.action == 'resume')
                result = approved_plan.summary(root, args.value)
            if args.json:
                emit(result)
            else:
                print('Approved plan %s: %s' % (result['plan_id'], result['status']))
                print('Функции: %d/%d проверено; задачи: %d/%d завершено' % (
                    sum(item['status'] == 'VERIFIED' for item in result['features']), len(result['features']),
                    sum(item['status'] == 'COMPLETED' for item in result['tasks']), len(result['tasks'])))
                print('Текущая задача: %s; осталось: %d' % (
                    result['current_task'] or 'нет', result['remaining_tasks']))
                print('Model turns: %d/%d; input tokens: %d; output tokens: %d; usage missing: %d; elapsed: %.1fs' % (
                    result['model_turns'], result['model_turn_budget'],
                    result['usage_totals']['input_tokens'], result['usage_totals']['output_tokens'],
                    result['missing_usage_records'], result['elapsed_seconds']))
                if result['reason']:
                    print('Причина остановки: ' + result['reason'])
                for item in result['tasks']:
                    print('  %s: %s, attempts=%d' % (item['id'], item['status'], item['attempts']))
            return 1 if result['status'] == 'BLOCKED' else 0
        if args.command == 'self-repair':
            source = self_repair.SOURCE
            if args.action in ('enable', 'disable'):
                emit(self_repair.configure(source, args.action == 'enable'))
            elif args.action == 'run':
                emit(self_repair.run_once(source))
            elif args.action == 'check':
                from .process import run
                from .self_health import record_safely
                code, output = run(self_repair.CHECK, source, timeout=300,
                    env=dict(os.environ, MEGAPROG_NO_MONITOR='1', MEGAPROG_SELF_REPAIR_CHILD='1'))
                print(output[-4000:])
                if code:
                    record_safely(source, {'id': 'self-check', 'updated_at': time.time()}, kind='self_test')
                    self_repair.maybe_start(source)
                return 1 if code else 0
            else:
                emit({'settings': self_repair.settings(source), 'incidents': list(self_repair.queue(source).values())})
            return 0
        if args.command == 'monitor':
            print('Native monitor: ' + native_monitor.open_monitors(root))
            return 0
        if args.command == 'worker-access':
            if sys.platform != 'win32':
                raise ValueError('worker-access поддерживается только на Windows.')
            path = root / '.ai-dev/config.json'
            config = read(path) if path.exists() else dict(supervisor.DEFAULT)
            if args.action != 'status':
                state_path = root / '.ai-dev/state.json'
                if state_path.exists() and read(state_path).get('status') not in ('COMPLETED', 'CANCELLED', 'IDLE'):
                    raise ValueError('Сначала завершите текущую задачу; доступ frozen на время задачи.')
                config['windows_worker_full_access'] = args.action == 'enable'
                write(path, config)
            emit({'project': str(root), 'windows_worker_full_access': config.get('windows_worker_full_access') is True,
                  'scope': 'Windows task worker and planner; independent review remains read-only',
                  'rollback': 'ai-dev -C PROJECT worker-access disable'})
            return 0
        if args.command in ('task', 'resume', 'verify'):
            native_monitor.auto_open(root)
        if args.command == 'release-check':
            result = check_archive(args.archive)
            print('Release archive OK: %s (version %s, %d files)' %
                  (result['archive'], result['version'], result['files']))
            return 0
        if args.command == 'stress-test':
            result = run_stress(args.projects, args.live)
            if args.json:
                emit(result)
            else:
                print('Stress mode: %s; projects: %s' % (result['mode'], result.get('projects', 0)))
                print('completed: %d; blocked: %d; errors: %d' %
                      (result['completed'], result['blocked'], result['errors']))
                for item in result.get('results', []):
                    print('  %s: %s%s' % (item.get('project', '(live)'), item['status'],
                                          (' — ' + item['reason']) if item.get('reason') else ''))
                if result.get('cross_project_contamination'):
                    print('contamination: ' + ', '.join(result['cross_project_contamination']))
            return 0
        if args.command == 'windows-check':
            result = windows_audit(root)
            if args.json:
                emit(result)
            else:
                print('Windows readiness: ' + result['overall'])
                for item in result['checks']:
                    print('%s: %s — %s' % (item['name'], item['status'], item['detail']))
                for action in result['next_actions']:
                    print('Next: ' + action)
            return 0
        if args.command == 'concurrency':
            emit(concurrency.configure(root, args.max_concurrent, args.wait_seconds))
            return 0
        if args.command == 'dashboard':
            if args.timeout < 0:
                raise ValueError('timeout должен быть >= 0')
            started = time.monotonic()
            previous_signature = None
            tick = 0
            screen = args.watch and not args.json and interactive()
            if screen:
                print('\033[?1049h\033[?25l', end='', flush=True)
            try:
                while True:
                    projects = args.dashboard_projects + (native_monitor.registered_projects() if args.all else [])
                    result = dashboard.dashboard(list(dict.fromkeys(projects)))
                    current_signature = dashboard.signature(result)
                    if args.json:
                        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                    elif screen:
                        print('\033[H\033[2J' + styled(dashboard.render(result, overview=args.all)), end='\n', flush=True)
                    elif current_signature != previous_signature:
                        print('\n' + styled(dashboard.render(result, overview=args.all)), flush=True)
                    else:
                        line = dashboard.heartbeat(result, tick)
                        if sys.stdout.isatty():
                            width = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
                            print('\r' + line[:width].ljust(width), end='', flush=True)
                        else:
                            print(line, flush=True)
                    previous_signature = current_signature
                    tick += 1
                    if not args.watch or (args.stop_file and Path(args.stop_file).exists()):
                        break
                    remaining = args.timeout - (time.monotonic() - started) if args.timeout else 2
                    if remaining <= 0:
                        break
                    time.sleep(min(2, remaining))
            except KeyboardInterrupt:
                pass
            finally:
                if screen:
                    print('\033[?25h\033[?1049l', end='', flush=True)
                    print('Монитор закрыт; рабочие задачи продолжают выполняться.', flush=True)
            return 0
        if args.command == 'orchestrate':
            if args.action == 'plan':
                commands = []
                for value in args.verify_command:
                    try:
                        parsed = json.loads(value) if value.lstrip().startswith('[') else shlex.split(value)
                    except (ValueError, TypeError) as exc:
                        raise ValueError('verify-command должен быть JSON-массивом или безопасной строкой') from exc
                    commands.append(parsed)
                result = orchestration.create_plan(root, args.objective or '', args.files, commands,
                                                   args.plan_id)
            elif args.action == 'task':
                result = orchestration.create_task(root, args.plan_id, args.prompt or '', args.files or None,
                                                   args.task_id)
            elif args.action == 'claim':
                result = orchestration.claim_task(root, args.task_id, args.worker_id)
            elif args.action == 'checkpoint':
                result = orchestration.checkpoint_task(root, args.task_id, args.worker_id,
                                                       args.summary or '', args.files)
            elif args.action == 'verify':
                result = orchestration.verify_task(root, args.task_id, args.verifier_id,
                                                   None, args.report)
            elif args.action == 'handoff':
                result = orchestration.handoff_task(root, args.task_id, args.from_role, args.to_role)
            elif args.action == 'status':
                result = orchestration.status(root, args.plan_id)
            elif args.action == 'registry':
                result = orchestration.registry(root)
            elif args.action == 'approve':
                result = orchestration.approve(root, args.scope, args.actor, args.plan_id, args.task_id)
            elif args.action == 'parity':
                result = orchestration.parity_check(root, args.manifest)
            else:
                result = orchestration.handoff_report(root, args.plan_id)
            if args.json:
                emit(result)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('status') not in ('BLOCKED',) else 1
        if args.command == 'mcp-advisory':
            if args.action == 'deploy-diagnose':
                deployment = diagnose_deployment(root)
                if args.json:
                    emit(deployment)
                else:
                    print('Deployment: ' + deployment['state'])
                    print('Tailscale client: ' + ('available' if deployment['client']['available'] else 'missing'))
                    print('Login: ' + deployment['login']['state'])
                    print('Hostname: ' + (deployment['hostname'] or 'unavailable'))
                    print('HTTPS/Funnel: %s/%s' % (deployment['https']['capable'], deployment['funnel']['capable']))
                    print('Next step: ' + deployment['next_step'])
                    if deployment.get('endpoint_template'):
                        print('URL template: ' + deployment['endpoint_template'])
                return 0
            if args.action == 'funnel-start':
                result = start_funnel(root, args.port, args.confirm_public)
                if result.get('state') == 'BLOCKED':
                    if args.json:
                        emit(result)
                    else:
                        print('Deployment: BLOCKED')
                        print(result['next_step'])
                    return 0
                if args.json:
                    emit(result)
                return 0
            status = runtime_status(root)
            status['bind'] = {'host': args.host, 'port': args.port}
            if args.action == 'health' and status['available']:
                status['discovery'] = discover_mcp_tools(root)
            if args.action == 'serve':
                run_mcp_host(root, args.host, args.port)
                return 0
            if args.json:
                emit(status)
            else:
                print('MCP/Apps SDK advisory: ' + ('available' if status['available'] else 'BLOCKED'))
                print(status['reason'])
                print('Permitted tools: ' + ', '.join(status['tools']))
                manifest = status.get('manifest') or {}
                print('Host manifest: ' + ('valid' if manifest.get('tools') else 'INVALID') +
                      ' (' + manifest.get('path', str(root / HOST_MANIFEST_FILE)) + ')')
                if args.action == 'health':
                    if status['available']:
                        print('Discovery: ' + ', '.join(status['discovery']['tools']))
                    else:
                        print('Health is blocked until the official optional SDK and Python 3.10+ are available.')
            return 0
        if args.command == 'review-batch':
            bundle = build_review_batch(root, args.max_items, args.max_bytes)
            if args.json:
                emit(bundle)
            else:
                print(render_review_batch(bundle))
            return 0
        if args.command == 'review-import':
            artifact = import_review(root, args.path)
            print('%s: %s (%s)' % (artifact['status'], artifact['task_id'],
                                   root / '.ai-dev' / 'reviews'))
            return 0
        if args.command == 'export-lessons':
            bundle = export_lessons(root, args.report, args.output, args.observation)
            result = {'status': 'EXPORTED', 'output': str(Path(args.output).expanduser().resolve()),
                      'source_project_id': bundle['source_project_id'],
                      'report_digest': bundle['report_digest'], 'fields': sorted(bundle['report_facts'])}
            if args.json:
                emit(result)
            else:
                print('Lessons export: EXPORTED')
                print('Bundle: %s' % result['output'])
                print('Source project: %s; report digest: %s' %
                      (result['source_project_id'], result['report_digest']))
            return 0
        if args.command == 'import-lessons':
            import_decision = 'accept' if args.accept else ('reject' if args.reject else None)
            result = import_lessons(root, args.bundle, import_decision, args.source_project_id)
            if args.json:
                emit(result)
            else:
                print('Lessons import: %s' % result['status'])
                print('Bundle: %s' % result.get('bundle_id', result.get('entry', {}).get('bundle_id', '(unknown)')))
                print('Причина: %s' % result.get('reason', 'duplicate'))
            return 0
        if args.command == 'chatgpt-limits':
            changed = any(value is not None for value in
                          (args.status, args.limit_turns, args.reserved_turns,
                           args.spent_turns, args.reset_at, args.observed_at,
                           args.source, args.confidence, args.notes, args.used,
                           args.remaining, args.default_bucket))
            result = update_chatgpt_limits(
                root, bucket=args.bucket, status=args.status, limit_turns=args.limit_turns,
                reserved_turns=args.reserved_turns, spent_turns=args.spent_turns,
                reset_at=args.reset_at, observed_at=args.observed_at, source=args.source,
                confidence=args.confidence, notes=args.notes, used=args.used,
                remaining=args.remaining, default_bucket=args.default_bucket) if changed else chatgpt_limit_report(root)
            if args.json:
                emit(result)
            else:
                print('ChatGPT only (Codex/Work separate): %s [%s]' % (result['status'], result['bucket']))
                print('Лимит известен: %s; лимит turns: %s; reserved: %d; spent: %d' %
                      (result['limit_known'], result['limit_turns'] if result['limit_known'] else 'unknown',
                       result['reserved_turns'], result['spent_turns']))
                print('reset_at: %s' % (result['reset_at'] or 'не задан'))
                print('Действие: ' + result['action'])
                print('Buckets: ' + ', '.join('%s=%s' % (name, item['status'])
                                               for name, item in result['buckets'].items()))
            return 0
        if args.command in ('chatgpt-request', 'review-prompt'):
            card = build_chatgpt_request(root, args.max_bytes, args.bucket)
            if args.json:
                emit(card)
            else:
                print(render_chatgpt_request(card))
            return 0
        if args.command == 'status':
            path = root / '.ai-dev/state.json'
            state = read(path) if path.exists() else {'status': 'IDLE'}
            configured = read(root / '.ai-dev/config.json') if (root / '.ai-dev/config.json').exists() else {}
            state['worker_access'] = ('FULL_ACCESS' if
                (state.get('config') or configured).get('windows_worker_full_access') is True else 'WORKSPACE')
            if args.json:
                emit(state)
            else:
                print('Состояние: ' + state['status'])
                print('Доступ Windows worker: ' + state['worker_access'])
                if 'prompt' in state:
                    print('Задача: ' + state['prompt'])
                    print('Попытки: %s/%s' % (state['attempts'], state['config']['max_attempts']))
                if state.get('decisions'):
                    d = state['decisions'][-1]
                    print('Модель: %s / %s' % (d['model'], d['reasoning']))
                for key in ('reason', 'last_run'):
                    if state.get(key):
                        print(state[key])
                if state.get('result', {}).get('message'):
                    print(state['result']['message'])
            return 0
        if args.command == 'handoff':
            from . import chat_continuity
            if args.check and args.output:
                raise ValueError('Use --check or --output, not both.')
            if args.check:
                result = chat_continuity.check(root, read(Path(args.check)))
                emit(result)
                return 0 if result['status'] == 'FRESH' else 2
            emit(chat_continuity.export(root, args.output))
            return 0
        if args.command == 'context':
            if args.check_stale:
                try:
                    prior = json.loads(Path(args.check_stale).expanduser().read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    raise ValueError('Не удалось прочитать capsule JSON: %s' % exc)
                result = coordinator_context.is_stale(root, prior)
                if args.json:
                    emit(result)
                else:
                    print('Context: %s' % ('STALE' if result['stale'] else 'FRESH'))
                    print('Причины: %s' % (', '.join(result['reasons']) or 'нет'))
                    print('HEAD: %s | задача: %s | последнее событие: %s' %
                          (result['current_head'] or 'unknown', result['current_task_id'] or 'none',
                           result['latest_event_id'] or 'none'))
                return 0
            result = coordinator_context.build_capsule(
                root, task_id=args.task, component=args.component, budget=args.budget,
                since_event_id=args.since_event_id, checkpoint_id=args.checkpoint)
            checkpoint_result = None
            if args.save_checkpoint:
                with lock(root):
                    checkpoint_result = coordinator_context.save_checkpoint(
                        root, args.save_checkpoint, result)
            if args.json:
                output = ({'capsule': result, 'checkpoint': checkpoint_result}
                          if args.save_checkpoint else result)
                print(json.dumps(output, ensure_ascii=False, separators=(',', ':')))
            else:
                print(coordinator_context.render_capsule(result))
                if args.save_checkpoint:
                    print('Checkpoint saved: %s' % args.save_checkpoint)
            return 0
        if args.command == 'evidence':
            if args.evidence_action == 'build':
                packet = evidence_packet.build_packet(root, task_id=args.task_id,
                    goal=args.goal, budget=args.budget, max_files=args.max_files,
                    initial_evidence_seeds=(
                        [{'kind': 'path', 'query': path, 'priority': 'required',
                          'reason': 'Explicit CLI initial evidence seed'} for path in args.evidence_seed] +
                        [{'kind': 'path', 'query': path, 'priority': 'optional',
                          'reason': 'Explicit CLI optional evidence seed'} for path in args.optional_evidence_seed]
                        if args.evidence_seed or args.optional_evidence_seed else None))
                path = evidence_packet.save_packet(root, packet)
                if args.json:
                    emit({'packet': packet, 'saved_to': str(path)})
                else:
                    print(evidence_packet.render_packet(packet))
                    print('Saved: %s' % path)
                return 0
            raw_path = Path(args.packet).expanduser()
            if not raw_path.exists():
                raw_path = root / '.ai-dev' / 'evidence' / (args.packet + '.json')
            packet = evidence_packet.load_packet(raw_path)
            if args.evidence_action == 'check':
                result = evidence_packet.check_packet(root, packet)
            else:
                result = packet
            if args.json:
                emit(result)
            elif args.evidence_action == 'check':
                print('Evidence Packet: %s' % result['status'])
                for reason in result.get('reasons', []):
                    print('  ' + reason)
            else:
                print(evidence_packet.render_packet(packet))
            return 0 if args.evidence_action != 'check' or result['status'] == 'VALID' else 1
        if args.command == 'usage-report':
            summary = aggregate(root, args.root)
            if args.json:
                emit(summary)
            else:
                print('Источник: записанное локальное usage (recorded local usage)')
                print(summary['disclaimer'])
                print('Задач: %d; попыток: %d' %
                      (summary['totals']['tasks'], summary['totals']['attempts']))
                print('Статусы: completed %d, blocked %d, failed %d; retry: задач %d, попыток %d' %
                      (summary['totals']['completed'], summary['totals']['blocked'],
                       summary['totals']['failed'], summary['totals']['retry_tasks'],
                       summary['totals']['retry_attempts']))
                print('Токены: %d; доли — только от записанных локальных токенов' %
                      summary['totals']['recorded_tokens'])
                for group in summary['groups']:
                    percent = ('неизвестно' if group['percent_of_recorded_tokens'] is None else
                               '%.2f%%' % group['percent_of_recorded_tokens'])
                    print('  %s / %s / %s: задач %d, попыток %d, токенов %d (%s)' %
                          (group['model'], group['reasoning'], group['kind'], group['tasks'],
                           group['attempts'], group['recorded_tokens'], percent))
                    print('    статус: completed %d, blocked %d, failed %d; retry: %d/%d; reason: %s' %
                          (group['completed'], group['blocked'], group['failed'],
                           group['retry_tasks'], group['retry_attempts'],
                           ', '.join(group['routing_reasons']) or '(unknown)'))
                print('Отсутствует usage: задач %d, попыток %d' %
                      (summary['missing_usage']['tasks'], summary['missing_usage']['attempts']))
                print('Account guard blocks: %d; fallback: %d; escalation: %d' %
                      (summary['guard_blocks'], summary['routing_events']['fallback']['attempts'],
                       summary['routing_events']['escalation']['attempts']))
                print('Неизвестная модель: задач %d, попыток %d' %
                      (summary['unknown_model']['tasks'], summary['unknown_model']['attempts']))
                print('Spark: выбрано задач %d, пропущено %d; причины: %s' %
                     (summary['spark']['selected_tasks'], summary['spark']['skipped_tasks'],
                       ', '.join(summary['spark']['skipped_reasons']) or '(unknown)'))
                print('Review imports: %d; accepted hints %d; stale %d' %
                      (summary['review_imports']['count'], summary['review_imports']['accepted_as_hint'],
                       summary['review_imports']['stale']))
                print('Lessons sync: accepted %d, pending %d, rejected %d; sources: %s' %
                      (summary['lessons_sync']['accepted'], summary['lessons_sync']['pending'],
                       summary['lessons_sync']['rejected'],
                       ', '.join('%s (%d)' % item for item in summary['lessons_sync']['by_source_project'].items()) or 'none'))
                print('ChatGPT advisory ledger: %s (spent %d, reserved %d)' %
                      (summary['chatgpt_limits']['status'],
                       summary['chatgpt_limits']['spent_turns'],
                       summary['chatgpt_limits']['reserved_turns']))
                print('Improvement hints:')
                for hint in summary['improvement_hints']:
                    print('  ' + hint)
                print('Историческая account-level разбивка: отсутствует')
                if summary['errors']:
                    print('Ошибок чтения: %d' % len(summary['errors']))
            return 0
        if args.command == 'lessons':
            summary = lesson_summary(root)
            summary['sync'] = sync_summary(root)
            if args.json:
                emit(summary)
            else:
                print('Уроков: %d' % summary['count'])
                print('Источники:')
                for item in summary['sources']:
                    print('  %s (%d)' % (item['value'], item['count']))
                if not summary['sources']:
                    print('  нет')
                print('Статусы: ' + (', '.join('%s (%d)' % pair for pair in summary['statuses'].items()) or 'нет'))
                for label, key in (('Повторяющиеся ошибки', 'repeated_errors'),
                                   ('Повторяющиеся наблюдения', 'repeated_observations')):
                    print(label + ':')
                    if summary[key]:
                        for item in summary[key]:
                            print('  %s (%d)' % (item['value'], item['count']))
                    else:
                        print('  нет')
                print('Sync: accepted %d, pending %d, rejected %d; источники: %s' %
                      (summary['sync']['accepted'], summary['sync']['pending'], summary['sync']['rejected'],
                       ', '.join('%s (%d)' % item for item in summary['sync']['by_source_project'].items()) or 'нет'))
            return 0
        with lock(root), self_repair.activity(root, enabled=args.command in ('task', 'resume', 'verify')):
            if args.command == 'plan-import':
                emit(import_plan(root, args.path))
                return 0
            if args.command in ('init', 'check'):
                path = root / '.ai-dev/config.json'
                config = read(path) if path.exists() else dict(supervisor.DEFAULT)
                state_path = root / '.ai-dev/state.json'
                if state_path.exists() and read(state_path)['status'] not in ('COMPLETED', 'CANCELLED', 'IDLE'):
                    raise ValueError('Сначала завершите или отмените текущую задачу.')
                if args.command == 'check':
                    command = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
                    if not command:
                        raise ValueError('Пример: ai-dev check python3 -m unittest discover -v')
                    config['verify'] = [command]
                elif args.auto:
                    config['model'] = 'auto'
                write(path, config)
                ignore = root / '.gitignore'
                existing = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
                if '.ai-dev/' not in existing.splitlines():
                    ignore.write_text(existing + ('\n' if existing and not existing.endswith('\n') else '') + '.ai-dev/\n', encoding='utf-8')
                print('Настройки: ' + str(path))
                if not config['verify']:
                    print('Задайте проверку: ai-dev check python3 -m unittest discover -v')
                return 0
            if args.command == 'commit':
                return supervisor.commit(root, args.message)
            if args.command == 'learn':
                lesson = import_lesson(root, args.report, args.note)
                print('Урок сохранён: %s (%s)' % (lesson['source'], lesson['status']))
                print('Журнал: %s' % (root / LESSONS_FILE))
                return 0
            if args.command == 'cancel':
                path = root / '.ai-dev/state.json'
                state = read(path)
                if state['status'] in ('COMPLETED', 'CANCELLED'):
                    raise ValueError('Задача уже закрыта.')
                state.update(status='CANCELLED', reason='Закрыта пользователем; файлы и журналы сохранены.')
                write(path, state)
                write(root / '.ai-dev/runs' / state['id'] / 'state.json', state)
                print(state['reason'])
                return 0
            if args.command in ('models', 'limits', 'plan'):
                report, catalog = get_catalog(root)
                if args.command == 'plan':
                    emit(decision(root, catalog, args.prompt, args))
                elif args.command == 'models':
                    for model in catalog['models']:
                        print('%s: %s' % (model['model'], ', '.join(e['reasoningEffort'] for e in model['supportedReasoningEfforts'])))
                    if not catalog['models']:
                        raise ValueError(str(catalog['errors']))
                    print('Каталог получен; реальная доступность подтверждается при выполнении задачи.')
                else:
                    limits = catalog['limits']
                    if not limits:
                        raise ValueError('Лимиты недоступны: ' + str(catalog['errors']))
                    buckets = limits.get('rateLimitsByLimitId') or {'codex': limits.get('rateLimits')}
                    for key, bucket in buckets.items():
                        if not bucket:
                            continue
                        print(bucket.get('limitName') or key)
                        for name in ('primary', 'secondary'):
                            window = bucket.get(name)
                            if window and window.get('usedPercent') is not None:
                                print('  Окно %s мин: осталось %g%%; сброс %s' %
                                      (window.get('windowDurationMins'), max(0, min(100, 100-window['usedPercent'])), datetime.fromtimestamp(window['resetsAt']).astimezone().strftime('%d.%m %H:%M %Z') if window.get('resetsAt') else 'неизвестен'))
                return 0
            if args.command == 'doctor':
                report = codex.doctor(root)
                config_path = root / '.ai-dev/config.json'
                configured = read(config_path) if config_path.exists() else {}
                report['windows_worker_full_access'] = configured.get('windows_worker_full_access') is True
                report['worker_access_scope'] = 'Windows task worker and planner; independent review remains read-only'
                if report['ready']:
                    catalog = discover(root, report['executable'])
                    write(root / '.ai-dev/catalog.json', catalog)
                    report['model_catalog'] = [m['model'] for m in catalog['models']]
                    report['usage'] = 'available' if catalog['limits'] else 'unavailable'
                    report['discovery_errors'] = catalog['errors']
                if args.probe:
                    report['probe'] = {'skipped': True,
                                       'reason': 'doctor никогда не запускает модель'}
                write(root / '.ai-dev/doctor.json', report)
                emit({k: v for k, v in report.items() if not k.endswith('_help')})
                return 0 if report['ready'] else 1
            if args.command == 'verify':
                config = supervisor.configure(root)
                result = supervisor.verify(root, config, root / '.ai-dev')
                write(root / '.ai-dev/verification.json', result)
                emit(result)
                return 0 if result['ok'] else 1
            if args.command == 'task':
                seed_rows = (
                    [{'kind': 'path', 'query': path, 'priority': 'required',
                      'reason': 'Explicit CLI initial evidence seed'} for path in args.evidence_seed] +
                    [{'kind': 'path', 'query': path, 'priority': 'optional',
                      'reason': 'Explicit CLI optional evidence seed'} for path in args.optional_evidence_seed])
                kwargs = {'initial_evidence_seeds': seed_rows} if seed_rows else {}
                return supervisor.task(root, args.prompt, args.kind, args.model, args.reasoning,
                                       getattr(args, 'orchestration_task_id', None),
                                       getattr(args, 'pin_scope', None), **kwargs)
            return supervisor.task(root, allow_no_progress_resume=
                                   bool(getattr(args, 'after_manual_fix', False)))
    except KeyboardInterrupt:
        print('Остановлено пользователем.', file=sys.stderr)
        return 130
    except evidence_packet.EvidenceSeedError as exc:
        emit(exc.as_dict())
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print('Ошибка: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if args.command in ('task', 'resume', 'verify'):
            try:
                self_repair.maybe_start()
            except (OSError, ValueError):
                print('Саморемонт отложен: не удалось запустить фоновую проверку.', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
