"""Read-only validation of a MegaProg release archive."""
import ast
import hashlib
import json
import stat
from pathlib import Path
import zipfile

from .version import __version__ as CURRENT_VERSION


REQUIRED_FILES = (
    '.gitignore', 'CONTRIBUTING.md', 'LICENSE', 'README.md',
    'README.ru.md', 'README.de.md', 'ДЛЯ_ДРУГОГО_ЧАТА.txt', 'SECURITY.md',
    'SECURITY.ru.md', 'SECURITY.de.md', 'CHANGELOG.md', '.github/workflows/tests.yml',
    'THIRD_PARTY_NOTICES.md', 'pyproject.toml',
    'licenses/CPython-3.12.14-LICENSE.txt',
    'licenses/PyInstaller-6.22.2-COPYING.txt',
    'licenses/Tcl-9.0.4-license.terms', 'licenses/Tk-9.0.4-license.terms',
    'licenses/Qt-qttranslations-6.10.2-BSD-3-Clause.txt',
    'licenses/Qt-qttranslations-6.10.2-GPL-3.0-only.txt',
    'licenses/Qt-qttranslations-6.10.2-LicenseRef-Qt-Commercial.txt',
    'licenses/Qt-qttranslations-6.10.2-Qt-GPL-exception-1.0.txt',
    'licenses/PySide6-6.10.2-GPL-3.0-only.txt',
    'licenses/PySide6-6.10.2-BSD-3-Clause.txt',
    'requirements-build.txt', 'ai-dev', 'ai-dev.cmd',
    'ai_dev/version.py', 'ai_dev/approved_plan.py', 'ai_dev/plan_gui.py',
    'docs/APPROVED_PLAN_MODE.md', 'docs/CONTINUITY.md', 'docs/DECISIONS.md',
    'docs/PRODUCT_ROADMAP.md', 'docs/RELEASE.md', 'docs/VALIDATION.md', 'docs/README.md',
    'docs/ORCHESTRATION_PARITY_MANIFEST.json', 'docs/mcp-advisory-host.json',
    'docs/RELEASE_PAGE.md',
    'assets/megaprog.svg', 'assets/megaprog.icns',
    'docs/ru/README.md', 'docs/ru/APPROVED_PLAN_MODE.md', 'docs/ru/CONTINUITY.md',
    'docs/ru/DECISIONS.md', 'docs/ru/PRODUCT_ROADMAP.md', 'docs/ru/RELEASE.md',
    'docs/ru/VALIDATION.md', 'docs/de/README.md', 'docs/de/APPROVED_PLAN_MODE.md',
    'docs/de/CONTINUITY.md', 'docs/de/DECISIONS.md', 'docs/de/PRODUCT_ROADMAP.md',
    'docs/de/RELEASE.md', 'docs/de/VALIDATION.md',
    'packaging/app_entry.py', 'packaging/build.py',
    'examples/two-features/approved-plan.json',
    'examples/two-features/project/src/calculator.py',
    'examples/two-features/project/tests/test_add.py',
    'examples/two-features/project/tests/test_multiply.py')
EXECUTABLE_FILES = ('ai-dev',)
REQUIRED_TEXT = {
    'LICENSE': ('GNU GENERAL PUBLIC LICENSE', 'Version 3'),
    'README.md': ('Preview', 'GPL-3.0-only', 'README.ru.md', 'README.de.md'),
    'README.ru.md': ('Предварительная версия', 'GPL-3.0-only'),
    'README.de.md': ('Vorschau', 'GPL-3.0-only'),
    'docs/APPROVED_PLAN_MODE.md': ('schema_version', 'max_model_turns'),
    'THIRD_PARTY_NOTICES.md': ('MegaProg-4.5.1-third-party-source.zip',),
}
FORBIDDEN_MARKERS = ('.ai-dev', '.git', 'runs', 'logs', 'cache', 'caches', 'secret', 'secrets')
FORBIDDEN_SUFFIXES = ('.log', '.tmp', '.secret', '.pem', '.key')


def _fail(message):
    raise ValueError(message)


def _version_from_source(source):
    try:
        tree = ast.parse(source, filename='ai_dev/version.py')
    except SyntaxError as exc:
        _fail('ai_dev/version.py has invalid Python syntax: %s' % exc)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == '__version__'
                for target in node.targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                break
    _fail('ai_dev/version.py does not define a literal __version__.')


def _version_from_pyproject(source):
    # TOML is intentionally read with a small structural check: runtime support
    # starts at Python 3.9, where tomllib is not available.
    if 'version = {attr = "ai_dev.version.__version__"}' not in source.replace("'", '"'):
        _fail('pyproject.toml does not use ai_dev.version.__version__ as its version source.')
    return True


def _archive_entries(zfile):
    try:
        bad_name = zfile.testzip()
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        _fail('ZIP integrity check failed: %s' % exc)
    if bad_name is not None:
        _fail('ZIP integrity check failed: a file could not be read.')
    infos = zfile.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        _fail('Archive contains duplicate ZIP entries.')
    for name in names:
        parts = name.replace('\\', '/').split('/')
        lower_parts = [part.lower() for part in parts]
        if '..' in lower_parts or any(part in FORBIDDEN_MARKERS for part in lower_parts):
            _fail('Forbidden service or secret path in archive: %s' % name)
        if any(part.endswith(FORBIDDEN_SUFFIXES) for part in lower_parts):
            _fail('Forbidden secret or log file in archive: %s' % name)
    return infos, names


def check_archive(archive):
    """Validate *archive* and return a small success summary.

    The archive is only read; it is never extracted and no project state is
    accessed or modified.
    """
    path = Path(archive).expanduser()
    if not path.is_file():
        _fail('Release archive does not exist: %s' % path)
    try:
        zfile = zipfile.ZipFile(path, 'r')
    except (OSError, zipfile.BadZipFile) as exc:
        _fail('Cannot open release ZIP: %s' % exc)
    with zfile:
        infos, names = _archive_entries(zfile)
        manifest_names = [name for name in names
                          if not name.endswith('/') and name.rsplit('/', 1)[-1] == 'manifest.json']
        if len(manifest_names) != 1:
            _fail('Archive must contain exactly one manifest.json.')
        manifest_name = manifest_names[0]
        prefix = manifest_name[:-len('manifest.json')]
        if not prefix or not prefix.endswith('/'):
            _fail('manifest.json must be inside the release directory.')
        try:
            manifest = json.loads(zfile.read(manifest_name).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail('manifest.json is not valid UTF-8 JSON: %s' % exc)
        if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list):
            _fail('manifest.json has an invalid files list.')
        if manifest.get('version') != CURRENT_VERSION:
            _fail('Manifest version %r is not the allowed version %s.' %
                  (manifest.get('version'), CURRENT_VERSION))

        expected = {}
        for item in manifest['files']:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                _fail('Manifest contains an invalid file entry.')
            relative = item['path']
            if relative in expected or relative == 'manifest.json' or relative.startswith('/'):
                _fail('Manifest contains a duplicate or invalid path: %s' % relative)
            if not isinstance(item.get('size'), int) or item['size'] < 0:
                _fail('Manifest has an invalid size for: %s' % relative)
            checksum = item.get('sha256')
            if not isinstance(checksum, str) or len(checksum) != 64:
                _fail('Manifest has an invalid SHA-256 for: %s' % relative)
            expected[relative] = (item['size'], checksum)

        actual = {}
        for info in infos:
            name = info.filename
            if name.endswith('/'):
                continue
            if name == manifest_name:
                continue
            if not name.startswith(prefix):
                _fail('Archive contains a file outside the manifest directory: %s' % name)
            relative = name[len(prefix):]
            data = zfile.read(name)
            actual[relative] = (len(data), hashlib.sha256(data).hexdigest())
        if actual != expected:
            _fail('Manifest file list, sizes, or SHA-256 do not match archive contents.')

        for required in REQUIRED_FILES:
            if required not in actual:
                _fail('Release archive is missing required file: %s' % required)
        for relative in EXECUTABLE_FILES:
            try:
                info = zfile.getinfo(prefix + relative)
            except KeyError:
                _fail('Release archive is missing executable: %s' % relative)
            mode = info.external_attr >> 16
            if info.create_system != 3 or not stat.S_ISREG(mode) or not (mode & 0o111):
                _fail('Release archive has non-executable launcher: %s' % relative)
        for filename, phrases in REQUIRED_TEXT.items():
            try:
                document = zfile.read(prefix + filename).decode('utf-8')
            except UnicodeDecodeError as exc:
                _fail('Required document is not valid UTF-8: %s' % exc)
            missing = [phrase for phrase in phrases if phrase not in document]
            if missing:
                _fail('Required document is incomplete: %s (%s).' %
                      (filename, ', '.join(missing)))
        try:
            source_text = zfile.read(prefix + 'ai_dev/version.py').decode('utf-8')
            pyproject_text = zfile.read(prefix + 'pyproject.toml').decode('utf-8')
        except UnicodeDecodeError as exc:
            _fail('Required version file is not valid UTF-8: %s' % exc)
        source_version = _version_from_source(source_text)
        if source_version != manifest['version']:
            _fail('Version mismatch: manifest.json and ai_dev/version.py.')
        _version_from_pyproject(pyproject_text)
        return {'ok': True, 'archive': str(path), 'version': manifest['version'],
                'files': len(actual)}
