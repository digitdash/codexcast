"""Setup prerequisites are checked before any personal files are changed."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
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
