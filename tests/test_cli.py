import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_usage.models import UsageSample
from codex_usage.estimator import estimate
from codex_usage.main import main, render
from codex_usage.monitor import UsageMonitor
from codex_usage.storage import Store
from codex_usage.provider import ProviderError

class FakeProvider:
    def sample(self): return [UsageSample(1800000000,25,banked_reset_count=1)]
    def close(self): pass


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class ScriptedProvider:
    def __init__(self, actions):
        self.actions = actions
        self.closed = False

    def sample(self):
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def close(self):
        self.closed = True


class ProviderFactory:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.instances = []

    def __call__(self, *args, **kwargs):
        provider = ScriptedProvider(self.scripts.pop(0))
        self.instances.append(provider)
        return provider

class CliTests(unittest.TestCase):
    def test_monitor_records_and_projects(self):
        with tempfile.TemporaryDirectory() as temp, Store(Path(temp)/'db.sqlite3') as db:
            result=UsageMonitor(FakeProvider(),db).tick()
            self.assertEqual(len(db.load()),1)
            self.assertEqual(result[0][1].status,'COLLECTING DATA')
    def test_render_unknowns_and_reset_count(self):
        row=FakeProvider().sample()[0]
        text=render([(row,estimate([row]))])
        self.assertIn('75.0%',text)
        self.assertIn('YES (1)',text)
        self.assertIn('collecting data',text)
        self.assertIn('unavailable',text)
    def test_once_live_interface_with_fake_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            path=str(Path(temp)/'usage.sqlite3')
            out=io.StringIO()
            with patch('codex_usage.main.CodexProvider',return_value=FakeProvider()),contextlib.redirect_stdout(out):
                code=main(['watch','--once','--db',path])
            self.assertEqual(code,0)
            self.assertIn('CODEX USAGE',out.getvalue())
            with Store(path) as db: self.assertEqual(len(db.load()),1)
    def test_history_and_export_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            path=str(Path(temp)/'usage.sqlite3')
            with Store(path) as db: db.add(FakeProvider().sample())
            out=io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(main(['history','--db',path]),0)
            self.assertIn('Total measured consumption',out.getvalue())
            out=io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(main(['export','--db',path,'--format','json']),0)
            self.assertEqual(json.loads(out.getvalue())[0]['used_percent'],25)
    def test_invalid_interval(self):
        for value in ('0','-1','nan','inf','2'):
            with self.subTest(value=value),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                main(['watch','--interval',value])
    def test_missing_requested_window_fails_once(self):
        with tempfile.TemporaryDirectory() as temp, patch('codex_usage.main.CodexProvider',return_value=FakeProvider()):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['watch','--once','--window','secondary','--db',str(Path(temp)/'db')]),1)

    def test_watch_recovers_from_provider_failure_and_records_a_later_sample(self):
        sample = [UsageSample(1800000000, 25)]
        factory = ProviderFactory([[ProviderError('temporarily unavailable')], [sample]])
        sleeps = []
        with tempfile.TemporaryDirectory() as temp, \
             patch('codex_usage.main.CodexProvider', side_effect=factory), \
             patch('codex_usage.main.time.sleep', side_effect=sleeps.append), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            path = str(Path(temp) / 'db.sqlite3')
            code = main(['watch', '--samples', '1', '--interval', '10', '--db', path])
            with Store(path) as db:
                stored = db.load()

        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10])
        self.assertEqual(len(stored), 1)
        self.assertTrue(all(provider.closed for provider in factory.instances))

    def test_unbounded_watch_uses_capped_exponential_backoff_and_resets_after_success(self):
        sample = [UsageSample(1800000000, 25)]
        scripts = [[ProviderError(f'failure {index}')] for index in range(6)]
        scripts.extend([[sample, ProviderError('failure after recovery')], [KeyboardInterrupt()]])
        factory = ProviderFactory(scripts)
        sleeps = []
        with tempfile.TemporaryDirectory() as temp, \
             patch('codex_usage.main.CodexProvider', side_effect=factory), \
             patch('codex_usage.main.time.sleep', side_effect=sleeps.append), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            code = main(['watch', '--interval', '10', '--db', str(Path(temp) / 'db.sqlite3')])

        self.assertEqual(code, 0)
        self.assertEqual(sleeps[:6], [10, 20, 40, 80, 160, 300])
        self.assertEqual(sleeps[6:], [10, 10])
        self.assertTrue(all(provider.closed for provider in factory.instances))

    def test_bounded_watch_returns_nonzero_after_three_consecutive_failures(self):
        factory = ProviderFactory([
            [ProviderError('first')],
            [ProviderError('second')],
            [ProviderError('third')],
        ])
        sleeps = []
        with tempfile.TemporaryDirectory() as temp, \
             patch('codex_usage.main.CodexProvider', side_effect=factory), \
             patch('codex_usage.main.time.sleep', side_effect=sleeps.append), \
             contextlib.redirect_stderr(io.StringIO()):
            code = main(['watch', '--samples', '2', '--interval', '10',
                         '--db', str(Path(temp) / 'db.sqlite3')])

        self.assertEqual(code, 1)
        self.assertEqual(len(factory.instances), 3)
        self.assertEqual(sleeps, [10, 20])
        self.assertTrue(all(provider.closed for provider in factory.instances))

    def test_ctrl_c_closes_provider_and_returns_success(self):
        factory = ProviderFactory([[KeyboardInterrupt()]])
        with tempfile.TemporaryDirectory() as temp, \
             patch('codex_usage.main.CodexProvider', side_effect=factory), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(['watch', '--db', str(Path(temp) / 'db.sqlite3')])

        self.assertEqual(code, 0)
        self.assertTrue(factory.instances[0].closed)

    def test_interactive_terminal_is_restored_when_watch_raises(self):
        factory = ProviderFactory([[RuntimeError('unexpected')]])
        terminal = TtyBuffer()
        with tempfile.TemporaryDirectory() as temp, \
             patch('codex_usage.main.CodexProvider', side_effect=factory), \
             contextlib.redirect_stdout(terminal):
            with self.assertRaisesRegex(RuntimeError, 'unexpected'):
                main(['watch', '--db', str(Path(temp) / 'db.sqlite3')])

        self.assertTrue(factory.instances[0].closed)
        self.assertEqual(terminal.getvalue(), '\033[?1049h\033[?25l\033[?25h\033[?1049l')

if __name__=='__main__': unittest.main()
