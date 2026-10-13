"""Setup prerequisites are checked before any personal files are changed."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install-local-plugin.py'
spec = importlib.util.spec_from_file_location('plugin_installer', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def require(self, name):
        function = getattr(installer, name, None)
        self.assertTrue(callable(function), f'Installer needs {name} for one-command setup')
        return function

    def executable(self, path, exit_code=0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nexit {exit_code}\n')
        path.chmod(0o700)
        return path

    def test_discovers_desktop_bundle_before_an_unrelated_path_cli(self):
        find = self.require('find_codex')
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            desktop = self.executable(home / 'Applications/ChatGPT.app/Contents/Resources/'
                                      'codex-cli/CodexCLI.app/Contents/MacOS/codex')
            other = self.executable(home / 'bin/codex', 2)
            with patch.object(Path, 'home', return_value=home), patch.object(installer.shutil, 'which', return_value=str(other)):
                self.assertEqual(find(), str(desktop.resolve()))

    def test_accepts_an_explicit_executable_path_with_spaces(self):
        find = self.require('find_codex')
        with tempfile.TemporaryDirectory() as temp:
            binary = self.executable(Path(temp) / 'Desktop App/codex')
            self.assertEqual(find(str(binary)), str(binary.resolve()))

    def test_skips_an_older_bundle_when_another_desktop_client_supports_plugins(self):
        find = self.require('find_codex')
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.executable(home / 'Applications/ChatGPT.app/Contents/Resources/'
                            'codex-cli/CodexCLI.app/Contents/MacOS/codex', 2)
            newer = self.executable(home / 'Applications/Codex.app/Contents/Resources/codex')
            with patch.object(Path, 'home', return_value=home), patch.object(installer.shutil, 'which', return_value=None):
                self.assertEqual(find(), str(newer.resolve()))

    def test_skips_unsupported_bundles_for_a_supported_path_client(self):
        find = self.require('find_codex')
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.executable(home / 'Applications/ChatGPT.app/Contents/Resources/'
                            'codex-cli/CodexCLI.app/Contents/MacOS/codex', 2)
            newer = self.executable(home / 'bin/codex')
            with patch.object(Path, 'home', return_value=home), \
                    patch.object(installer.shutil, 'which', return_value=str(newer)), \
                    patch.object(installer.subprocess, 'run') as run:
                run.side_effect = lambda command, **kwargs: SimpleNamespace(
                    returncode=0 if Path(command[0]) == newer.resolve() else 2)
                self.assertEqual(find(), str(newer.resolve()))

    def test_a_new_custom_codex_home_is_created_after_prerequisite_checks(self):
        main = self.require('main')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'codex'
            binary.write_text('#!/bin/sh\n[ -d "$CODEX_HOME" ] || exit 2\nexit 0\n')
            binary.chmod(0o700)
            home = root / 'new-profile'
            installed = {'marketplace': str(root / '.agents/plugins/marketplace.json'),
                         'marketplace_name': 'local-codex-usage'}
            with patch.object(installer.sys, 'platform', 'darwin'), \
                    patch.object(installer, 'install', return_value=installed), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = main(['--codex-bin', str(binary), '--codex-home', str(home)])
            self.assertEqual(result, 0)
            self.assertTrue(home.is_dir())

    def test_invalid_marketplace_layout_fails_before_writing_files(self):
        main = self.require('main')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = self.executable(root / 'codex')
            stderr = io.StringIO()
            with patch.object(installer.sys, 'platform', 'darwin'), \
                    patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                    contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                result = main(['--codex-bin', str(binary), '--destination', str(root / 'plugin'),
                               '--runtime', str(root / 'runtime'), '--codex-home', str(root / 'profile'),
                               '--marketplace', str(root / 'catalog/marketplace.json')])
            self.assertEqual(result, 1)
            self.assertIn('.agents/plugins/marketplace.json', stderr.getvalue())
            self.assertFalse((root / 'plugin').exists())
            self.assertFalse((root / 'runtime').exists())
            self.assertFalse((root / 'catalog').exists())

    def test_missing_explicit_executable_gives_an_actionable_error(self):
        find = self.require('find_codex')
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'codex-bin'):
                find(str(Path(temp) / 'missing'))

    def test_old_client_fails_before_creating_installation_files(self):
        main = self.require('main')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = self.executable(root / 'old-codex', 2)
            stderr = io.StringIO()
            with patch.object(installer.sys, 'platform', 'darwin'), contextlib.redirect_stderr(stderr):
                result = main(['--codex-bin', str(binary), '--destination', str(root / 'plugin'),
                               '--runtime', str(root / 'runtime'), '--marketplace', str(root / '.agents/plugins/marketplace.json')])
            self.assertEqual(result, 1)
            self.assertIn('plugin', stderr.getvalue().lower())
            self.assertFalse((root / 'plugin').exists())
            self.assertFalse((root / 'runtime').exists())
            self.assertFalse((root / '.agents').exists())

    def test_unsupported_platform_fails_before_creating_installation_files(self):
        main = self.require('main')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stderr = io.StringIO()
            with patch.object(installer.sys, 'platform', 'win32'), contextlib.redirect_stderr(stderr):
                result = main(['--destination', str(root / 'plugin'), '--runtime', str(root / 'runtime')])
            self.assertEqual(result, 1)
            self.assertIn('macOS', stderr.getvalue())
            self.assertFalse((root / 'plugin').exists())
            self.assertFalse((root / 'runtime').exists())

    def test_old_python_reports_the_required_version_before_writes(self):
        main = self.require('main')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stderr = io.StringIO()
            with patch.object(installer.sys, 'version_info', (3, 9, 0)), contextlib.redirect_stderr(stderr):
                result = main(['--destination', str(root / 'plugin'), '--runtime', str(root / 'runtime')])
            self.assertEqual(result, 1)
            self.assertIn('Python 3.11', stderr.getvalue())
            self.assertFalse((root / 'plugin').exists())
            self.assertFalse((root / 'runtime').exists())

    def configuration_args(self, home, destination, runtime_root=None):
        return SimpleNamespace(db=str(home / 'Library/Application Support/codex-usage-estimator/usage.sqlite3'),
                               codex_home=str(home / '.codex'), codex_bin='/usr/local/bin/codex',
                               destination=str(destination), collector_runtime=runtime_root)

    def test_long_home_configuration_initializes_the_actual_collector(self):
        configure = self.require('make_configuration')
        from codex_usage import collector
        home = Path('/Users/alexandermillersmith')
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=home), \
                patch.object(collector, 'DEFAULT_DB', home / 'Library/Application Support/codex-usage-estimator/usage.sqlite3'):
            args = self.configuration_args(home, Path(temp) / 'plugin')
            result = collector.CollectorConfig(**configure(args))
            self.assertLess(len(installer.os.fsencode(result.socket)), 104)
            self.assertEqual(result.root.parent, home / '.codex/usage-runtime')

    def test_deep_home_uses_a_bounded_persistent_collector_directory(self):
        configure = self.require('make_configuration')
        from codex_usage import collector
        home = Path('/Volumes/network-homes') / ('deep-directory/' * 20) / 'user'
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=home), \
                patch.object(collector, 'DEFAULT_DB', home / 'Library/Application Support/codex-usage-estimator/usage.sqlite3'):
            result = collector.CollectorConfig(**configure(self.configuration_args(home, Path(temp) / 'plugin')))
            self.assertLess(len(installer.os.fsencode(result.socket)), 104)
            self.assertEqual(result.root.parent, Path('/Users/Shared') / f'.codex-usage-{installer.os.getuid()}')

    def test_unusable_new_home_preserves_an_existing_installation(self):
        main = self.require('main')
        import json
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = self.executable(root / 'codex')
            destination = root / 'plugin'
            (destination / '.codex-plugin').mkdir(parents=True)
            (destination / '.codex-plugin/plugin.json').write_text(json.dumps({'name':'codex-usage'}))
            original = '{"codex_home":"original-home"}\n'
            (destination / 'runtime.json').write_text(original)
            blocked = root / 'not-a-directory'
            blocked.write_text('preserve this file')
            stderr = io.StringIO()
            with patch.object(installer.sys, 'platform', 'darwin'), \
                    patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                    contextlib.redirect_stderr(stderr):
                result = main(['--codex-bin', str(binary), '--codex-home', str(blocked / 'profile'),
                               '--destination', str(destination), '--runtime', str(root / 'runtime'),
                               '--marketplace', str(root / '.agents/plugins/marketplace.json')])
            self.assertEqual(result, 1)
            self.assertEqual((destination / 'runtime.json').read_text(), original)
            self.assertEqual(blocked.read_text(), 'preserve this file')
            self.assertFalse((root / 'runtime').exists())
            self.assertFalse((root / '.agents').exists())

    def test_shared_fallback_directory_is_private_and_rejects_symlinks(self):
        secure = self.require('ensure_private_directory')
        import stat
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'runtime'
            secure(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            link = Path(temp) / 'linked'
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, 'private directory'):
                secure(link)

    def test_unwritable_existing_home_preserves_the_registered_plugin(self):
        main = self.require('main')
        import json
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = self.executable(root / 'codex')
            home = root / 'profile'
            home.mkdir()
            plugin = root / 'plugin'
            (plugin / '.codex-plugin').mkdir(parents=True)
            (plugin / '.codex-plugin/plugin.json').write_text(json.dumps({'name':'codex-usage'}))
            original = '{"codex_home":"original-home"}\n'
            (plugin / 'runtime.json').write_text(original)
            with patch.object(installer.sys, 'platform', 'darwin'), \
                    patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                    patch.object(installer.tempfile, 'TemporaryFile', side_effect=PermissionError('Read-only Codex home')), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                result = main(['--codex-bin', str(binary), '--codex-home', str(home),
                               '--destination', str(plugin), '--runtime', str(root / 'runtime'),
                               '--marketplace', str(root / '.agents/plugins/marketplace.json')])
            self.assertEqual(result, 1)
            self.assertEqual((plugin / 'runtime.json').read_text(), original)
            self.assertFalse((root / 'runtime').exists())
            self.assertFalse((root / '.agents').exists())

    def test_shared_fallback_rejects_a_directory_owned_by_another_user(self):
        secure = self.require('ensure_private_directory')
        import stat
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'runtime'
            path.mkdir()
            foreign = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=installer.os.getuid()+1)
            with patch.object(Path, 'lstat', return_value=foreign), self.assertRaisesRegex(ValueError, 'owned by you'):
                secure(path)

    def test_shared_fallback_rejects_an_existing_directory_that_is_not_private(self):
        secure = self.require('ensure_private_directory')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'runtime'
            path.mkdir()
            path.chmod(0o777)
            child = path / 'predictable-key'
            child.mkdir()
            victim = root / 'victim-file'
            victim.write_text('preserve this file')
            (child / 'baselines.sqlite3').symlink_to(victim)
            with self.assertRaisesRegex(ValueError, 'already be private'):
                secure(path)
            self.assertEqual(victim.read_text(), 'preserve this file')
            self.assertTrue((child / 'baselines.sqlite3').is_symlink())

    def test_compatible_home_keeps_the_existing_default_baseline_location(self):
        configure = self.require('make_configuration')
        from codex_usage import collector
        home = Path('/Users/testuser')
        db = home / 'Library/Application Support/codex-usage-estimator/usage.sqlite3'
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=home), \
                patch.object(collector, 'DEFAULT_DB', db):
            args = self.configuration_args(home, Path(temp) / 'plugin')
            result = collector.CollectorConfig(**configure(args))
            self.assertEqual(result.root.parent, db.parent / 'runtime')

    def test_repeat_install_reuses_an_existing_custom_baseline_location(self):
        configure = self.require('make_configuration')
        from codex_usage import collector
        import json
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'plugin'
            destination.mkdir()
            (destination / 'runtime.json').write_text(json.dumps({'runtime_root':'/tmp/cu-preserved'}))
            result = collector.CollectorConfig(**configure(self.configuration_args(Path('/Users/testuser'), destination)))
            self.assertEqual(result.root.parent, Path('/tmp/cu-preserved').resolve())

    def test_relative_collector_directory_is_pinned_to_the_install_location(self):
        configure = self.require('make_configuration')
        with tempfile.TemporaryDirectory(prefix='cc-path-', dir='/tmp') as temp, contextlib.chdir(temp):
            args = self.configuration_args(Path('/Users/testuser'), Path(temp) / 'plugin', 'sockets')
            self.assertEqual(configure(args)['runtime_root'], str((Path(temp) / 'sockets').resolve()))

    def test_explicit_overlong_socket_directory_fails_before_writes(self):
        main = self.require('main')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = self.executable(root / 'codex')
            stderr = io.StringIO()
            with patch.object(installer.sys, 'platform', 'darwin'), contextlib.redirect_stderr(stderr):
                try:
                    result = main(['--codex-bin', str(binary), '--destination', str(root / 'plugin'),
                                   '--runtime', str(root / 'runtime'), '--marketplace', str(root / '.agents/plugins/marketplace.json'),
                                   '--codex-home', str(root / 'profile'),
                                   '--collector-runtime', '/tmp/'+'long'*35])
                except SystemExit as error:
                    result = error.code
            self.assertEqual(result, 1)
            self.assertIn('socket path', stderr.getvalue())
            self.assertFalse((root / 'plugin').exists())
            self.assertFalse((root / 'runtime').exists())
