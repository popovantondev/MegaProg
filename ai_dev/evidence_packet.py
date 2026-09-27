"""Deterministic, bounded evidence packets for planning (no model calls)."""

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from .storage import read, write

SCHEMA_VERSION = 1
DEFAULT_BUDGET = 28672
MIN_BUDGET = 4096
MAX_BUDGET = 49152
MAX_SOURCE_FILE = 512 * 1024
MAX_PACKET_HASH_BYTES = 128 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_FRAGMENT_BYTES = 1200
MAX_SEEDED_FRAGMENT_BYTES = 720
MAX_INITIAL_EVIDENCE_SEEDS = 32
TASK_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$')
ALWAYS_EXCLUDED_PARTS = {'.git', '.ai-dev', 'node_modules', 'vendor', 'coverage',
                         '__pycache__', '.venv'}
GENERATED_PARTS = {'dist', 'build', 'target', 'releases', 'release', 'cache', '.generated'}
SOURCE_SUFFIXES = {'.py', '.pyi', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx',
                   '.rs', '.go', '.java', '.kt', '.swift', '.c', '.h', '.cpp', '.hpp',
                   '.cs', '.html', '.css', '.scss', '.sql', '.sh', '.ps1', '.strings'}
REFERENCE_SUFFIXES = {'.md', '.rst', '.txt'}
TEST_MARKERS = ('test', 'spec', 'qa')
EXPLICIT_PATH = re.compile(
    r'(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:pyi?|js|mjs|cjs|ts|tsx|jsx|rs|go|java|kt|swift|c|h|cpp|hpp|cs|html|css|scss|sql|sh|ps1|md|rst|txt|json|ya?ml|toml))(?![A-Za-z0-9_.-])',
    re.IGNORECASE)
_SEED_SHELL_SYNTAX = re.compile(r"[$`;&|<>*?!{}\[\]\x00\r\n]")


class EvidenceSeedError(ValueError):
    """Structured pre-planner seed failure; no planner turn may follow it."""
    def __init__(self, code, details):
        self.code = code
        self.details = details if isinstance(details, dict) else {}
        super().__init__(code + ': ' + str(self.details.get('reason') or code))

    def as_dict(self):
        rejected = self.details.get('rejected', [])
        return {'status': 'BLOCKED', 'failure_code': self.code,
                'rejected_seed_count': len(rejected) if isinstance(rejected, list) else 0,
                **self.details}


def validate_initial_evidence_seeds(seeds):
    """Validate the canonical seed schema without applying planner lookup limits."""
    if seeds is None:
        return []
    if not isinstance(seeds, list) or len(seeds) > MAX_INITIAL_EVIDENCE_SEEDS:
        raise EvidenceSeedError('PREPLANNER_EVIDENCE_SEED_INVALID', {
            'reason': 'initial_evidence_seeds must be a list of at most %d path seeds.' %
                      MAX_INITIAL_EVIDENCE_SEEDS,
            'required_seed_count': sum(isinstance(row, dict) and row.get('priority') == 'required'
                                       for row in seeds) if isinstance(seeds, list) else 0,
            'rejected': [{'index': None, 'reason': 'seed count/type limit exceeded'}]})
    accepted, rejected, seen = [], [], set()
    for index, row in enumerate(seeds):
        reason = ''
        if not isinstance(row, dict) or set(row) != {'kind', 'query', 'priority', 'reason'}:
            reason = 'seed must contain exactly kind, query, priority, and reason'
        else:
            query = row.get('query')
            priority = row.get('priority')
            if row.get('kind') != 'path':
                reason = 'only kind=path is supported'
            elif priority not in ('required', 'optional'):
                reason = 'priority must be required or optional'
            elif not isinstance(query, str) or not query.strip() or len(query) > 512:
                reason = 'query must be a non-empty repository-relative path of at most 512 characters'
            elif _SEED_SHELL_SYNTAX.search(query):
                reason = 'query contains shell syntax or a control character'
            elif not _safe_rel(query, allow_generated=True):
                reason = 'query is not a safe repository-relative path'
            elif not isinstance(row.get('reason'), str) or not row['reason'].strip() or len(row['reason']) > 300:
                reason = 'reason must contain 1–300 characters'
        if reason:
            rejected.append({'index': index, 'seed': row if isinstance(row, dict) else None,
                             'reason': reason})
            continue
        value = {'kind': 'path', 'query': _safe_rel(query, allow_generated=True),
                 'priority': priority, 'reason': _clean(row['reason'], 300)}
        key = value['query'].casefold()
        if key in seen:
            rejected.append({'index': index, 'seed': value, 'reason': 'duplicate seed path'})
        else:
            accepted.append(value)
            seen.add(key)
    if rejected:
        raise EvidenceSeedError('PREPLANNER_EVIDENCE_SEED_INVALID', {
            'reason': 'one or more initial evidence seeds were rejected',
            'required_seed_count': sum(row.get('priority') == 'required' for row in accepted),
            'valid_seed_count': len(accepted), 'rejected': rejected})
    return accepted


def _git(root, *args):
    try:
        return subprocess.check_output(['git', *args], cwd=str(root), text=True,
                                       stderr=subprocess.DEVNULL, timeout=5).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8'))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _file_fingerprint(path):
    try:
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        return _digest(path.read_bytes())
    except OSError:
        return None


def _task_contract_fingerprint(state):
    if not isinstance(state, dict):
        return None
    contract = {key: state.get(key) for key in ('id', 'prompt', 'base_head', 'routing',
                                                'initial_evidence_seeds')}
    config = state.get('config')
    if isinstance(config, dict):
        contract['verification'] = config.get('verify')
        contract['strong_planning'] = config.get('strong_planning')
    return _digest(json.dumps(contract, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')).encode('utf-8'))


def _bounded_file_hash(path, remaining=MAX_PACKET_HASH_BYTES):
    try:
        size = path.stat().st_size
        if size > remaining:
            return None, size, 'SIZE_LIMIT'
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while True:
                chunk = stream.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest(), size, 'OK'
    except OSError:
        return None, None, 'UNREADABLE'


def _clean(value, limit=1600):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())[:limit]


def _tokens(text):
    values = re.findall(r'[A-Za-zА-Яа-яЁё0-9_]{3,}', (text or '').casefold())
    stop = {'the', 'and', 'for', 'with', 'from', 'that', 'this', 'into', 'then',
            'from', 'keep', 'only', 'must', 'should', 'test', 'tests', 'file', 'files',
            'после', 'чтобы', 'который', 'также', 'задача', 'проект', 'проверить', 'должен'}
    return sorted(set(value for value in values if value not in stop))


def _safe_rel(value, allow_generated=False):
    if not isinstance(value, str):
        return None
    value = value.replace('\\', '/').strip()
    path = Path(value)
    if path.is_absolute() or not value or '..' in path.parts or ':' in value:
        return None
    parts = [part.casefold() for part in path.parts]
    if any(part in ALWAYS_EXCLUDED_PARTS for part in parts):
        return None
    if not allow_generated and _generated_path(parts):
        return None
    return path.as_posix()


def _generated_path(parts):
    return any(part in GENERATED_PARTS or part.startswith('releases.during-turn-')
               for part in parts)


def _windows_specific_path(rel):
    low = rel.casefold()
    parts = Path(low).parts
    if any(part in ('windows', 'win32', 'win64', 'windows-only') for part in parts):
        return True
    name = Path(low).name
    return (name.endswith(('.ps1', '.cmd', '.bat')) or name.startswith(('windows_', 'win_')) or
            '_windows.' in name or '.windows.' in name)


def _shared_contract_path(rel):
    markers = ('shared', 'contract', 'schema', 'protocol', 'serialization', 'portable')
    return any(marker in part.casefold() for part in Path(rel).parts for marker in markers)


def _windows_deferred(text):
    low = (text or '').casefold()
    patterns = (r'windows.{0,50}(?:deferred|postponed|later|out of scope|не входит|отлож)',
                r'(?:deferred|postponed|out of scope|отлож).{0,50}windows',
                r'сборк\w* windows.{0,40}(?:позже|отлож|не входит)',
                r'windows.{0,40}(?:позже|отлож|не входит)')
    return any(re.search(pattern, low) for pattern in patterns)


def _platform_scope(text, host):
    low = (text or '').casefold()
    deferred = ['windows'] if _windows_deferred(low) else []
    active = 'macos' if host == 'darwin' else ('windows' if host == 'windows' else host)
    shared = []
    if re.search(r'(?:preserv\w*|keep|maintain|сохран\w*|остав\w*).{0,50}windows.{0,35}(?:support|поддерж)', low):
        shared.append('Preserve existing Windows support; do not create a Windows build in this task.')
    explicit = bool(deferred or re.search(r'\bmacos only\b|\bonly macos\b|только macos|только mac\b', low))
    return {'active_platform': active, 'deferred_platforms': deferred,
            'shared_invariants': shared, 'explicit': explicit,
            'source': 'task_or_plan' if explicit else 'host_only'}


def _ignored_paths(root, paths):
    if not paths:
        return set()
    try:
        result = subprocess.run(['git', 'check-ignore', '--no-index', '-z', '--stdin'],
            cwd=str(root), input='\0'.join(paths) + '\0', text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        return set(result.stdout.split('\0')) - {''}
    except (OSError, subprocess.SubprocessError):
        return set()


def _validate_evidence_requests(requests):
    if not isinstance(requests, list) or len(requests) > 8:
        raise ValueError('Evidence requests must be an array of at most 8 items.')
    result, seen = [], set()
    for row in requests:
        if not isinstance(row, dict) or set(row) - {'kind', 'query', 'reason'}:
            raise ValueError('Malformed evidence request.')
        kind = row.get('kind')
        query = _clean(row.get('query'), 240)
        reason = _clean(row.get('reason'), 300)
        if kind not in ('path', 'symbol', 'test', 'reference') or not query or not reason:
            raise ValueError('Evidence request needs a supported kind, query, and reason.')
        if kind == 'path':
            query = _safe_rel(query, allow_generated=True)
            if not query:
                raise ValueError('Evidence path request is not a safe repository-relative path.')
        key = (kind, query.casefold())
        if key not in seen:
            result.append({'kind': kind, 'query': query, 'reason': reason})
            seen.add(key)
    return result


def _load_task(root, task_id=None):
    candidates = []
    canonical = root / '.ai-dev/state.json'
    if canonical.is_file():
        candidates.append(canonical)
    if task_id:
        candidates.append(root / '.ai-dev' / 'runs' / str(task_id) / 'state.json')
    for path in candidates:
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            value = read(path)
            if isinstance(value, dict) and (not task_id or str(value.get('id')) == str(task_id)):
                return value, path.relative_to(root).as_posix()
        except (OSError, ValueError, TypeError):
            continue
    return None, None


def _dirty_snapshot(root):
    try:
        output = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'],
                                         cwd=str(root), text=True, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {'status': 'UNKNOWN', 'paths': [], 'hash': None}
    entries = []
    hashed_bytes = 0
    complete = True
    for line in output.splitlines():
        if len(line) < 4:
            continue
        rel = _safe_rel(line[3:].split(' -> ')[-1])
        if not rel:
            continue
        path = root / rel
        if path.is_file():
            content_hash, size, hash_status = _bounded_file_hash(
                path, max(0, MAX_PACKET_HASH_BYTES - hashed_bytes))
            hashed_bytes += size or 0
        else:
            content_hash, size, hash_status = None, None, 'MISSING_OR_UNTRACKED_DELETE'
        if hash_status != 'OK' and hash_status != 'MISSING_OR_UNTRACKED_DELETE':
            complete = False
        entries.append({'path': rel, 'index_status': line[:2], 'content_hash': content_hash,
                        'hash_status': hash_status, 'size_bytes': size})
    entries.sort(key=lambda row: row['path'])
    return {'status': 'DIRTY' if entries else 'CLEAN', 'paths': entries,
            'hash_complete': complete,
            'hash': (_digest(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode())
                     if complete else None)}


def _plan_scope(state):
    plan = state.get('plan') if isinstance(state, dict) else None
    if not isinstance(plan, dict):
        return [], [], []
    steps = plan.get('steps') if isinstance(plan.get('steps'), list) else []
    index = state.get('checkpoint_index', 0)
    if type(index) is int and 0 <= index < len(steps):
        selected = steps[index:index + 1]
    else:
        selected = steps[:4]
    files, acceptance, titles = [], [], []
    for step in selected:
        if not isinstance(step, dict):
            continue
        titles.append(_clean(step.get('title'), 300))
        acceptance.append(_clean(step.get('acceptance'), 1200))
        for name in step.get('files') or []:
            rel = _safe_rel(name, allow_generated=True)
            if rel and rel not in files:
                files.append(rel)
    return files[:24], acceptance[:4], titles[:4]


def _explicit_task_paths(text):
    """Extract only literal repository path spellings from the task text."""
    paths = []
    for match in EXPLICIT_PATH.finditer(text or ''):
        rel = _safe_rel(match.group(1))
        if rel and rel not in paths:
            paths.append(rel)
        if len(paths) >= 12:
            break
    return paths


def _list_sources(root):
    tracked = _git(root, 'ls-files', '-z')
    if tracked is None:
        return [], 'UNKNOWN'
    paths = []
    for rel in tracked.split('\0'):
        clean = _safe_rel(rel, allow_generated=True)
        if clean and Path(clean).suffix.casefold() in SOURCE_SUFFIXES | REFERENCE_SUFFIXES:
            paths.append(clean)
    return sorted(set(paths)), 'OK'


def _candidate_score(rel, tokens, explicit, dirty_paths, task_references=()):
    low = rel.casefold()
    score = 0
    if rel in explicit:
        score += 100
    if rel in task_references:
        score += 250
    if rel in dirty_paths:
        score += 55
    stem = Path(low).stem
    parts = set(re.findall(r'[a-zа-яё0-9_]+', low))
    score += 5 * len(parts.intersection(tokens))
    score += 2 * sum(1 for token in tokens if len(token) >= 5 and token in stem)
    if any(mark in low for mark in TEST_MARKERS):
        score -= 5
    return score


def _file_hashes(root, paths):
    rows = []
    hashed_bytes = 0
    for rel in paths:
        path = root / rel
        if not path.is_file():
            rows.append({'path': rel, 'working_hash': None, 'size_bytes': None,
                         'exists': False, 'hash_status': 'MISSING'})
            continue
        value, size, status = _bounded_file_hash(path, max(0, MAX_PACKET_HASH_BYTES - hashed_bytes))
        hashed_bytes += size or 0
        rows.append({'path': rel, 'working_hash': value, 'size_bytes': size,
                     'exists': True, 'hash_status': status})
    return rows


def _fragment(root, rel, tokens, snapshot_head, reason, max_bytes=MAX_FRAGMENT_BYTES):
    path = root / rel
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_FILE or b'\x00' in raw:
            return None
        text = raw.decode('utf-8')
    except (OSError, UnicodeError):
        return None
    lines = text.splitlines()
    if not lines:
        return None
    scored = []
    for number, line in enumerate(lines, 1):
        lower = line.casefold()
        overlap = sum(1 for token in tokens if token in lower)
        if overlap:
            scored.append((overlap, number))
    if scored:
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        center = scored[0][1]
    else:
        center = 1
    start = end = center
    current_bytes = len(lines[center - 1].encode('utf-8')) + len(str(center)) + 2
    if current_bytes > max_bytes:
        return None
    while True:
        options = []
        if start > 1:
            options.append(start - 1)
        if end < len(lines):
            options.append(end + 1)
        if not options:
            break
        options.sort(key=lambda number: (abs(number - center), number))
        added = False
        for number in options:
            encoded_length = len(lines[number - 1].encode('utf-8')) + len(str(number)) + 2
            next_bytes = current_bytes + encoded_length + 1
            if next_bytes <= max_bytes and number in (start - 1, end + 1):
                if number == start - 1:
                    start = number
                else:
                    end = number
                current_bytes = next_bytes
                added = True
                break
        if not added:
            break
    # Never split source characters or claim that an excerpt is a full-file view.
    excerpt = '\n'.join('%d: %s' % (n, lines[n - 1]) for n in range(start, end + 1))
    overlay_hash = _digest(raw)
    committed_hash = None
    version = 'WORKTREE_OVERLAY'
    try:
        committed = subprocess.check_output(['git', 'show', 'HEAD:' + rel], cwd=str(root),
                                            stderr=subprocess.DEVNULL, timeout=5)
        committed_hash = _digest(committed)
        if committed_hash == overlay_hash:
            version = 'HEAD'
    except (OSError, subprocess.SubprocessError):
        pass
    symbol = None
    for number in range(start, end + 1):
        line = lines[number - 1].strip()
        if re.match(r'(async\s+)?(def|class|function)\s+', line):
            symbol = line[:220]
            break
    return {'path': rel, 'symbol_or_anchor': symbol, 'line_start': start,
            'line_end': end, 'selection_reason': reason,
            'excerpt_bytes': len(excerpt.encode('utf-8')),
            'content_hash': overlay_hash, 'committed_hash': committed_hash,
            'source_version': version, 'snapshot_commit': snapshot_head,
            'excerpt': excerpt}


def _read_memory_records(root, name, max_bytes=2 * 1024 * 1024, max_rows=4000):
    path = root / '.ai-dev' / 'memory' / name
    if not path.is_file():
        return [], 'MISSING'
    try:
        size = path.stat().st_size
        if size > max_bytes:
            return [], 'TOO_LARGE'
        rows = []
        for line in path.read_text(encoding='utf-8').splitlines()[-max_rows:]:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
        return rows, 'OK'
    except (OSError, UnicodeError, ValueError):
        return [], 'INVALID'


def _event_projection(events, decisions):
    """Deduplicate exact events; collapse milestone admin records only with explicit supersession evidence."""
    unique = {}
    visible = []
    conflicts = []
    for row in events:
        ident = row.get('event_id')
        if not ident:
            continue
        canonical = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        previous = unique.get(ident)
        if previous is None:
            unique[ident] = (canonical, row)
            visible.append(row)
        elif previous[0] != canonical:
            conflicts.append({'event_id': ident, 'kind': 'DUPLICATE_ID_DIFFERENT_CONTENT'})
            # Conflicting records stay in the generated view for inspection; neither wins silently.
            visible.append(row)
    rows = visible
    # Follow only explicit decision supersedes links. If absent or ambiguous, preserve all records.
    milestone_decisions = [d for d in decisions if str(d.get('stage_id') or '').casefold() in
                           ('milestone-5', 'milestone_5', 'm5')]
    superseded_commits = set()
    decision_by_id = {str(d.get('decision_id')): d for d in milestone_decisions if d.get('decision_id')}
    for decision in milestone_decisions:
        supersedes = decision.get('supersedes')
        links = supersedes if isinstance(supersedes, list) else ([supersedes] if supersedes else [])
        for item in links:
            item = str(item).removeprefix('git:')
            prior = decision_by_id.get(item)
            # A linked older decision gives explicit commit-level supersession evidence.
            if prior and prior.get('commit'):
                superseded_commits.add(str(prior['commit']).removeprefix('git:'))
            elif item:
                superseded_commits.add(item)
    m5 = [r for r in rows if str(r.get('stage_id') or '').casefold() in ('milestone-5', 'milestone_5', 'm5')]
    collapsed, rolled_up = [], []
    if m5 and superseded_commits:
        for row in rows:
            commit = str(row.get('commit') or '').removeprefix('git:')
            if row in m5 and commit and commit in superseded_commits:
                rolled_up.append(row.get('event_id'))
            else:
                collapsed.append(row)
    else:
        collapsed = rows
    collapsed.sort(key=lambda row: (str(row.get('timestamp') or ''), str(row.get('event_id') or '')))
    return collapsed, {'raw_unique_count': len(rows), 'visible_count': len(collapsed),
                       'rolled_up_event_ids': sorted(item for item in rolled_up if item),
                       'conflicts': conflicts}


def _match_records(rows, task_id, scope_paths, tokens, kind):
    selected = []
    for row in rows:
        if task_id and row.get('task_id') == task_id:
            selected.append(row)
            continue
        fields = ['summary', 'decision', 'rationale', 'error_signature', 'error_class', 'stage_id']
        values = [row.get(field) for field in fields]
        values += row.get('affected_paths') or []
        values += row.get('evidence_refs') or []
        values += row.get('artifact_refs') or []
        normalized = ' '.join(value for value in values if isinstance(value, str)).casefold()
        exact_path = any(path.casefold() in normalized for path in scope_paths)
        overlap = sum(1 for token in tokens if token in normalized)
        if exact_path or overlap >= (1 if kind == 'events' else 2):
            selected.append(row)
    selected.sort(key=lambda row: (str(row.get('timestamp') or ''),
                                   str(row.get('decision_id') or row.get('failure_id') or row.get('event_id') or '')))
    return selected[-12:]


def _measure(packet):
    return _json_bytes(packet)


def build_packet(root, task_id=None, goal=None, budget=DEFAULT_BUDGET, max_files=8,
                 extra_requests=None, initial_evidence_seeds=None):
    """Build offline packet. It never runs tests, model turns, or task commands."""
    started = time.monotonic()
    root = Path(root).resolve()
    if type(budget) is not int or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise ValueError('budget must be between %d and %d bytes' % (MIN_BUDGET, MAX_BUDGET))
    if type(max_files) is not int or not 1 <= max_files <= 16:
        raise ValueError('max_files must be between 1 and 16')
    if task_id is not None and (not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id)):
        raise ValueError('task_id must be a safe 1–81 character identifier.')
    state, state_path = _load_task(root, task_id)
    seed_rows = validate_initial_evidence_seeds(initial_evidence_seeds if initial_evidence_seeds is not None
                                                else (state or {}).get('initial_evidence_seeds', []))
    task_id = str(state.get('id')) if state and state.get('id') else task_id
    goal = _clean(goal or (state or {}).get('prompt'), 6000)
    if not goal:
        raise ValueError('Provide --goal or a known task with a saved prompt.')
    head = _git(root, 'rev-parse', 'HEAD')
    branch = _git(root, 'branch', '--show-current')
    dirty = _dirty_snapshot(root)
    explicit, acceptance, stages = _plan_scope(state or {})
    requests = _validate_evidence_requests(extra_requests or [])
    requested_paths = [row['query'] for row in requests if row['kind'] == 'path']
    requested_directories = set()
    for rel in requested_paths:
        try:
            directory = (root / rel).resolve(strict=True)
            directory.relative_to(root)
            if directory.is_dir():
                requested_directories.add(rel.rstrip('/'))
        except (OSError, RuntimeError, ValueError):
            pass
    # A path request is an exact lookup, not a fuzzy lexical hint. Otherwise a
    # missing requested path can accidentally surface an unrelated file sharing
    # one directory token and falsely count as new evidence.
    task_reference_paths = _explicit_task_paths(goal)
    tokens = _tokens(goal + ' ' + ' '.join(explicit) + ' ' +
                     ' '.join(task_reference_paths) + ' ' +
                     ' '.join(stages) + ' ' + ' '.join(row['query'] for row in requests
                     if row['kind'] != 'path'))
    all_paths, source_status = _list_sources(root)
    seeded_fragments, seed_status = [], []
    for seed in seed_rows:
        rel = seed['query']
        status = {'path': rel, 'priority': seed['priority'], 'reason': seed['reason'],
                  'status': 'MISSING', 'fragment': None}
        candidate = root / rel
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if rel in all_paths and resolved.is_file():
                fragment = _fragment(root, rel, _tokens(goal), head, 'INITIAL_EVIDENCE_SEED',
                                     max_bytes=MAX_SEEDED_FRAGMENT_BYTES)
                if fragment:
                    status.update(status='SATISFIED', fragment=fragment)
                    seeded_fragments.append(fragment)
                else:
                    status['status'] = 'UNREADABLE'
            else:
                status['status'] = 'MISSING'
        except (OSError, RuntimeError, ValueError):
            status['status'] = 'MISSING'
        seed_status.append(status)
    missing_required = [row for row in seed_status if row['priority'] == 'required' and row['status'] != 'SATISFIED']
    if missing_required:
        raise EvidenceSeedError('PREPLANNER_EVIDENCE_SEED_MISSING', {
            'reason': 'required initial evidence seed could not be read as an exact tracked source path',
            'required_seed_count': sum(row['priority'] == 'required' for row in seed_rows),
            'unsatisfied_seeds': [{'path': row['path'], 'status': row['status']} for row in missing_required]})
    discovered_test_paths = [rel for rel in all_paths
        if (root / rel).is_file() and any(marker in Path(rel).name.casefold() or marker in rel.casefold()
               for marker in TEST_MARKERS)]
    dirty_paths = {item['path'] for item in dirty['paths']}
    source_paths = all_paths
    task_reference_set = set(task_reference_paths).intersection(source_paths)
    considered_bytes = 0
    candidates = []
    excluded_generated, excluded_platform, excluded_ignore = [], [], []
    platform_text = ' '.join([goal] + stages + acceptance)
    platform_scope = _platform_scope(platform_text, sys_platform())
    # Only task/plan declarations can override source/platform exclusions;
    # planner-issued lookups cannot reactivate a deferred platform.
    explicit_set = set(explicit)
    discovered_requests = []
    ignored_paths = _ignored_paths(root, source_paths)
    for rel in source_paths:
        if _generated_path([part.casefold() for part in Path(rel).parts]) and rel not in explicit_set:
            excluded_generated.append(rel)
            continue
        if rel in ignored_paths and rel not in explicit_set:
            excluded_ignore.append(rel)
            continue
        if ('windows' in platform_scope['deferred_platforms'] and
                _windows_specific_path(rel) and not _shared_contract_path(rel) and rel not in explicit_set):
            excluded_platform.append(rel)
            continue
        if rel in requested_paths or any(rel.startswith(directory + '/')
                                          for directory in requested_directories):
            discovered_requests.append(rel)
        score = _candidate_score(rel, tokens, explicit, dirty_paths, task_reference_set)
        if rel in discovered_requests:
            score += 125
        # For paths outside explicit/changed scope use only a bounded prefix for lexical relevance.
        if score < 1:
            continue
        try:
            size = (root / rel).stat().st_size
        except OSError:
            size = 0
        considered_bytes += min(size, MAX_SOURCE_FILE)
        candidates.append((score, rel))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    selected_code, selected_tests, selected_references = [], [], []
    for score, rel in candidates:
        is_test = any(marker in Path(rel).name.casefold() or marker in rel.casefold()
                      for marker in TEST_MARKERS)
        is_reference = Path(rel).suffix.casefold() in REFERENCE_SUFFIXES and not is_test
        target = selected_tests if is_test else (selected_references if is_reference else selected_code)
        if len(target) >= (3 if is_reference else max_files):
            continue
        reason = ('PLANNER_REQUEST' if rel in discovered_requests else
                  'EXPLICIT_TASK_REFERENCE' if rel in task_reference_set else
                  'PLAN_SCOPE' if rel in explicit else
                  'WORKTREE_CHANGE' if rel in dirty_paths else 'PATH_TOKEN_MATCH')
        fragment = _fragment(root, rel, tokens, head, reason)
        if fragment:
            target.append((score, fragment))
    selected_code.sort(key=lambda item: (-item[0], item[1]['path']))
    selected_tests.sort(key=lambda item: (-item[0], item[1]['path']))
    selected_references.sort(key=lambda item: (-item[0], item[1]['path']))
    fragments = [item[1] for item in selected_code[:max_files]]
    test_fragments = [item[1] for item in selected_tests[:max_files]]
    reference_fragments = [item[1] for item in selected_references[:3]]
    # Include actual task-plan files in the scope even if no source excerpt could be read.
    scoped_paths = list(dict.fromkeys(explicit + discovered_requests +
        [item['path'] for item in seeded_fragments + fragments + test_fragments + reference_fragments]))[:64]
    file_hashes = _file_hashes(root, scoped_paths)
    decisions, decision_status = _read_memory_records(root, 'decisions.jsonl')
    failures, failure_status = _read_memory_records(root, 'failures.jsonl')
    events, event_status = _read_memory_records(root, 'events.jsonl')
    visible_events, rollup = _event_projection(events, decisions)
    selected_decisions = _match_records(decisions, task_id, scoped_paths, tokens, 'decisions')
    selected_failures = _match_records(failures, task_id, scoped_paths, tokens, 'failures')
    selected_events = _match_records(visible_events, task_id, scoped_paths, tokens, 'events')
    memory_hashes = {name: _file_fingerprint(root / '.ai-dev' / 'memory' / name)
                     for name in ('decisions.jsonl', 'failures.jsonl', 'events.jsonl')}
    facts, fact_conflicts = [], []
    # Facts are only directly observed metadata; no generated hypotheses are treated as facts.
    if head:
        facts.append({'key': 'git.head', 'value': head, 'source_refs': ['git:HEAD'],
                      'confidence': 'observed'})
    if task_id:
        facts.append({'key': 'task.status', 'value': (state or {}).get('status', 'UNKNOWN'),
                      'source_refs': [state_path] if state_path else [], 'confidence': 'observed'})
    assumptions = []
    unknowns = []
    if decision_status != 'OK':
        unknowns.append('Project Memory decisions unavailable: ' + decision_status)
    if failure_status != 'OK':
        unknowns.append('Project Memory failures unavailable: ' + failure_status)
    if event_status != 'OK':
        unknowns.append('Project Memory events unavailable: ' + event_status)
    if not acceptance:
        unknowns.append('No acceptance criteria found in the selected saved plan stage.')
    if discovered_test_paths and not test_fragments:
        unknowns.append('Test files exist, but no relevant test excerpt was selected; no tests were run during packet build.')
    elif not discovered_test_paths:
        unknowns.append('No tracked test source exists; no tests were run during packet build.')
    if source_status != 'OK' or head is None:
        unknowns.append('Git/source discovery is incomplete.')
    if not dirty.get('hash_complete'):
        unknowns.append('Dirty working-tree fingerprint exceeded its safe hashing bound.')
    if any(row.get('hash_status') == 'SIZE_LIMIT' for row in file_hashes):
        unknowns.append('One or more scoped files exceeded the safe hashing bound.')
    if any(row.get('hash_status') == 'UNREADABLE' for row in file_hashes):
        unknowns.append('One or more scoped files could not be hashed.')
    platform_constraints = [{'platform': platform_scope['active_platform'],
        'evidence': 'builder host runtime', 'status': 'observed'}]
    for platform in ('macos', 'windows'):
        if platform != platform_scope['active_platform']:
            status = 'DEFERRED' if platform in platform_scope['deferred_platforms'] else 'UNKNOWN_UNVERIFIED'
            platform_constraints.append({'platform': platform, 'evidence': None, 'status': status})
    plan_contract = {'objective': _clean((state or {}).get('plan', {}).get('objective'), 1200),
                     'stage_titles': stages, 'acceptance': acceptance,
                     'worker_kind': (state or {}).get('plan', {}).get('worker_kind')}
    task = {'task_id': task_id or 'UNSPECIFIED', 'goal': goal,
              'task_status': (state or {}).get('status', 'NOT_IN_SAVED_STATE'),
            'blocker': _clean((state or {}).get('reason') or (state or {}).get('last_failure'), 500) or None,
            'state_ref': state_path, 'stage_id': ((state or {}).get('plan', {}).get('steps') or [{}])[min(
                max(0, (state or {}).get('checkpoint_index', 0)),
                max(0, len((state or {}).get('plan', {}).get('steps') or [{}]) - 1))].get('id')
                if isinstance((state or {}).get('plan', {}).get('steps'), list) and (state or {}).get('plan', {}).get('steps') else None,
            'plan_contract': plan_contract}
    verification = (state or {}).get('verification')
    if isinstance(verification, dict):
        task['latest_verification'] = {'ok': verification.get('ok'),
            'checks': [{'name': _clean(item.get('name'), 100),
                        'status': _clean(item.get('status'), 40)}
                       for item in (verification.get('checks') or [])[:12] if isinstance(item, dict)]}
    attempts_total = (state or {}).get('attempts')
    attempt_rows = (state or {}).get('attempts_detail')
    if not isinstance(attempt_rows, list):
        attempt_rows = (state or {}).get('planning_attempts')
    if not isinstance(attempt_rows, list):
        attempt_rows = []
    decisions_by_attempt = {}
    for decision in (state or {}).get('decisions') or []:
        if isinstance(decision, dict):
            decisions_by_attempt[(decision.get('attempt'), decision.get('checkpoint'))] = decision
            decisions_by_attempt[(decision.get('attempt'), None)] = decision
    compact_attempts = []
    for item in attempt_rows[-8:]:
        if not isinstance(item, dict):
            continue
        route = (decisions_by_attempt.get((item.get('attempt'), item.get('checkpoint'))) or
                 decisions_by_attempt.get((item.get('attempt'), None)))
        merged = dict(route or {})
        merged.update(item)
        if isinstance(item.get('codex'), dict):
            merged['codex'] = item['codex']
        compact_attempts.append(_compact_attempt(merged))
    task['prior_attempts'] = compact_attempts
    task['prior_attempt_usage'] = _aggregate_attempts(compact_attempts)
    task['attempts_total'] = attempts_total if type(attempts_total) is int else len(attempt_rows)
    relevant_dirty = [item for item in dirty['paths'] if item['path'] in scoped_paths]
    remaining_dirty = [item for item in dirty['paths'] if item['path'] not in scoped_paths]
    unrelated_dirty_sample = [{'path': item['path'], 'index_status': item['index_status']}
                              for item in remaining_dirty[:4]]
    snapshot = {'head': head or 'UNKNOWN', 'branch': branch or 'UNKNOWN',
                'working_tree': dirty['status'], 'dirty_path_count': len(dirty['paths']),
                'dirty_paths': relevant_dirty + unrelated_dirty_sample,
                'dirty_paths_omitted': len(remaining_dirty) - len(unrelated_dirty_sample),
                'dirty_hash': dirty['hash'], 'dirty_hash_complete': dirty['hash_complete']}
    packet = {'schema_version': SCHEMA_VERSION, 'task': task, 'snapshot': snapshot,
              'scope': {'files': scoped_paths, 'seeded_fragments': seeded_fragments,
                        'initial_evidence_seeds': [{key: value for key, value in row.items() if key != 'fragment'}
                                                   for row in seed_status],
                        'source_fragments': fragments,
                        'test_fragments': test_fragments,
                        'reference_fragments': reference_fragments,
                        'test_discovery': 'DISCOVERED_NOT_RUN' if discovered_test_paths else 'NONE_FOUND',
                        'test_paths_discovered': discovered_test_paths[:32]},
              'git_evidence': {'head': head, 'branch': branch,
                               'recent_commits': _recent_commits(root, scoped_paths)},
              'decisions': [_compact_memory(row, 'decision') for row in selected_decisions],
              'failures': [_compact_memory(row, 'failure') for row in selected_failures],
              'events': [_compact_memory(row, 'event') for row in selected_events],
              'event_projection': rollup,
              'platform_constraints': platform_constraints,
              'platform_scope': platform_scope,
              'known_facts': facts, 'assumptions': assumptions,
              'unknowns': sorted(set(unknowns)), 'conflicts': fact_conflicts + rollup['conflicts'],
              'completeness': {'status': 'INCOMPLETE', 'checks': {}},
              'references': {'task_state': state_path, 'memory': {
                  'decisions': '.ai-dev/memory/decisions.jsonl',
                  'failures': '.ai-dev/memory/failures.jsonl',
                  'events': '.ai-dev/memory/events.jsonl'}},
              'budget': {'target_bytes': budget, 'hard_ceiling_bytes': MAX_BUDGET,
                         'final_bytes': 0, 'estimated_tokens': 0,
                         'items_considered': len(candidates), 'files_considered': len(source_paths),
                         'source_bytes_considered': considered_bytes,
                         'files_selected': 0, 'items_selected': 0, 'items_dropped': 0,
                         'unknown_count': len(unknowns), 'conflict_count': len(fact_conflicts) + len(rollup['conflicts']),
                         'build_time_ms': 0, 'model_turns_used_to_build': 0,
                         'excluded_generated_count': len(excluded_generated),
                         'excluded_platform_count': len(excluded_platform),
                         'excluded_ignore_count': len(excluded_ignore)},
              'selection_hygiene': {'generated_excluded': excluded_generated[:24],
                                    'platform_excluded': excluded_platform[:24],
                                    'ignore_excluded': excluded_ignore[:24],
                                    'evidence_requests': [{'kind': row['kind'], 'query': row['query'],
                                        'found': ((row['query'] in source_paths or
                                            any(rel.startswith(row['query'].rstrip('/') + '/')
                                                for rel in source_paths)) if row['kind'] == 'path' else
                                            any(token in rel.casefold() for rel in source_paths
                                                for token in _tokens(row['query'])))}
                                        for row in requests],
                                    'explicit_overrides': sorted(path for path in explicit_set
                                        if path in source_paths and (_generated_path(
                                            [part.casefold() for part in Path(path).parts]) or
                                        _windows_specific_path(path) or path in ignored_paths))}}
    checks = {'goal_present': bool(goal), 'snapshot_known': head is not None,
              'scope_found': bool(scoped_paths), 'acceptance_found': bool(acceptance),
              'tests_discovered': bool(test_fragments), 'memory_readable': decision_status == failure_status == event_status == 'OK',
              'conflicts_absent': not packet['conflicts'],
              'platform_scope_explicit': platform_scope['explicit']}
    packet['completeness']['checks'] = checks
    packet['completeness']['status'] = 'READY' if all(checks.values()) else ('DEGRADED' if head and scoped_paths else 'INCOMPLETE')
    _fit_budget(packet, budget)
    packet['budget']['build_time_ms'] = int((time.monotonic() - started) * 1000)
    packet['budget']['files_selected'] = len(scoped_paths)
    packet['budget']['items_selected'] = (len(fragments) + len(test_fragments) +
        len(reference_fragments) + len(selected_decisions) + len(selected_failures) +
        len(selected_events) + len(facts))
    # Runtime timing changes size only by digits; recompute and remove no content for a timing delta.
    packet['budget']['final_bytes'] = _measure(packet)
    packet['budget']['estimated_tokens'] = (packet['budget']['final_bytes'] + 3) // 4
    packet['packet_id'] = _digest(json.dumps({'task': task, 'snapshot': snapshot,
        'scope': scoped_paths, 'hashes': file_hashes, 'memory_hashes': memory_hashes,
        'decisions': selected_decisions, 'failures': selected_failures,
        'events': selected_events}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode())[:24]
    packet['staleness'] = {'status': 'CHECKABLE', 'file_hashes': file_hashes,
                           'task_state_hash': _file_fingerprint(root / state_path) if state_path else None,
                           'task_contract_hash': _task_contract_fingerprint(state),
                           'memory_hashes': memory_hashes}
    # Final fields are included in exact size accounting. If necessary only whole low-value items drop.
    _fit_budget(packet, budget)
    packet['budget']['final_bytes'] = _measure(packet)
    packet['budget']['estimated_tokens'] = (packet['budget']['final_bytes'] + 3) // 4
    if packet['budget']['final_bytes'] > min(budget, MAX_BUDGET):
        raise ValueError('Required packet fields exceed the configured byte budget; no partial JSON emitted.')
    return packet


def sys_platform():
    import sys
    return 'macos' if sys.platform == 'darwin' else ('windows' if sys.platform == 'win32' else sys.platform)


def _recent_commits(root, paths):
    if not paths:
        return []
    args = ['git', 'log', '-5', '--format=%H%x09%cs%x09%s', '--'] + paths[:24]
    try:
        text = subprocess.check_output(args, cwd=str(root), text=True, stderr=subprocess.DEVNULL,
                                       timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for line in text.splitlines()[:5]:
        fields = line.split('\t', 2)
        if len(fields) == 3:
            result.append({'commit': fields[0], 'date': fields[1], 'subject': fields[2][:240]})
    return result


def _compact_memory(row, kind):
    if kind == 'decision':
        return {'id': row.get('decision_id'), 'task_id': row.get('task_id'),
                'stage_id': row.get('stage_id'), 'commit': row.get('commit'),
                'decision': _clean(row.get('decision'), 600), 'rationale': _clean(row.get('rationale'), 400),
                'status': row.get('status'), 'evidence_refs': (row.get('evidence_refs') or [])[:8],
                'supersedes': row.get('supersedes')}
    if kind == 'failure':
        return {'id': row.get('failure_id'), 'task_id': row.get('task_id'),
                'stage_id': row.get('stage_id'), 'error_class': row.get('error_class'),
                'error_signature': _clean(row.get('error_signature'), 300),
                'resolution_status': row.get('resolution_status'),
                'affected_paths': (row.get('affected_paths') or [])[:8],
                'artifact_refs': (row.get('artifact_refs') or [])[:8]}
    return {'id': row.get('event_id'), 'task_id': row.get('task_id'),
            'stage_id': row.get('stage_id'), 'type': row.get('type'),
            'summary': _clean(row.get('summary'), 300), 'commit': row.get('commit'),
            'artifact_refs': (row.get('artifact_refs') or [])[:8]}


def _compact_attempt(row):
    verification = row.get('verification') if isinstance(row.get('verification'), dict) else {}
    codex = row.get('codex') if isinstance(row.get('codex'), dict) else {}
    usage = codex.get('usage') if isinstance(codex.get('usage'), dict) else {}
    telemetry = row.get('telemetry') if isinstance(row.get('telemetry'), dict) else {}
    if isinstance(telemetry.get('usage'), dict):
        usage = telemetry['usage']
    verification_result = ('PASS' if verification.get('ok') is True else
                           ('FAIL' if verification.get('ok') is False else 'UNKNOWN'))
    turn_result = ('COMPLETED' if codex.get('exit_code') == 0 else
                   ('FAILED' if isinstance(codex.get('exit_code'), int) else 'UNKNOWN'))
    return {'attempt': row.get('attempt'), 'stage_id': row.get('checkpoint') or row.get('stage_id'),
            'role': row.get('role'), 'configured_model': row.get('model'),
            'configured_reasoning': row.get('reasoning'),
            'observed_model': telemetry.get('observed_model', 'UNKNOWN'),
            'observed_reasoning': telemetry.get('observed_reasoning', 'UNKNOWN'),
            'session_id': telemetry.get('session_id', row.get('session_id')),
            'duration_seconds': telemetry.get('duration_seconds'),
            'retry_number': telemetry.get('retry_number'),
            'escalation_reason': _clean(telemetry.get('escalation_reason'), 180) or None,
            'runtime_verified': row.get('runtime_verified'),
            'turn_result': telemetry.get('result') or turn_result,
            'verification_result': verification_result,
            'exit_code': codex.get('exit_code'), 'input_tokens': usage.get('input_tokens'),
            'cached_input_tokens': usage.get('cached_input_tokens'),
            'cache_write_input_tokens': usage.get('cache_write_input_tokens'),
            'output_tokens': usage.get('output_tokens'),
            'reasoning_output_tokens': usage.get('reasoning_output_tokens'),
            'reason': _clean(row.get('reason') or row.get('failure'), 240) or None}


def _aggregate_attempts(attempts):
    groups = {}
    for item in attempts:
        key = (item.get('role') or 'UNKNOWN', item.get('configured_model') or 'UNKNOWN',
               item.get('configured_reasoning') or 'UNKNOWN', item.get('observed_model') or 'UNKNOWN')
        group = groups.setdefault(key, {'role': key[0], 'configured_model': key[1],
            'configured_reasoning': key[2], 'observed_model': key[3], 'turns': 0,
            'input_tokens': 0, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
            'output_tokens': 0, 'reasoning_output_tokens': 0, 'known_usage_turns': 0})
        group['turns'] += 1
        for field in ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                      'output_tokens', 'reasoning_output_tokens'):
            value = item.get(field)
            if type(value) is int and value >= 0:
                group[field] += value
        if any(type(item.get(field)) is int and item[field] >= 0
               for field in ('input_tokens', 'cached_input_tokens', 'output_tokens')):
            group['known_usage_turns'] += 1
    return [groups[key] for key in sorted(groups)]


def _fit_budget(packet, budget):
    """Drop whole evidence records in low-priority order, never cut text arbitrarily."""
    packet['budget']['final_bytes'] = _measure(packet)
    if packet['budget']['final_bytes'] <= budget:
        return
    seed_rows = packet.get('scope', {}).get('initial_evidence_seeds', [])
    optional_paths = {row.get('path') for row in seed_rows if row.get('priority') == 'optional'}
    optional_fragments = packet['scope'].get('seeded_fragments', [])
    while packet['budget']['final_bytes'] > budget and optional_paths:
        removed = next((item for item in reversed(optional_fragments)
                        if item.get('path') in optional_paths), None)
        if removed is None:
            break
        optional_fragments.remove(removed)
        optional_paths.remove(removed.get('path'))
        for row in seed_rows:
            if row.get('path') == removed.get('path'):
                row['status'] = 'OMITTED_BUDGET'
                break
        packet['budget']['items_dropped'] += 1
        packet['budget']['final_bytes'] = _measure(packet)
    if packet['budget']['final_bytes'] <= budget:
        return
    required = [row for row in packet.get('scope', {}).get('initial_evidence_seeds', [])
                if row.get('priority') == 'required']
    required_paths = {row.get('path') for row in required if row.get('status') == 'SATISFIED'}
    required_fragments = [row for row in packet['scope'].get('seeded_fragments', [])
                          if row.get('path') in required_paths]
    required_packet = {'scope': {'seeded_fragments': required_fragments,
                                 'initial_evidence_seeds': required}}
    minimum = _measure(required_packet) + 2048
    if required and minimum > budget:
        raise EvidenceSeedError('PREPLANNER_EVIDENCE_BUDGET_EXCEEDED', {
            'reason': 'required evidence seed fragments exceed the configured packet budget',
            'required_seed_count': len(required), 'attempted_bytes': _measure(required_packet),
            'hard_ceiling_bytes': budget,
            'unsatisfied_seeds': [{'path': row.get('path'), 'status': 'BUDGET_EXCEEDED'}
                                  for row in required]})
    attempts = packet.get('task', {}).get('prior_attempts', [])
    while packet['budget']['final_bytes'] > budget and attempts:
        attempts.pop(0)
        packet['task']['prior_attempts_omitted'] = packet['task'].get('prior_attempts_omitted', 0) + 1
        packet['budget']['items_dropped'] += 1
        packet['budget']['final_bytes'] = _measure(packet)
    if 'prior_attempts_omitted' not in packet['task']:
        packet['task']['prior_attempts_omitted'] = 0
    drops = [('events', 0), ('failures', 0), ('decisions', 0), ('known_facts', 0),
             ('reference_fragments', 0), ('test_fragments', 0), ('source_fragments', 0)]
    for name, _ in drops:
        key = name
        if name in ('source_fragments', 'test_fragments', 'reference_fragments'):
            key = 'scope'
            sub = name
        while packet['budget']['final_bytes'] > budget:
            values = packet[key][sub] if key == 'scope' else packet[key]
            if not values:
                break
            values.pop()
            packet['budget']['items_dropped'] += 1
            packet['budget']['final_bytes'] = _measure(packet)
    packet['scope']['files'] = list(dict.fromkeys(
        [item['path'] for item in packet['scope'].get('seeded_fragments', []) + packet['scope']['source_fragments'] +
         packet['scope']['test_fragments'] + packet['scope']['reference_fragments']] +
        packet['scope']['files'][:4]))
    packet['budget']['final_bytes'] = _measure(packet)
    if packet['budget']['final_bytes'] > budget and required:
        raise EvidenceSeedError('PREPLANNER_EVIDENCE_BUDGET_EXCEEDED', {
            'reason': 'required initial evidence seeds and packet contract exceed the configured packet budget',
            'required_seed_count': len(required), 'attempted_bytes': packet['budget']['final_bytes'],
            'hard_ceiling_bytes': budget,
            'unsatisfied_seeds': [{'path': row.get('path'), 'status': 'BUDGET_EXCEEDED'}
                                  for row in required]})
    packet['budget']['files_selected'] = len(packet['scope']['files'])
    packet['budget']['items_selected'] = (len(packet['scope']['source_fragments']) +
        len(packet['scope']['test_fragments']) + len(packet['scope']['reference_fragments']) + len(packet['decisions']) +
        len(packet['failures']) + len(packet['events']) + len(packet['known_facts']))


def save_packet(root, packet):
    root = Path(root).resolve()
    path = root / '.ai-dev' / 'evidence' / (packet['packet_id'] + '.json')
    write(path, packet)
    return path


def load_packet(path):
    value = read(Path(path).expanduser())
    if (not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION or
            not isinstance(value.get('packet_id'), str) or not isinstance(value.get('staleness'), dict)):
        raise ValueError('Malformed or unsupported Evidence Packet.')
    return value


def check_packet(root, packet, task_contract_only=False):
    root = Path(root).resolve()
    reasons = []
    current_head = _git(root, 'rev-parse', 'HEAD')
    if not current_head:
        return {'status': 'UNKNOWN', 'reasons': ['GIT_HEAD_UNAVAILABLE']}
    if current_head != packet.get('snapshot', {}).get('head'):
        reasons.append('HEAD_CHANGED')
    state_ref = packet.get('references', {}).get('task_state')
    saved_state_hash = packet.get('staleness', {}).get('task_state_hash')
    expected_contract_hash = packet.get('staleness', {}).get('task_contract_hash')
    if task_contract_only and state_ref and not expected_contract_hash:
        return {'status': 'UNKNOWN', 'reasons': ['TASK_CONTRACT_FINGERPRINT_UNAVAILABLE'],
                'current_head': current_head}
    if state_ref and not task_contract_only and not saved_state_hash:
        return {'status': 'UNKNOWN', 'reasons': ['TASK_STATE_FINGERPRINT_UNAVAILABLE'],
                'current_head': current_head}
    if state_ref and task_contract_only and expected_contract_hash:
        if not (state_ref == '.ai-dev/state.json' or
                re.fullmatch(r'\.ai-dev/runs/[A-Za-z0-9._-]{1,100}/state\.json', state_ref)):
            return {'status': 'UNKNOWN', 'reasons': ['INVALID_TASK_STATE_REFERENCE'],
                    'current_head': current_head}
        try:
            current_state = read(root / state_ref)
        except (OSError, ValueError, TypeError):
            current_state = None
        actual_contract_hash = _task_contract_fingerprint(current_state)
        if actual_contract_hash is None:
            reasons.append('TASK_STATE_UNAVAILABLE')
        elif actual_contract_hash != expected_contract_hash:
            reasons.append('TASK_CONTRACT_CHANGED')
    if state_ref and not task_contract_only and saved_state_hash:
        if not (state_ref == '.ai-dev/state.json' or
                re.fullmatch(r'\.ai-dev/runs/[A-Za-z0-9._-]{1,100}/state\.json', state_ref)):
            return {'status': 'UNKNOWN', 'reasons': ['INVALID_TASK_STATE_REFERENCE'],
                    'current_head': current_head}
        current_state_hash = _file_fingerprint(root / state_ref)
        if current_state_hash is None:
            reasons.append('TASK_STATE_UNAVAILABLE')
        elif current_state_hash != saved_state_hash:
            reasons.append('TASK_STATE_CHANGED')
    memory_hashes = packet.get('staleness', {}).get('memory_hashes')
    if not isinstance(memory_hashes, dict):
        return {'status': 'UNKNOWN', 'reasons': ['MEMORY_FINGERPRINTS_UNAVAILABLE'],
                'current_head': current_head}
    if task_contract_only:
        for name, kind, key, packet_key in (
                ('decisions.jsonl', 'decision', 'decision_id', 'decisions'),
                ('failures.jsonl', 'failure', 'failure_id', 'failures'),
                ('events.jsonl', 'event', 'event_id', 'events')):
            expected_rows = packet.get(packet_key) or []
            if not expected_rows:
                continue
            rows, status = _read_memory_records(root, name)
            if status != 'OK':
                return {'status': 'UNKNOWN', 'reasons': ['SELECTED_MEMORY_UNAVAILABLE:' + name],
                        'current_head': current_head}
            by_id = {row.get(key): row for row in rows}
            for expected in expected_rows:
                current = by_id.get(expected.get('id'))
                if current is None or _compact_memory(current, kind) != expected:
                    reasons.append('SELECTED_MEMORY_CHANGED:%s:%s' % (name, expected.get('id')))
    else:
        for name, expected in memory_hashes.items():
            actual = _file_fingerprint(root / '.ai-dev' / 'memory' / name)
            if actual != expected:
                reasons.append('MEMORY_CHANGED:' + name)
    hashes = packet.get('staleness', {}).get('file_hashes')
    if not isinstance(hashes, list):
        return {'status': 'UNKNOWN', 'reasons': ['FILE_HASHES_UNAVAILABLE'], 'current_head': current_head}
    for item in hashes:
        rel = _safe_rel(item.get('path'), allow_generated=True)
        if not rel or rel != item.get('path'):
            return {'status': 'UNKNOWN', 'reasons': ['INVALID_FILE_REFERENCE'],
                    'current_head': current_head}
        path = root / rel
        if item.get('hash_status') == 'MISSING':
            if path.exists():
                reasons.append('FILE_APPEARED:' + rel)
            continue
        if item.get('hash_status') != 'OK' or not isinstance(item.get('working_hash'), str):
            return {'status': 'UNKNOWN', 'reasons': ['FILE_HASH_UNAVAILABLE:' + rel],
                    'current_head': current_head}
        try:
            if not path.is_file():
                reasons.append('FILE_MISSING:' + item['path'])
            elif _digest(path.read_bytes()) != item.get('working_hash'):
                reasons.append('FILE_CHANGED:' + item['path'])
        except OSError:
            return {'status': 'UNKNOWN', 'reasons': ['FILE_UNREADABLE:' + item['path']],
                    'current_head': current_head}
    current_dirty = _dirty_snapshot(root)
    if current_dirty['status'] == 'UNKNOWN' or not current_dirty.get('hash_complete'):
        return {'status': 'UNKNOWN', 'reasons': ['DIRTY_STATE_UNAVAILABLE'], 'current_head': current_head}
    if (packet.get('snapshot', {}).get('dirty_hash_complete') is not True or
            not packet.get('snapshot', {}).get('dirty_hash')):
        return {'status': 'UNKNOWN', 'reasons': ['PACKET_DIRTY_FINGERPRINT_UNAVAILABLE'],
                'current_head': current_head}
    if current_dirty['hash'] != packet.get('snapshot', {}).get('dirty_hash'):
        reasons.append('WORKTREE_CHANGED')
    status = 'STALE' if reasons else 'VALID'
    return {'status': status, 'reasons': reasons, 'packet_id': packet.get('packet_id'),
            'snapshot_head': packet.get('snapshot', {}).get('head'), 'current_head': current_head}


def render_packet(packet):
    task = packet['task']
    budget = packet['budget']
    lines = ['Evidence Packet %s | %s' % (packet['packet_id'], packet['completeness']['status']),
             'Task: %s | %s' % (task['task_id'], task['task_status']),
             'Goal: ' + task['goal'],
             'Snapshot: %s | %s | worktree %s' % (packet['snapshot']['head'][:12],
                 packet['snapshot']['branch'], packet['snapshot']['working_tree']),
             'Initial seeds: %d satisfied (%d required) | automatic evidence: %d' % (
                 sum(row.get('status') == 'SATISFIED' for row in packet['scope'].get('initial_evidence_seeds', [])),
                 sum(row.get('priority') == 'required' for row in packet['scope'].get('initial_evidence_seeds', [])),
                 len(packet['scope']['source_fragments']) + len(packet['scope']['test_fragments']) +
                 len(packet['scope']['reference_fragments'])),
             'Seed errors: %s' % (packet.get('scope', {}).get('seed_failure_code') or 'none'),
             'Scope: %d files | code %d | tests %d (discovered, not run)' % (
                 len(packet['scope']['files']), len(packet['scope']['source_fragments']),
                 len(packet['scope']['test_fragments'])),
             'Prior decisions: %d | failures: %d | events: %d' % (
                 len(packet['decisions']), len(packet['failures']), len(packet['events'])),
             'Unknowns: %d | conflicts: %d' % (len(packet['unknowns']), len(packet['conflicts'])),
             'Budget: %d/%d bytes (~%d tokens), model turns: %d' % (budget['final_bytes'],
                 budget['target_bytes'], budget['estimated_tokens'], budget['model_turns_used_to_build'])]
    for fragment in packet['scope']['source_fragments'][:4]:
        lines.append('  %s:%s-%s %s' % (fragment['path'], fragment['line_start'],
                    fragment['line_end'], fragment.get('symbol_or_anchor') or 'excerpt'))
    for fragment in packet['scope'].get('seeded_fragments', []):
        lines.append('  INITIAL_EVIDENCE_SEED %s:%s-%s' % (fragment['path'], fragment['line_start'], fragment['line_end']))
    for unknown in packet['unknowns'][:8]:
        lines.append('  UNKNOWN: ' + unknown)
    return '\n'.join(lines)
