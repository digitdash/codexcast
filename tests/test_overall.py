"""Independent synthetic window histories, then the aggregate reset-aware decision."""
import unittest
from dataclasses import replace
from codex_usage.models import UsageSample
from codex_usage.estimator import estimate
from codex_usage.overall import overall_projection

BASE = 1800000000
NOW = BASE + 7200


def window(name='primary', minutes=300, remaining=50, burn=10, reset_in=4,
           points=241, banked=0, limit_id='codex'):
    rows = [UsageSample(NOW-(points-1-i)*30,
                        100-remaining-burn*(points-1-i)/120,
                        limit_id=limit_id, window=name, window_minutes=minutes,
                        resets_at=None if reset_in is None else NOW+reset_in*3600,
                        banked_reset_count=banked) for i in range(points)]
    p = estimate(rows)
    # Aggregation fixtures represent independently calibrated constant-rate bands.
    # Empirical tests separately verify whether real histories qualify for bands.
    active_band = (remaining/burn, remaining/burn) if burn and p.active_hours_to_empty is not None else None
    wall_band = (remaining/burn, remaining/burn) if burn and p.wall_hours_to_empty is not None else None
    if remaining == 0 and p.wall_hours_to_empty == 0:
        active_band = wall_band = (0, 0)
    return rows[-1], replace(p, active_range=active_band, wall_range=wall_band,
                            confidence='HIGH' if active_band else 'COLLECTING',
                            wall_confidence='HIGH' if wall_band else 'COLLECTING')


class OverallTests(unittest.TestCase):
    def test_both_safe_until_reset(self):
        o=overall_projection([window(),window('secondary',10080,reset_in=3)])
        self.assertEqual(o.status,'SAFE THROUGH NEXT RESET')
        self.assertEqual(o.natural_survival,'PASS')
        self.assertIsNone(o.limiting_window)
        self.assertIsNone(o.hours_to_empty)

    def test_short_term_exhausts_first(self):
        o=overall_projection([window(remaining=10),window('secondary',10080,remaining=20)])
        self.assertEqual(o.limiting_window,('codex','primary'))
        self.assertAlmostEqual(o.hours_to_empty,1)
        self.assertEqual(o.natural_survival,'FAIL')

    def test_weekly_exhausts_first(self):
        o=overall_projection([window(remaining=30),window('secondary',10080,remaining=10)])
        self.assertEqual(o.limiting_window,('codex','secondary'))
        self.assertAlmostEqual(o.hours_to_empty,1)

    def test_short_term_resets_before_exhaustion(self):
        o=overall_projection([window(remaining=10,reset_in=.5),
                              window('secondary',10080,remaining=20)])
        self.assertEqual(o.limiting_window,('codex','secondary'))
        self.assertAlmostEqual(o.hours_to_empty,2)

    def test_weekly_resets_before_exhaustion(self):
        o=overall_projection([window(remaining=20),
                              window('secondary',10080,remaining=10,reset_in=.5)])
        self.assertEqual(o.limiting_window,('codex','primary'))

    def test_incomplete_history_reports_available_limit_with_coverage(self):
        o=overall_projection([window(remaining=10),window('secondary',10080,points=1)])
        self.assertEqual(o.status,'PROJECTED QUOTA EXHAUSTION')
        self.assertEqual(o.limiting_window,('codex','primary'))
        self.assertAlmostEqual(o.hours_to_empty,1)
        self.assertEqual(len(o.unresolved_windows),1)
        self.assertEqual(o.known_risk,('codex','primary'))
        self.assertEqual(o.natural_survival,'FAIL')

    def test_both_insufficient_history(self):
        o=overall_projection([window(points=1),window('secondary',10080,points=1)])
        self.assertEqual(o.status,'COLLECTING DATA')
        self.assertEqual(o.natural_survival,'UNKNOWN')

    def test_insufficient_idle_history_does_not_block_another_window(self):
        a,p=window()
        rows=[UsageSample(BASE+i*30,10+i*.1,resets_at=NOW+4*3600) for i in range(21)]
        rows += [replace(rows[-1],timestamp=BASE+i*30) for i in range(21,61)]
        idle=(rows[-1],estimate(rows))
        self.assertTrue(idle[1].idle)
        o=overall_projection([idle,window('secondary',10080,remaining=10)])
        self.assertEqual(o.natural_survival,'FAIL')
        self.assertAlmostEqual(o.hours_to_empty,1)
        self.assertEqual(len(o.unresolved_windows),1)
        self.assertEqual(o.known_risk,('codex','secondary'))

    def test_banked_reset_never_rescues_natural_survival(self):
        o=overall_projection([window(remaining=10,banked=1),window('secondary',10080,banked=1)])
        self.assertEqual(o.banked_reset_count,1)
        self.assertEqual(o.natural_survival,'FAIL')
        self.assertAlmostEqual(o.hours_to_empty,1)

    def test_limiting_window_changes_as_independent_burn_changes(self):
        weekly=window('secondary',10080,remaining=20,burn=10)
        first=overall_projection([window(remaining=30,burn=10),weekly])
        later=overall_projection([window(remaining=30,burn=20),weekly])
        self.assertEqual(first.limiting_window,('codex','secondary'))
        self.assertEqual(later.limiting_window,('codex','primary'))
        self.assertAlmostEqual(later.hours_to_empty,1.5)

    def test_unknown_reset_blocks_all_safe_and_precise_eta(self):
        o=overall_projection([window(reset_in=None),window('secondary',10080)])
        self.assertEqual(o.natural_survival,'UNKNOWN')
        self.assertIsNone(o.hours_to_empty)
        self.assertEqual(o.status,'RESET RACE UNCERTAIN')

    def test_elapsed_reset_requires_fresh_data(self):
        o=overall_projection([window(reset_in=-1),window('secondary',10080)])
        self.assertEqual(o.status,'RESET RACE UNCERTAIN')
        self.assertEqual(o.natural_survival,'UNKNOWN')

    def test_currently_empty_is_known_even_without_history_elsewhere(self):
        o=overall_projection([window(remaining=0,points=1),window('secondary',10080,points=1)])
        self.assertEqual(o.limiting_window,('codex','primary'))
        self.assertEqual(o.hours_to_empty,0)
        self.assertEqual(o.natural_survival,'FAIL')

    def test_idle_alone_is_not_all_safe(self):
        o=overall_projection([window(burn=0)])
        self.assertEqual(o.status,'NO ACTIVE BURN')
        self.assertEqual(o.natural_survival,'UNKNOWN')

    def test_dynamic_duration_and_other_bucket_are_candidates(self):
        o=overall_projection([window(),window('secondary',777,remaining=10,limit_id='other')])
        self.assertEqual(o.limiting_window,('other','secondary'))

    def test_unavailable_and_conflicting_reset_counts_stay_unknown(self):
        self.assertIsNone(overall_projection([window(banked=None)]).banked_reset_count)
        self.assertIsNone(overall_projection([window(banked=1),window('secondary',banked=0)]).banked_reset_count)

    def test_confidence_remains_visible(self):
        a,p=window(remaining=10)
        o=overall_projection([(a,replace(p,wall_confidence='LOW'))])
        self.assertEqual(o.confidence,'LOW')

    def test_empty_input_is_collecting(self):
        self.assertEqual(overall_projection([]).status,'COLLECTING DATA')

if __name__=='__main__': unittest.main()
