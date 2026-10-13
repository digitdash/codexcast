import unittest
from dataclasses import replace
from codex_usage.estimator import estimate, EstimatorConfig
from codex_usage.models import UsageSample

BASE=1800000000


def trace(minutes=240, active_minutes=10, cycle_minutes=60, rate=12,
          remaining=50, reset_in_hours=2):
    used=0.0
    rows=[UsageSample(BASE,used,resets_at=BASE+minutes*60+reset_in_hours*3600)]
    for i in range(1,minutes*2+1):
        if ((i-1)*.5) % cycle_minutes < active_minutes:
            used += rate/120
        rows.append(replace(rows[0],timestamp=BASE+i*30,used_percent=used))
    offset=100-remaining-used
    return [replace(s,used_percent=s.used_percent+offset) for s in rows]


class DutyCycleTests(unittest.TestCase):
    def test_constant_active_burn_has_separate_active_and_wall_estimates(self):
        p=estimate(trace(120,60,60,rate=12))
        self.assertAlmostEqual(p.active_hours_to_empty,50/12)
        self.assertAlmostEqual(p.duty_cycle,1)
        self.assertAlmostEqual(p.effective_wall_rate,12)
        self.assertAlmostEqual(p.wall_hours_to_empty,50/12)

    def test_active_history_does_not_establish_wall_confidence(self):
        p=estimate(trace(90,60,60,remaining=50,rate=26.6,reset_in_hours=142))
        self.assertIsNotNone(p.active_hours_to_empty)
        self.assertIsNone(p.duty_cycle)
        self.assertIsNone(p.effective_wall_rate)
        self.assertIsNone(p.wall_hours_to_empty)
        self.assertIsNone(p.projected_remaining)
        self.assertIsNone(p.survives_reset)
        self.assertEqual(p.reset_survival,'UNKNOWN')
        self.assertEqual(p.wall_confidence,'collecting')
        self.assertEqual(p.intensity_confidence,'stable')

    def test_insufficient_active_and_wall_history(self):
        p=estimate(trace(2,60,60))
        self.assertIsNone(p.active_hours_to_empty)
        self.assertIsNone(p.wall_hours_to_empty)

    def test_active_then_long_idle_reduces_wall_rate(self):
        p=estimate(trace(180,90,180,rate=12))
        self.assertTrue(p.idle)
        self.assertAlmostEqual(p.duty_cycle,95/180)
        self.assertAlmostEqual(p.effective_wall_rate,12*95/180)
        self.assertGreater(p.wall_hours_to_empty,50/12)

    def test_repeated_active_idle_cycles_use_existing_five_minute_grace(self):
        p=estimate(trace(360,10,60))
        self.assertAlmostEqual(p.duty_cycle,.25)
        self.assertAlmostEqual(p.duty_active_seconds,90*60)
        self.assertAlmostEqual(p.duty_observed_seconds,360*60)

    def test_low_duty_cycle_can_survive_when_continuous_use_would_not(self):
        p=estimate(trace(360,10,60,remaining=10,reset_in_hours=2))
        self.assertAlmostEqual(p.smoothed_rate,12)
        self.assertLess(10/p.smoothed_rate,2)
        self.assertAlmostEqual(p.sustained_rate,8)  # Two points per 15 active minutes incl. grace.
        self.assertAlmostEqual(p.wall_hours_to_empty,5)
        self.assertTrue(p.survives_reset)
        self.assertEqual(p.reset_survival,'SAFE')
        self.assertAlmostEqual(p.projected_remaining,6)

    def test_high_duty_cycle_exhausts_before_reset_and_clamps(self):
        p=estimate(trace(240,40,60,remaining=10,reset_in_hours=3))
        self.assertAlmostEqual(p.duty_cycle,.75)
        self.assertLess(p.wall_hours_to_empty,3)
        self.assertFalse(p.survives_reset)
        self.assertEqual(p.reset_survival,'PROJECTED EMPTY BEFORE RESET')
        self.assertEqual(p.projected_remaining,0)

    def test_projection_never_exceeds_one_hundred(self):
        p=estimate(trace(360,10,60,remaining=80,reset_in_hours=.01))
        self.assertGreaterEqual(p.projected_remaining,0)
        self.assertLessEqual(p.projected_remaining,100)

    def test_rolling_cycle_adapts_to_new_behavior(self):
        old=trace(360,10,60,rate=3)
        newer=trace(360,50,60,rate=3)
        newer=[replace(s,timestamp=s.timestamp+360*60,
                       used_percent=s.used_percent-newer[0].used_percent+old[-1].used_percent,
                       resets_at=old[-1].resets_at+360*60) for s in newer]
        old=[replace(s,resets_at=newer[-1].resets_at) for s in old]
        # Keep cumulative quota valid while retaining a single epoch.
        rows=old+newer[1:]
        p=estimate(rows)
        self.assertAlmostEqual(estimate(old).duty_cycle,.25)
        self.assertAlmostEqual(p.duty_cycle,55/60)
        self.assertAlmostEqual(p.duty_observed_seconds,6*3600)

    def test_gaps_are_not_observed_idle_and_reduce_coverage(self):
        rows=trace(360,10,60,rate=3)
        rows=[s for s in rows if not BASE+60*60 < s.timestamp < BASE+300*60]
        p=estimate(rows)
        self.assertLess(p.duty_coverage,.8)
        self.assertIsNone(p.wall_hours_to_empty)
        self.assertIsNone(p.projected_remaining)

    def test_at_least_thirty_active_minutes_required(self):
        p=estimate(trace(120,1,120))
        self.assertLess(p.duty_active_seconds,1800)
        self.assertIsNone(p.duty_cycle)

    def test_at_least_five_usage_changes_required(self):
        rows=trace(180,60,60,rate=0)
        rows=[replace(s,used_percent=10+min(i//120,2)) for i,s in enumerate(rows)]
        p=estimate(rows)
        self.assertLess(p.duty_events,5)
        self.assertIsNone(p.wall_hours_to_empty)

    def test_past_reset_never_uses_wall_forecast_for_safety(self):
        p=estimate(trace(240,10,60,reset_in_hours=-1))
        self.assertEqual(p.reset_survival,'UNKNOWN')
        self.assertIsNone(p.projected_remaining)

    def test_boundary_two_hours_and_coverage_are_measured(self):
        self.assertIsNone(estimate(trace(119,60,60)).wall_hours_to_empty)
        self.assertIsNotNone(estimate(trace(120,60,60)).wall_hours_to_empty)

    def test_invalid_duty_settings(self):
        for values in ({'duty_window_hours':0},{'min_wall_history_hours':0},
                       {'min_wall_active_minutes':0},{'min_wall_events':0},
                       {'min_wall_coverage':1.1},{'min_wall_coverage':0},
                       {'min_wall_history_hours':7}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                EstimatorConfig(**values)

if __name__=='__main__': unittest.main()
