import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

BUILD_PATH = Path(__file__).parents[1] / 'packaging' / 'build.py'
SPEC = importlib.util.spec_from_file_location('megaprog_build', BUILD_PATH)
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)

from ai_dev.release_check import check_archive
from ai_dev.version import __version__


class PackagingTests(unittest.TestCase):
    def test_preview_removes_unneeded_qt_frameworks_and_plugins(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / 'MegaProg.app'
            qt_lib = bundle / 'Contents/Frameworks/PySide6/Qt/lib'
            plugins = bundle / 'Contents/Frameworks/PySide6/Qt/plugins'
            (qt_lib / 'QtWidgets.framework').mkdir(parents=True)
            (qt_lib / 'QtPdf.framework').mkdir()
            (qt_lib / 'QtQml.framework').mkdir()
            (plugins / 'imageformats').mkdir(parents=True)
            (plugins / 'imageformats/libqpdf.dylib').write_bytes(b'plugin')
            BUILD._prune_optional_qt(bundle)
            self.assertTrue((qt_lib / 'QtWidgets.framework').exists())
            self.assertFalse((qt_lib / 'QtPdf.framework').exists())
            self.assertFalse((qt_lib / 'QtQml.framework').exists())
            self.assertFalse((plugins / 'imageformats/libqpdf.dylib').exists())
            (qt_lib / 'QtUnknown.framework').mkdir()
            with self.assertRaisesRegex(SystemExit, 'Unreviewed Qt frameworks'):
                BUILD._prune_optional_qt(bundle)

    def test_source_archive_is_deterministic_allowlisted_and_checkable(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / 'one.zip'
            second = Path(folder) / 'two.zip'
            with patch.object(BUILD, '_commit', return_value='a' * 40):
                BUILD._write_source_zip(first)
                BUILD._write_source_zip(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            result = check_archive(first)
            self.assertEqual(result['version'], __version__)
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertTrue(any(n.endswith('/README.ru.md') for n in names))
                self.assertTrue(any(n.endswith('/README.de.md') for n in names))
                self.assertTrue(any(n.endswith('/LICENSE') for n in names))
                self.assertTrue(any(n.endswith('/assets/megaprog.icns') for n in names))
                for required in ('ai_dev/gui_window.py','ai_dev/native_monitor_windows.cmd','mcp_host/verify.mjs','mcp_host/megaprog-mcp-host.mjs'):
                    self.assertTrue(any(n.endswith('/'+required) for n in names),required)
                for name in ('CPython-3.12.14-LICENSE.txt', 'PyInstaller-6.22.2-COPYING.txt',
                             'Tcl-9.0.4-license.terms', 'Tk-9.0.4-license.terms',
                             'Qt-qttranslations-6.10.2-BSD-3-Clause.txt',
                             'PySide6-6.10.2-GPL-3.0-only.txt'):
                    self.assertTrue(any(n.endswith('/licenses/' + name) for n in names))
                self.assertFalse(any('olya' in n.lower() or 'telegramarchive' in n.lower()
                                     for n in names))
                manifest_path = next(n for n in names if n.endswith('/manifest.json'))
                manifest = json.loads(archive.read(manifest_path))
                prefix = manifest_path[:-len('manifest.json')]
                for item in manifest['files']:
                    data = archive.read(prefix + item['path'])
                    self.assertEqual(item['size'], len(data))
                    self.assertEqual(item['sha256'], hashlib.sha256(data).hexdigest())

    def test_macos_app_build_uses_megaprog_icon(self):
        command = BUILD._pyinstaller_command(Path('/tmp/megaprog-preview'))
        icon_index = command.index('--icon')
        self.assertEqual(command[icon_index + 1], str(BUILD.APP_ICON))
        self.assertTrue(BUILD.APP_ICON.is_file())
        example_data = str(BUILD.ROOT / 'examples' / 'two-features') + ':examples/two-features'
        self.assertTrue(any(command[index + 1] == example_data
                            for index, value in enumerate(command[:-1])
                            if value == '--add-data'))

    def test_release_check_rejects_changed_source_file(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / 'source.zip'
            with patch.object(BUILD, '_commit', return_value='a' * 40):
                BUILD._write_source_zip(archive_path)
            damaged = Path(folder) / 'damaged.zip'
            with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(damaged, 'w') as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename.endswith('/README.md'):
                        data += b'changed\n'
                    target.writestr(info, data)
            with self.assertRaisesRegex(ValueError, 'Manifest file list'):
                check_archive(damaged)

    def test_build_requires_clean_source(self):
        with patch.object(BUILD.subprocess, 'check_output', return_value=b' M ai_dev/cli.py\n'):
            with self.assertRaisesRegex(SystemExit, 'clean Git worktree'):
                BUILD._require_clean_source()

    def test_mac_app_zip_preserves_symlink_and_executable_mode(self):
        if shutil.which('ditto'):
            self.skipTest('ditto owns native macOS bundle metadata on this host')
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / 'MegaProg.app'
            executable = bundle / 'Contents' / 'MacOS' / 'MegaProg'
            executable.parent.mkdir(parents=True)
            executable.write_text('app', encoding='utf-8')
            executable.chmod(0o755)
            (bundle / 'Contents' / 'MacOS' / 'Alias').symlink_to('MegaProg')
            archive = Path(folder) / 'app.zip'
            BUILD._zip_app(bundle, archive)
            with zipfile.ZipFile(archive) as z:
                info = z.getinfo('MegaProg.app/Contents/MacOS/MegaProg')
                self.assertTrue(stat.S_IMODE(info.external_attr >> 16) & 0o111)
                link_info = z.getinfo('MegaProg.app/Contents/MacOS/Alias')
                self.assertTrue(stat.S_ISLNK(link_info.external_attr >> 16))

    def test_app_license_copy_contains_all_runtime_license_texts(self):
        with tempfile.TemporaryDirectory() as folder:
            resources = Path(folder) / 'Resources'
            resources.mkdir()
            BUILD._copy_runtime_licenses(resources)
            for source in BUILD.RUNTIME_LICENSE_FILES:
                copied = resources / 'LICENSES' / source.name
                self.assertEqual(copied.read_bytes(), source.read_bytes())

    def test_finished_bundle_is_signed_after_metadata_and_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / 'MegaProg.app'
            bundle.mkdir()
            with (patch.object(BUILD.shutil, 'which', return_value='/usr/bin/codesign') as locate,
                  patch.object(BUILD.subprocess, 'run') as run):
                BUILD._seal_macos_app(bundle)
            locate.assert_called_once_with('codesign')
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][:5],
                             ['/usr/bin/codesign', '--force', '--deep', '--sign', '-'])
            self.assertEqual(run.call_args_list[1].args[0][:4],
                             ['/usr/bin/codesign', '--verify', '--deep', '--strict'])


if __name__ == '__main__':
    unittest.main()
