"""Build the allow-listed source archive and a self-contained macOS preview app."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
DIST = Path(os.environ.get('MEGAPROG_BUILD_DIR', str(ROOT / 'releases')))
sys.path.insert(0, str(ROOT))
from ai_dev.version import __version__ as VERSION

ROOT_FILES = ('.gitignore', 'AGENTS.md', 'CONTRIBUTING.md', 'LICENSE', 'README.md',
              'README.ru.md', 'README.de.md', 'ДЛЯ_ДРУГОГО_ЧАТА.txt', 'SECURITY.md',
              'SECURITY.ru.md', 'SECURITY.de.md', 'CHANGELOG.md', '.github/workflows/tests.yml',
              'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'requirements-build.txt', 'ai-dev', 'ai-dev.cmd', 'package.json', 'package-lock.json', 'launch_gui.py',
              'mcp_host/megaprog-mcp-host.mjs', 'mcp_host/verify.mjs')
DOC_FILES = ('docs/APPROVED_PLAN_MODE.md', 'docs/CONTINUITY.md', 'docs/DECISIONS.md',
             'docs/PRODUCT_ROADMAP.md', 'docs/RELEASE.md', 'docs/VALIDATION.md',
             'docs/README.md', 'docs/RELEASE_PAGE.md', 'docs/PREVIEW_VALIDATION_REPORT.md', 'docs/ORCHESTRATION_PARITY_MANIFEST.json',
             'docs/mcp-advisory-host.json', 'docs/GUI_DESIGN.md')
for language in ('ru', 'de'):
    DOC_FILES += tuple('docs/%s/%s' % (language, name) for name in
        ('README.md', 'APPROVED_PLAN_MODE.md', 'CONTINUITY.md', 'DECISIONS.md',
         'PRODUCT_ROADMAP.md', 'RELEASE.md', 'VALIDATION.md', 'PREVIEW_VALIDATION_REPORT.md'))
PACKAGING_FILES = ('packaging/app_entry.py', 'packaging/build.py', 'packaging/generate_icon.py', 'packaging/preview_gui.py')
SCREENSHOT_FILES = tuple('docs/screenshots/%s-%s.png' % (language, page)
                         for language in ('ru', 'de', 'en')
                         for page in ('plan', 'result'))
RUNTIME_LICENSE_FILES = tuple(sorted(path for path in (ROOT / 'licenses').iterdir() if path.is_file()))
APP_ICON = ROOT / 'assets' / 'megaprog.icns'
QT_FRAMEWORKS_IN_RELEASE = frozenset({
    'QtCore', 'QtDBus', 'QtGui', 'QtNetwork', 'QtOpenGL', 'QtSvg', 'QtWidgets',
})
QT_FRAMEWORKS_TO_REMOVE = frozenset({
    'QtPdf', 'QtQml', 'QtQmlMeta', 'QtQmlModels', 'QtQmlWorkerScript',
    'QtQuick', 'QtVirtualKeyboard', 'QtVirtualKeyboardQml',
})


def _commit():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()


def _require_clean_source():
    status = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=ROOT)
    if status:
        raise SystemExit('Release requires a clean Git worktree; commit the reviewed source first.')


def _source_files():
    result = [ROOT / name for name in ROOT_FILES + DOC_FILES + PACKAGING_FILES + SCREENSHOT_FILES]
    result += sorted((ROOT / 'ai_dev').glob('*.py'))
    result += sorted((ROOT / 'ai_dev').glob('*.cmd'))
    result += sorted((ROOT / 'tests').glob('test_*.py'))
    result += sorted((ROOT / 'assets').glob('*.svg'))
    result += sorted((ROOT / 'assets').glob('*.icns'))
    result += list(RUNTIME_LICENSE_FILES)
    result += sorted(path for path in (ROOT / 'examples' / 'two-features').rglob('*')
                     if path.is_file())
    missing = [str(path.relative_to(ROOT)) for path in result if not path.is_file()]
    if missing:
        raise SystemExit('Missing allow-listed source files: ' + ', '.join(missing))
    return result


def _write_source_zip(archive):
    root_name = 'MegaProg-' + VERSION + '-source'
    entries = []
    for path in _source_files():
        relative = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        entries.append({'path': relative, 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'manifest_version': 1, 'product': 'MegaProg', 'version': VERSION,
                'commit': _commit(), 'license': 'GPL-3.0-only', 'files': entries}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in _source_files():
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(root_name + '/' + relative, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)) << 16
            z.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo(root_name + '/manifest.json', (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        z.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def _build_macos_app(destination):
    if sys.platform != 'darwin' or __import__('platform').machine() != 'arm64':
        raise SystemExit('The first preview app build is supported only on Apple Silicon macOS.')
    if not APP_ICON.is_file():
        raise SystemExit('The MegaProg macOS app icon is missing: %s' % APP_ICON)
    pyinstaller = _pyinstaller_command(destination)
    subprocess.run(pyinstaller, cwd=ROOT, check=True)
    bundle = destination / 'dist' / 'MegaProg.app'
    if not (bundle / 'Contents' / 'MacOS' / 'MegaProg').is_file():
        raise SystemExit('PyInstaller did not create a macOS app bundle.')
    _prune_optional_qt(bundle)
    resources = bundle / 'Contents' / 'Resources'
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'LICENSE', resources / 'LICENSE')
    shutil.copyfile(ROOT / 'THIRD_PARTY_NOTICES.md', resources / 'THIRD_PARTY_NOTICES.md')
    _copy_runtime_licenses(resources)
    plist = bundle / 'Contents' / 'Info.plist'
    if plist.is_file():
        import plistlib
        data = plistlib.loads(plist.read_bytes())
        data.update({'CFBundleDisplayName': 'MegaProg', 'CFBundleShortVersionString': VERSION,
                     'CFBundleVersion': VERSION, 'CFBundleIdentifier': 'org.popovantondev.megaprog'})
        plist.write_bytes(plistlib.dumps(data, sort_keys=True))
    # Finder hidden flags are cosmetic; hidden plugins cannot be discovered by Qt.
    # Clear this flag on the newly built bundle only, before signing it.
    if hasattr(os, 'chflags'):
        for entry in bundle.rglob('*'):
            if not entry.is_symlink():
                flags=entry.stat().st_flags
                if flags & stat.UF_HIDDEN: os.chflags(entry,flags & ~stat.UF_HIDDEN)
    _seal_macos_app(bundle)
    shutil.copytree(bundle, destination / 'MegaProg.app', symlinks=True)


def _prune_optional_qt(bundle):
    """Remove Qt plugin dependencies unused by the Widgets-only preview app."""
    frameworks = bundle / 'Contents' / 'Frameworks'
    resources = bundle / 'Contents' / 'Resources'
    qt_lib = frameworks / 'PySide6' / 'Qt' / 'lib'
    plugins = frameworks / 'PySide6' / 'Qt' / 'plugins'
    for relative in (
        'imageformats/libqpdf.dylib',
        'platforminputcontexts/libqtvirtualkeyboardplugin.dylib',
    ):
        (plugins / relative).unlink(missing_ok=True)
    for name in QT_FRAMEWORKS_TO_REMOVE:
        for path in (qt_lib / (name + '.framework'), frameworks / name, resources / name):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
    if qt_lib.is_dir():
        remaining = {path.stem for path in qt_lib.glob('Qt*.framework')}
        unexpected = remaining - QT_FRAMEWORKS_IN_RELEASE
        if unexpected:
            raise SystemExit('Unreviewed Qt frameworks in app bundle: ' + ', '.join(sorted(unexpected)))


def _pyinstaller_command(destination):
    """Return the pinned build command, including the reviewed product icon."""
    from PySide6.QtCore import QLibraryInfo
    translations=Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
            '--name', 'MegaProg', '--icon', str(APP_ICON), '--paths', str(ROOT),
            '--distpath', str(destination / 'dist'), '--workpath', str(destination / 'work'),
            '--specpath', str(destination / 'spec'), '--hidden-import', 'PySide6.QtSvg',
            '--add-data', str(ROOT / 'assets') + ':assets',
            '--add-data', str(ROOT / 'examples' / 'two-features') + ':examples/two-features',
            str(ROOT / 'packaging' / 'app_entry.py')]
    for language in ('ru','de'):
        path=translations / ('qtbase_'+language+'.qm')
        if not path.is_file(): raise SystemExit('Missing Qt translation: '+str(path))
        command += ['--add-data', str(path)+':PySide6/Qt/translations']
    return command


def _copy_runtime_licenses(resources):
    destination = resources / 'LICENSES'
    destination.mkdir(parents=True, exist_ok=True)
    for source in RUNTIME_LICENSE_FILES:
        shutil.copyfile(source, destination / source.name)


def _seal_macos_app(bundle):
    codesign = shutil.which('codesign')
    if not codesign:
        raise SystemExit('codesign is required to seal the completed Preview app bundle.')
    subprocess.run([codesign, '--force', '--deep', '--sign', '-', str(bundle)], check=True)
    subprocess.run([codesign, '--verify', '--deep', '--strict', str(bundle)], check=True)


def _zip_app(bundle, archive):
    ditto = shutil.which('ditto')
    if ditto:
        subprocess.run([ditto, '-c', '-k', '--sequesterRsrc', '--keepParent', str(bundle), str(archive)], check=True)
    else:
        # Preserve symlink entries and executable modes for non-macOS inspection tools.
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for path in sorted(bundle.rglob('*')):
                rel = path.relative_to(bundle.parent).as_posix()
                info = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                if path.is_symlink():
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, os.readlink(path))
                elif path.is_file():
                    info.external_attr = (stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    z.writestr(info, path.read_bytes())


def build(dist=None):
    _require_clean_source()
    starting_commit = _commit()
    dist = Path(dist) if dist else DIST
    dist.mkdir(parents=True, exist_ok=True)
    if any(dist.iterdir()):
        raise SystemExit('Build output directory must be empty.')
    source = dist / ('MegaProg-' + VERSION + '-source.zip')
    app_archive = dist / ('MegaProg-' + VERSION + '-macos-arm64.zip')
    _write_source_zip(source)
    with tempfile.TemporaryDirectory(prefix='megaprog-build-') as temporary:
        temp = Path(temporary)
        _build_macos_app(temp)
        _zip_app(temp / 'MegaProg.app', app_archive)
    if _commit() != starting_commit:
        raise SystemExit('Source commit changed during release build.')
    checksums = dist / 'SHA256SUMS.txt'
    checksums.write_text(''.join('%s  %s\n' % (hashlib.sha256(p.read_bytes()).hexdigest(), p.name)
                                 for p in (app_archive, source)), encoding='utf-8')
    for artifact in (app_archive, source):
        print('%s\n%d bytes\nsha256 %s' % (artifact, artifact.stat().st_size,
                                             hashlib.sha256(artifact.read_bytes()).hexdigest()))
    return app_archive, source, checksums


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DIST,
                        help='empty directory for the app, source ZIP, and checksums')
    build(parser.parse_args().output)
