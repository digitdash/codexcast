"""Refills and seconds of reset-clock jitter must not erase learned usage."""
import unittest
from dataclasses import replace
from codex_usage.estimator import estimate, summarize
from codex_usage.main import render
from codex_usage.uncertainty import forecast
from codex_usage.backtest import backtest
from test_estimator import samples


class ResetContinuityTests(unittest.TestCase):
    def test_one_second_jitter_preserves_observed_changes_and_duty(self):
        for jitter in (1, 2):
            with self.subTest(jitter=jitter):
                rows = [replace(s, resets_at=s.resets_at + (i % 2)*jitter)
                        for i, s in enumerate(samples(180, rate=6))]
                p = estimate(rows)
                self.assertAlmostEqual(p.consumed, 18)
                self.assertAlmostEqual(p.smoothed_rate, 6)
                self.assertAlmostEqual(p.duty_cycle, 1)
                self.assertAlmostEqual(p.active_hours_to_empty, 12)
                self.assertEqual(p.events, 360)

    def test_jitter_does_not_restart_idle_grace_on_every_poll(self):
        rows = [replace(s, window_minutes=10080) for s in samples(180, rate=6)]
        last = rows[-1]
        rows += [replace(last, timestamp=last.timestamp+i*30, resets_at=last.resets_at+i%2)
                 for i in range(1, 121)]
        p = estimate(rows)
        self.assertTrue(p.idle)
        self.assertAlmostEqual(p.duty_active_seconds, 185*60)
        self.assertAlmostEqual(p.duty_cycle, 185/240)

    def test_confirmed_refill_retains_labeled_low_confidence_capacity(self):
        rows = [replace(s, window_minutes=10080) for s in samples(180, rate=6)]
        last = rows[-1]
        rows.append(replace(last, timestamp=last.timestamp+30, used_percent=0,
                            resets_at=last.resets_at+86400))
        p = forecast(rows)
        self.assertIsNotNone(p.sustained_rate)
        self.assertAlmostEqual(p.sustained_rate, 6)
        self.assertAlmostEqual(p.active_hours_to_empty, 100/6)
        self.assertIsNotNone(p.wall_hours_to_empty)
        self.assertTrue(p.sustained_previous_cycle)
        self.assertEqual(p.confidence, 'LOW')
        self.assertEqual(p.wall_confidence, 'LOW')
        self.assertIn('previous quota cycle', render([(rows[-1], p)]))
        self.assertAlmostEqual(summarize(rows)['consumed'], 18)

    def test_new_cycle_evidence_replaces_prior_cycle_rate(self):
        rows = [replace(s, window_minutes=10080) for s in samples(180, rate=6)]
        last = rows[-1]
        refill = replace(last, timestamp=last.timestamp+30, used_percent=0,
                         resets_at=last.resets_at+86400)
        rows.append(refill)
        rows += [replace(refill, timestamp=refill.timestamp+i*30, used_percent=i*18/120)
                 for i in range(1, 241)]
        p = estimate(rows)
        self.assertAlmostEqual(p.sustained_rate, 18)
        self.assertIs(getattr(p, "sustained_previous_cycle", None), False)

    def test_unconfirmed_drop_and_duration_change_still_invalidate_capacity(self):
        rows = [replace(s, window_minutes=10080) for s in samples(180, rate=6)]
        last = rows[-1]
        for changes in ({'used_percent': 0}, {'used_percent': 0, 'resets_at': None},
                        {'used_percent': 0, 'window_minutes': 777, 'resets_at': last.resets_at+86400}):
            with self.subTest(changes=changes):
                p = estimate(rows+[replace(last, timestamp=last.timestamp+30, **changes)])
                self.assertIsNone(p.sustained_rate)

    def test_large_reset_shift_still_splits_current_observations(self):
        rows = [replace(s, window_minutes=10080) for s in samples(180, rate=6)]
        last = rows[-1]
        rows.append(replace(last, timestamp=last.timestamp+30, used_percent=last.used_percent+10,
                            resets_at=last.resets_at+3600))
        self.assertEqual(estimate(rows).events, 0)
        self.assertAlmostEqual(summarize(rows)['consumed'], 18)

    def test_jitter_backtests_keep_complete_observation_coverage(self):
        rows = [replace(s, resets_at=s.resets_at+i%2)
                for i, s in enumerate(samples(480, rate=6, reset_hours=48))]
        report = backtest(rows)
        metric = report['metrics']['wall/15']['sustained']
        self.assertGreater(metric['calibration_count'], 10)
        self.assertAlmostEqual(metric['median_absolute_error_pp'], 0)


if __name__ == '__main__':
    unittest.main()
