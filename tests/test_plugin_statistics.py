import tempfile
import unittest
from pathlib import Path
from codex_usage.storage import Store
from codex_usage.models import UsageSample
from codex_usage.tokens import TokenEvent
from codex_usage.estimator import EstimatorConfig
try:
    from codex_usage.collection import collect, statistics
    from codex_usage.plugin_state import Baselines
except ImportError:
    collect = statistics = Baselines = None


class PluginStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(statistics, 'Shared offline statistics are missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'usage.sqlite3')
        self.addCleanup(self.store.close)
        self.config = EstimatorConfig(min_history_minutes=1, min_events=2)
        self.samples = [UsageSample(1800000000+i*30, i, window_minutes=300,
                                   resets_at=1800018000) for i in range(11)]
        self.store.add(self.samples)

    def test_statistics_does_not_poll_or_write_samples(self):
        result = statistics(self.store, [self.samples[-1]], self.config)
        self.assertIsNotNone(result['windows'][0]['projection']['smoothed_rate'])
        self.assertEqual(self.store.load(), self.samples)

    def test_current_reset_is_isolated_global_reset_applies_to_both_and_persists(self):
        path = Path(self.temp.name)/'baselines.sqlite3'
        with Baselines(path) as state:
            state.reset('session-a', 1800000299)
            self.assertEqual(state.since('session-a'), 1800000299)
            self.assertIsNone(state.since('session-b'))
            reset = statistics(self.store, [self.samples[-1]], self.config,
                               since=state.since('session-a'))
            self.assertIsNone(reset['windows'][0]['projection']['smoothed_rate'])
            state.reset(None, 1800000300)
            self.assertEqual(state.since('session-a'), 1800000300)
            self.assertEqual(state.since('session-b'), 1800000300)
        with Baselines(path) as state:
            self.assertEqual(state.since('session-a'), 1800000300)
        self.assertEqual(self.store.load(), self.samples)

    def test_crossing_and_unknown_response_boundaries_excluded_after_reset(self):
        events = [TokenEvent('crossing', 1800000300, 1800000250, 100, 'native',
                            response_started_at=1800000000),
                  TokenEvent('unknown', 1800000300, 1800000250, 100, 'legacy'),
                  TokenEvent('after', 1800000300, 1800000250, 100, 'native',
                            response_started_at=1800000250)]
        self.store.add_token_events(events)
        result = statistics(self.store, [self.samples[-1]], self.config, since=1800000200)
        self.assertEqual(result['credits']['observations'], 1)
        self.assertEqual(len(self.store.load_token_events()), 3)

    def test_collect_retains_account_readings_without_duplicate_response_events(self):
        sample = self.samples[-1]
        class Provider:
            supports_token_telemetry = True
            credit_balance = '42'
            def sample(self): return [sample]
        class Reader:
            def read(self, *, now):
                return [TokenEvent('one', sample.timestamp, sample.timestamp-10, 5, 'native')], [], []
        collect(Provider(), self.store, Reader())
        collect(Provider(), self.store, Reader())
        self.assertEqual(len(self.store.load_token_events()), 1)
        self.assertEqual(self.store.load_account_credits(since=0), [(sample.timestamp, '42')])

    def test_invalid_session_does_not_create_reset_state(self):
        with Baselines(Path(self.temp.name)/'baselines.sqlite3') as state:
            for invalid in ('', 'x'*201, '../session', '\n'):
                with self.assertRaises(ValueError): state.reset(invalid, 1800000300)
            self.assertIsNone(state.since('session-a'))
