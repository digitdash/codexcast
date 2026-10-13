import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from codex_usage.models import UsageSample
from codex_usage.monitor import has_recent_usage
from codex_usage.main import main
from codex_usage.storage import Store

NOW=1800007200
SPARK='codex_bengalfox'


def sample(timestamp, used=0, window='primary', **kwargs):
    return UsageSample(timestamp,used,limit_id=SPARK,window=window,
                       window_minutes=300,resets_at=NOW+3600,**kwargs)


class RecentUsageTests(unittest.TestCase):
    def check(self, rows):
        with tempfile.TemporaryDirectory() as temp, Store(Path(temp)/'db') as store:
            store.add(rows)
            return has_recent_usage(store, SPARK, NOW)

    def test_recent_increase_shows_bucket(self):
        self.assertTrue(self.check([sample(NOW-90,10),sample(NOW-60,11)]))

    def test_first_use_after_unused_window_reset_timestamp_moves(self):
        self.assertTrue(self.check([sample(NOW-30,0),replace(sample(NOW,1),resets_at=NOW+7200)]))

    def test_unchanged_nonzero_usage_is_not_recent(self):
        self.assertFalse(self.check([sample(NOW-90,10),sample(NOW,10)]))

    def test_single_nonzero_sample_is_not_evidence(self):
        self.assertFalse(self.check([sample(NOW,10)]))

    def test_hour_boundary_includes_preceding_baseline(self):
        self.assertTrue(self.check([sample(NOW-3630,10),sample(NOW-3600,11)]))
        self.assertFalse(self.check([sample(NOW-3631,10),sample(NOW-3601,11)]))

    def test_reset_and_observation_gaps_do_not_imply_recent_usage(self):
        self.assertFalse(self.check([sample(NOW-300,10),sample(NOW,11)]))
        self.assertFalse(self.check([sample(NOW-30,10),replace(sample(NOW,11),resets_at=NOW+7200)]))
        self.assertFalse(self.check([sample(NOW-30,10),sample(NOW,1)]))

    def test_either_window_counts_without_mixing_windows(self):
        self.assertTrue(self.check([sample(NOW-90,10,'secondary'),sample(NOW-60,11,'secondary')]))
        self.assertFalse(self.check([sample(NOW-90,0),sample(NOW-60,11,'secondary')]))

    def test_reading_from_future_is_ignored(self):
        self.assertFalse(self.check([sample(NOW-30,10),sample(NOW+30,11)]))

    def test_combined_terminal_retains_all_returned_windows_and_history(self):
        self.run_watch(recent=False)

    def test_terminal_shows_both_windows_after_recent_use_persisted_across_restart(self):
        self.run_watch(recent=True)

    def run_watch(self, recent):
        rows=[UsageSample(NOW,30,window_minutes=10080),sample(NOW,10),sample(NOW,20,'secondary')]
        class Provider:
            def sample(self): return rows
            def close(self): pass
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'db'
            with Store(path) as store:
                store.add([sample(NOW-60,9 if recent else 10)])
            output=io.StringIO()
            with patch('codex_usage.main.CodexProvider',return_value=Provider()),contextlib.redirect_stdout(output):
                self.assertEqual(main(['watch','--once','--db',str(path)]),0)
            with Store(path) as store:
                self.assertEqual(len(store.load()),4)
            text=output.getvalue()
            self.assertIn('codex / primary',text)
            self.assertIn('3 exposed windows',text)
            self.assertIn(f'{SPARK} / primary',text)
            self.assertEqual(text.count('OVERALL'),1)
            self.assertNotIn('\nGPT-5.3-Codex-Spark /',text)
            self.assertIn(f'{SPARK} / secondary',text)

if __name__=='__main__': unittest.main()
