"""Deterministic suggestions for likely tests based on changed paths."""

from pathlib import Path


def recommend(root, changed_files, explicit_checks=None, limit=20):
    root = Path(root)
    tests = root / 'tests'
    found = []
    reasons = {}
    mapped = set()
    if not isinstance(changed_files, list):
        changed_files = []
    for raw in changed_files[:256]:
        if not isinstance(raw, str):
            continue
        path = raw.replace('\\', '/').lstrip('./')
        basename = Path(path).name
        candidates = []
        if path.startswith('tests/') and basename.startswith('test_') and basename.endswith('.py'):
            candidates = [path]
        elif basename.endswith('.py'):
            stem = basename[:-3]
            candidates = ['tests/test_%s.py' % stem]
            if path.startswith('ai_dev/'):
                candidates.append('tests/test_%s.py' % stem)
        for candidate in candidates:
            if (root / candidate).is_file():
                mapped.add(path)
                if candidate not in found:
                    found.append(candidate)
                reasons[candidate] = 'matching module/test naming convention'
    checks = []
    for item in explicit_checks or []:
        if isinstance(item, list) and item and all(isinstance(arg, str) for arg in item):
            checks.append(item[:32])
    return {'schema_version': 1, 'method': 'explicit_mapping+test_naming',
            'changed_files': changed_files[:128],
            'candidate_tests': found[:limit],
            'reasons': {key: reasons[key] for key in found[:limit]},
            'configured_checks': checks[:20],
            'automatic_execution': False,
            'unmapped_files': [path for path in changed_files[:128]
                               if isinstance(path, str) and path not in mapped][:64]}
