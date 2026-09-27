import json
import os
import shutil
import re
import time
from pathlib import Path
from .process import run, WorkerStalled
from .telemetry import make_turn, runtime_model, runtime_reasoning


def environment():
    env = dict(os.environ)
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL'):
        env.pop(key, None)
    return env


def doctor(root):
    override = os.environ.get('AI_DEV_CODEX')
    candidates = [override] if override else [shutil.which('codex'),
        '/Applications/ChatGPT.app/Contents/Resources/codex',
        '/Applications/Codex.app/Contents/Resources/codex']
    available = []
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            code, version = run([candidate, '--version'], root)
            match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
            if code == 0 and match:
                available.append((tuple(map(int, match.groups())), candidate))
    exe = max(available)[1] if available else None
    report = {'executable': exe, 'ready': False, 'usage': 'run ai-dev limits',
              'model_catalog': 'run ai-dev models', 'model_runtime': 'not probed',
              'api_fallback': False, 'fast_mode': False}
    if not exe:
        return report
    for name, args in [('version', ['--version']), ('exec_help', ['exec', '--help']),
                       ('resume_help', ['exec', 'resume', '--help']),
                       ('login', ['login', 'status'])]:
        code, output = run([exe] + args, root, env=environment())
        report[name] = output.strip() if code == 0 else 'unavailable'
    required = ['--json', '--model', '--ignore-user-config', '--strict-config']
    report['exec'] = all(flag in report['exec_help'] for flag in required)
    report['resume'] = all(flag in report['resume_help'] for flag in required)
    report['full_access_supported'] = all('--dangerously-bypass-approvals-and-sandbox' in report[name]
                                          for name in ('exec_help', 'resume_help'))
    report['chatgpt_login'] = 'Logged in using ChatGPT' in report['login']
    report['ready'] = report['exec'] and report['chatgpt_login']
    config_path = Path(root) / '.ai-dev/config.json'
    if os.name == 'nt' and config_path.exists():
        try:
            opted_in = json.loads(config_path.read_text(encoding='utf-8')).get('windows_worker_full_access') is True
        except (OSError, ValueError, AttributeError):
            opted_in = False
        if opted_in and not report['full_access_supported']:
            report['ready'] = False
            report['full_access_error'] = 'Codex exec/resume do not support the requested full-access flag.'
    return report


def command(exe, config, session=None, sandbox='workspace-write'):
    from .routing import validate_model_turn
    validate_model_turn(config.get('model'), config.get('reasoning'))
    full_access = (os.name == 'nt' and config.get('windows_worker_full_access') is True and
                   (sandbox == 'workspace-write' or
                    (sandbox == 'read-only' and config.get('_megaprog_task_planner') is True)))
    permission_profile = {'workspace-write': ':workspace', 'read-only': ':read-only'}.get(sandbox)
    if permission_profile is None:
        raise ValueError('Unsupported Codex sandbox: ' + str(sandbox))
    argv = [exe, 'exec']
    if session:
        argv += ['resume']
    argv += ['--ignore-user-config', '--strict-config', '--json', '--model', config['model']]
    if full_access:
        argv += ['--dangerously-bypass-approvals-and-sandbox']
    for key, value in {'model_reasoning_effort': config['reasoning'],
                       'model_provider': 'openai', 'forced_login_method': 'chatgpt',
                       'service_tier': 'default',
                       'approval_policy': 'never'}.items():
        argv += ['-c', key + '=' + json.dumps(value)]
    if not full_access:
        argv += ['-c', 'default_permissions=' + json.dumps(permission_profile)]
    if session:
        argv += [session]
    argv += ['-']
    return argv


def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
    from .live import LiveSignal
    # The old default is retired even in existing task snapshots. Do not
    # rewrite their config or break resume equality checks.
    timeout = config['timeout_seconds']
    if timeout == 900:
        timeout = 0
    stalled = False
    live = LiveSignal(root, log, verification_commands=(config.get('verify')
                      if config.get('detect_stall') and sandbox != 'read-only' else None))
    started = time.monotonic()
    try:
        code, output = run(command(report['executable'], config, session, sandbox), root,
                           timeout=timeout, input_text=prompt, env=environment(), output_path=log,
                           live=live)
    except WorkerStalled:
        stalled = True
        code, output = 1, log.read_text(encoding='utf-8', errors='replace')
    log.write_text(output, encoding='utf-8')
    result = {'exit_code': code, 'session_id': session, 'completed': False, 'usage': None}
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        events.append(event)
        if event.get('type') == 'thread.started':
            result['session_id'] = event['thread_id']
        elif event.get('type') == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
            result['message'] = event['item'].get('text', '')
        elif event.get('type') == 'turn.completed':
            result['completed'] = True
            result['usage'] = event.get('usage')
        elif event.get('type') in ('turn.failed', 'error'):
            result['error'] = event
    result['ok'] = code == 0 and result['completed'] and 'error' not in result
    result['telemetry'] = make_turn(
        configured_model=config.get('model'),
        configured_reasoning=config.get('reasoning'),
        observed_model=runtime_model(events),
        observed_reasoning=runtime_reasoning(events),
        usage=result.get('usage'),
        duration_seconds=round(max(0.0, time.monotonic() - started), 3),
        session_id=result.get('session_id'),
        result_status='COMPLETED' if result['ok'] else 'FAILED')
    if stalled:
        result['stalled'] = True
        result['error'] = 'Two configured verification failures in one model turn.'
        result['stall_verification'] = live.stall_verification()
    return result
