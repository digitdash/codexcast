import unittest
from dataclasses import replace
from codex_usage.models import UsageSample
from codex_usage.estimator import EstimatorConfig, estimate, summarize

BASE = 1_800_000_000

def samples(minutes=60, rate=12, start=10, reset_hours=6):
    return [UsageSample(BASE+i*30, start+rate*i/120,
                        resets_at=BASE+reset_hours*3600) for i in range(minutes*2+1)]

class EstimatorTests(unittest.TestCase):
    def test_constant_burn(self):
        p=estimate(samples(120))
        self.assertAlmostEqual(p.smoothed_rate,12)
        self.assertAlmostEqual(p.hours_to_empty,66/12)
        for v in p.rates.values(): self.assertAlmostEqual(v,12)
        self.assertAlmostEqual(p.session_rate,12)
        self.assertEqual(p.intensity_confidence,'stable')
        self.assertEqual(p.confidence,'stable')

    def test_bursty_burn_retains_normal_pauses(self):
        rows=samples(30)
        rows=[replace(s,used_percent=10+(i//4)*0.4) for i,s in enumerate(rows)]
        p=estimate(rows)
        self.assertAlmostEqual(p.smoothed_rate,12)
        self.assertLess(p.smoothed_rate,48)
        self.assertIsNone(p.hours_to_empty)  # Thirty minutes is insufficient for sustained burn.

    def test_short_history_retains_intensity_but_cannot_establish_capacity(self):
        rows=samples(10)
        rows += [replace(rows[-1],timestamp=BASE+i*30) for i in range(21,121)]
        p=estimate(rows)
        self.assertAlmostEqual(p.smoothed_rate,12)
        self.assertTrue(p.idle)
        self.assertEqual(p.status,'NO ACTIVE BURN')
        self.assertIsNone(p.projected_remaining)
        self.assertIsNone(p.hours_to_empty)
        self.assertAlmostEqual(p.idle_seconds,45*60)

    def test_resume_after_idle_does_not_dilute(self):
        rows=samples(10)
        rows += [replace(rows[-1],timestamp=BASE+i*30) for i in range(21,81)]
        rows += [replace(rows[-1],timestamp=BASE+40*60+i*30,
                         used_percent=rows[-1].used_percent+i*0.1) for i in range(1,21)]
        p=estimate(rows)
        self.assertFalse(p.idle)
        self.assertAlmostEqual(p.smoothed_rate,12)
        self.assertIsNone(p.hours_to_empty)  # Only twenty observed consuming minutes.

    def test_accelerating_burn_favors_recent(self):
        rows=samples(30,rate=6)
        rows += [replace(rows[-1],timestamp=BASE+1800+i*30,
                         used_percent=13+i*0.2) for i in range(1,31)]
        p=estimate(rows)
        self.assertGreater(p.smoothed_rate,p.session_rate)
        self.assertLess(p.smoothed_rate,24)
        self.assertEqual(p.intensity_confidence,'variable')

    def test_nearly_exhausted(self):
        p=estimate(samples(120,rate=12,start=75))
        self.assertEqual(p.status,'CRITICAL')
        self.assertAlmostEqual(p.hours_to_empty,1/12)

    def test_zero_burn(self):
        p=estimate(samples(rate=0))
        self.assertEqual(p.status,'NO ACTIVE BURN')
        self.assertIsNone(p.hours_to_empty)

    def test_insufficient_history(self):
        p=estimate(samples(2))
        self.assertEqual(p.status,'COLLECTING DATA')
        self.assertIsNone(p.hours_to_empty)
        self.assertIsNone(p.rates[5])

    def test_one_coarse_change_is_not_reliable(self):
        rows=samples(10,rate=0)
        rows[-1]=replace(rows[-1],used_percent=11)
        self.assertEqual(estimate(rows).status,'COLLECTING DATA')

    def test_reset_before_exhaustion(self):
        p=estimate(samples(120,reset_hours=3))
        self.assertEqual(p.status,'SAFE')
        self.assertTrue(p.survives_reset)
        self.assertAlmostEqual(p.projected_remaining,54)

    def test_exhaustion_before_reset(self):
        p=estimate(samples(120,reset_hours=10))
        self.assertEqual(p.status,'PROJECTED EMPTY BEFORE RESET')
        self.assertFalse(p.survives_reset)
        self.assertAlmostEqual(p.projected_remaining,0)

    def test_caution_depends_on_reset_margin(self):
        p=estimate(samples(120,reset_hours=7))
        self.assertEqual(p.status,'CAUTION')
        self.assertAlmostEqual(p.projected_remaining,6)

    def test_imminent_reset_avoids_critical_when_allowance_survives(self):
        rows=samples(120,rate=12,start=75,reset_hours=121/60)
        p=estimate(rows)
        self.assertTrue(p.survives_reset)
        self.assertEqual(p.status,'CAUTION')

    def test_missing_reset(self):
        p=estimate([replace(s,resets_at=None) for s in samples(120)])
        self.assertEqual(p.status,'RESET UNKNOWN')
        self.assertIsNone(p.survives_reset)
        self.assertIsNotNone(p.hours_to_empty)

    def test_elapsed_reset_never_claims_safe(self):
        p=estimate(samples(reset_hours=0.5))
        self.assertEqual(p.status,'AWAITING RESET')
        self.assertIsNone(p.projected_remaining)
        self.assertIsNone(p.hours_to_empty)

    def test_reset_starts_new_epoch(self):
        rows=samples(10)
        rows.append(replace(rows[-1],timestamp=BASE+630,used_percent=0,resets_at=BASE+36000))
        p=estimate(rows)
        self.assertEqual(p.status,'COLLECTING DATA')
        self.assertEqual(p.consumed,0)

    def test_decrease_starts_epoch_even_without_reset_time(self):
        rows=samples(10)
        rows.append(replace(rows[-1],timestamp=BASE+630,used_percent=0))
        self.assertEqual(estimate(rows).consumed,0)

    def test_gap_does_not_attribute_unobserved_consumption(self):
        rows=samples(10)
        rows.append(replace(rows[-1],timestamp=BASE+3600,used_percent=30))
        p=estimate(rows)
        self.assertEqual(p.status,'COLLECTING DATA')
        self.assertEqual(p.active_seconds,0)

    def test_different_windows_rejected(self):
        rows=samples(10)
        rows[-1]=replace(rows[-1],window='secondary')
        with self.assertRaises(ValueError): estimate(rows)

    def test_duplicate_and_out_of_order_time_rejected(self):
        rows=samples(10)
        with self.assertRaises(ValueError): estimate(rows+[rows[-1]])
        with self.assertRaises(ValueError): estimate(list(reversed(rows)))

    def test_poll_cadence_independence(self):
        rows=samples(30)
        self.assertAlmostEqual(estimate(rows).smoothed_rate,estimate(rows[::2]).smoothed_rate)

    def test_summary_across_reset_excludes_reset_delta(self):
        rows=samples(10)
        rows += [replace(s,timestamp=s.timestamp+630,used_percent=s.used_percent-10,
                         resets_at=BASE+36000) for s in samples(10)]
        h=summarize(rows)
        self.assertAlmostEqual(h['consumed'],4)
        self.assertAlmostEqual(h['active_seconds'],1200)
        self.assertAlmostEqual(h['average_active_rate'],12)
        self.assertAlmostEqual(h['median_active_rate'],12)
        self.assertAlmostEqual(h['peak_rate'],12)

    def test_empty(self):
        with self.assertRaises(ValueError): estimate([])
        self.assertEqual(summarize([])['consumed'],0)

    def test_invalid_settings(self):
        for kwargs in [{'idle_minutes':0},{'half_life_minutes':float('nan')},
                       {'min_history_minutes':-1},{'min_events':0},{'max_gap_seconds':0}]:
            with self.assertRaises(ValueError): EstimatorConfig(**kwargs)

if __name__=='__main__': unittest.main()
