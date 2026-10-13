import unittest
from dataclasses import replace
from codex_usage.estimator import estimate
from codex_usage.main import render
from codex_usage.overall import overall_projection
from test_estimator import samples, BASE
from test_overall import window


def append(rows, minutes, burn=0):
    last=rows[-1]
    return rows+[replace(last,timestamp=last.timestamp+i*30,
                         used_percent=last.used_percent+burn*i/120)
                 for i in range(1,int(minutes*2)+1)]


class SustainedTests(unittest.TestCase):
    def test_single_extreme_burst_does_not_control_capacity(self):
        rows=append(samples(120,rate=6),30)
        rows=append(rows,.5,burn=2400)
        p=estimate(rows)
        self.assertGreater(p.smoothed_rate,60)
        self.assertAlmostEqual(getattr(p,'sustained_rate',None) or -1,6)
        self.assertAlmostEqual(p.active_hours_to_empty,58/6)

    def test_irregular_poll_burst_cannot_control_two_of_three_blocks(self):
        rows=[replace(samples(1)[0],timestamp=BASE+i*37,
                      used_percent=10+6*i*37/3600+(20 if i>=33 else 0),
                      resets_at=BASE+86400) for i in range(152)]
        self.assertIsNone(estimate(rows[:82]).active_hours_to_empty)
        self.assertAlmostEqual(estimate(rows).sustained_rate,6)

    def test_extreme_burst_followed_by_normal_work_uses_normal_rate(self):
        rows=append(append(samples(120,rate=6),30),.5,burn=2400)
        p=estimate(append(rows,30,burn=6))
        self.assertAlmostEqual(p.sustained_rate,6)
        self.assertAlmostEqual(p.active_hours_to_empty,55/6)

    def test_inconsistent_sustained_blocks_have_variable_confidence(self):
        rows=samples(0,reset_hours=24)
        for burn in (3,6,9,12,15,18,21):
            rows=append(rows,15,burn=burn)
        p=estimate(rows)
        self.assertAlmostEqual(p.sustained_rate,12)
        self.assertEqual(p.confidence,'variable')

    def test_burst_then_idle_keeps_capacity_and_rate(self):
        rows=append(append(samples(120,rate=6),30),.5,burn=2400)
        before=estimate(rows); idle=estimate(append(rows,180))
        self.assertTrue(idle.idle)
        self.assertIsNotNone(idle.active_hours_to_empty)
        self.assertAlmostEqual(idle.active_hours_to_empty,before.active_hours_to_empty)
        self.assertAlmostEqual(getattr(idle,'sustained_rate',None) or -1,6)

    def test_constant_sustained_burn(self):
        p=estimate(samples(120))
        self.assertAlmostEqual(getattr(p,'sustained_rate',None) or -1,12)
        self.assertAlmostEqual(p.active_hours_to_empty,5.5)
        self.assertEqual(p.confidence,'stable')

    def test_variable_but_consistent_blocks_are_stable(self):
        rows=samples(120)
        rows=[replace(s,used_percent=10+(i//2)*.2+(i%2)*.05) for i,s in enumerate(rows)]
        p=estimate(rows)
        self.assertAlmostEqual(getattr(p,'sustained_rate',None) or -1,12)
        self.assertEqual(p.confidence,'stable')

    def test_higher_long_term_rate_eventually_replaces_old_rate(self):
        p=estimate(append(samples(120,rate=6),120,burn=18))
        self.assertAlmostEqual(getattr(p,'sustained_rate',None) or -1,18)
        self.assertAlmostEqual(p.active_hours_to_empty,42/18)

    def test_idle_does_not_erase_historical_capacity(self):
        p=estimate(append(samples(120,reset_hours=24),240))
        self.assertTrue(p.idle)
        self.assertAlmostEqual(p.active_hours_to_empty or -1,5.5)

    def test_short_resume_after_overnight_idle_retains_capacity(self):
        rows=append(samples(120,rate=6,reset_hours=48),400)
        p=estimate(append(rows,.5,burn=6))
        self.assertIsNotNone(p.active_hours_to_empty)
        self.assertAlmostEqual(p.active_hours_to_empty,77.95/6)
        self.assertEqual(p.confidence,'low')

    def test_resume_updates_capacity_from_actual_consumption(self):
        idle=append(samples(120),60)
        before=estimate(idle); after=estimate(append(idle,30,burn=12))
        self.assertIsNotNone(before.active_hours_to_empty)
        self.assertAlmostEqual(after.active_hours_to_empty,5)
        self.assertFalse(after.idle)

    def test_little_active_history_cannot_establish_sustained_rate(self):
        p=estimate(samples(20))
        self.assertIsNone(p.active_hours_to_empty)
        self.assertEqual(p.confidence,'collecting')

    def test_coarse_changes_after_idle_are_aggregated_over_activity(self):
        rows=[replace(s,used_percent=10+i//20) for i,s in enumerate(samples(240,rate=0))]
        p=estimate(rows)
        self.assertGreater(p.smoothed_rate,100)
        self.assertLess(getattr(p,'sustained_rate',None) or 1000,15)
        self.assertGreater(p.active_hours_to_empty or 0,4)
        self.assertLess(p.effective_wall_rate,8)

    def test_reset_timestamp_jitter_does_not_erase_sustained_history(self):
        rows=[replace(s,resets_at=s.resets_at+(i%31==0)) for i,s in enumerate(samples(180,rate=6))]
        p=estimate(rows)
        self.assertIsNotNone(p.active_hours_to_empty)
        self.assertAlmostEqual(getattr(p,'sustained_rate',None) or -1,6)

    def test_real_reset_and_duration_change_invalidate_capacity(self):
        rows=samples(120)
        for changes in ({'used_percent':0},{'window_minutes':777}):
            p=estimate(rows+[replace(rows[-1],timestamp=rows[-1].timestamp+30,**changes)])
            self.assertIsNone(p.active_hours_to_empty)

    def test_gap_consumption_is_not_used_as_burn(self):
        rows=samples(120,rate=6)
        p=estimate(rows+[replace(rows[-1],timestamp=rows[-1].timestamp+3600,used_percent=50)])
        self.assertAlmostEqual(getattr(p,'sustained_rate',None) or -1,6)
        self.assertAlmostEqual(p.active_hours_to_empty,50/6)

    def test_sustained_rate_uses_only_recent_activity(self):
        old=samples(120,rate=6,reset_hours=24)
        rows=append(old,400)
        rows=append(rows,120,burn=18)
        self.assertAlmostEqual(getattr(estimate(rows),'sustained_rate',None) or -1,18)

    def test_inactive_spark_does_not_hide_available_wall_forecast(self):
        o=overall_projection([window(remaining=10),window(limit_id='codex_bengalfox',burn=0)])
        self.assertEqual(o.active_limiting_window,('codex','primary'))
        self.assertEqual(o.limiting_window,('codex','primary'))
        self.assertAlmostEqual(o.wall_hours_to_empty,1)
        self.assertEqual(len(o.unresolved_windows),1)
        self.assertEqual(getattr(o,'wall_forecast_count',0),1)

    def test_render_explains_exhaustion_and_partial_coverage(self):
        text=render([window(remaining=10),window(limit_id='codex_bengalfox',burn=0)])
        self.assertRegex(text,r'Current intensity: +10.0')
        self.assertRegex(text,r'Active capacity remaining: +~1h 00m')
        self.assertRegex(text,r'Reset race: +DEPLETION WINS')
        self.assertRegex(text,r'Projected at reset: +EXHAUSTED')
        self.assertRegex(text,r'Dead before reset: +3h 00m')
        self.assertRegex(text,r'Forecast coverage: +1/2')

    def test_render_reset_wins_and_unknown(self):
        text=render([window(reset_in=1)])
        self.assertRegex(text,r'Reset race: +RESET WINS')
        self.assertRegex(text,r'Projected at reset: +40.0%')
        self.assertRegex(render([window(points=1)]),r'Reset race: +UNKNOWN')

if __name__=='__main__':unittest.main()
