import math
import unittest
from dataclasses import replace
from test_estimator import BASE
from codex_usage.models import UsageSample
try:
    from codex_usage.backtest import backtest, empirical_bounds, calibrate
    from codex_usage.uncertainty import forecast
except ImportError:
    backtest=empirical_bounds=calibrate=forecast=None


def history(minutes=780, burn=1, remaining=1, reset_in=24, step=60):
    end=BASE+minutes*60
    start_used=100-remaining-burn*minutes/60
    return [UsageSample(BASE+i*step,start_used+burn*i*step/3600,
                        resets_at=end+reset_in*3600,window_minutes=10080)
            for i in range(minutes*60//step+1)]


class EmpiricalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(backtest,'Empirical backtesting not implemented')

    def test_perfect_consumption_has_zero_error_at_every_available_horizon(self):
        report=backtest(history())
        for key,methods in report['metrics'].items():
            for name,values in methods.items():
                if values['count']:
                    self.assertAlmostEqual(values['median_absolute_error_pp'],0)
                    self.assertAlmostEqual(values['p80_relative_error'],0)
        self.assertGreater(report['metrics']['active/60']['sustained']['count'],10)

    def test_suffix_does_not_change_forecasts_at_earlier_origins(self):
        rows=history()
        changed=[replace(s,used_percent=s.used_percent+(1 if s.timestamp>BASE+6*3600 else 0)) for s in rows]
        a=backtest(rows)['evaluations']; b=backtest(changed)['evaluations']
        before={(x['clock'],x['horizon_minutes'],x['origin']):x['predicted'] for x in a if x['origin']<=BASE+6*3600}
        after={(x['clock'],x['horizon_minutes'],x['origin']):x['predicted'] for x in b if x['origin']<=BASE+6*3600}
        self.assertEqual(before,after)

    def test_active_targets_are_exact_and_outcomes_do_not_overlap(self):
        rows=history(step=37)
        report=backtest(rows)
        for horizon in (15,30,60,120):
            ev=[e for e in report['evaluations'] if e['clock']=='active' and e['horizon_minutes']==horizon]
            for e in ev:self.assertAlmostEqual(e['active_seconds'],horizon*60)
            for a,b in zip(ev,ev[1:]):self.assertLessEqual(a['available_at'],b['origin'])

    def test_robust_finite_sample_range_ignores_one_extreme_outlier(self):
        bounds=empirical_bounds(4,[math.log(2)]*9+[math.log(1e40)])
        self.assertAlmostEqual(bounds[0],2)
        self.assertAlmostEqual(bounds[1],8)
        self.assertIsNone(empirical_bounds(4,[math.log(2)]*9))

    def test_variable_errors_widen_range(self):
        low=empirical_bounds(4,[math.log(1.1)]*20)
        high=empirical_bounds(4,[math.log(3)]*20)
        self.assertGreater(high[1]-high[0],low[1]-low[0])
        self.assertGreater(empirical_bounds(8,[math.log(3)]*20)[1]-empirical_bounds(8,[math.log(3)]*20)[0],high[1]-high[0])

    def test_zero_consumption_can_produce_open_upper_bound(self):
        self.assertEqual(empirical_bounds(4,[0]*8+[math.inf]*2),(0,None))

    def test_matured_errors_only_and_matching_horizon(self):
        report=backtest(history())
        now=BASE+6*3600
        full=calibrate(report['evaluations'],clock='active',point_hours=.25,now=now)
        prefix=calibrate(backtest(history()[:361])['evaluations'],clock='active',point_hours=.25,now=now)
        self.assertEqual(full,prefix)
        self.assertIsNone(calibrate(report['evaluations'],clock='active',point_hours=10,now=BASE+13*3600)['range'])

    def test_active_and_wall_intervals_and_evidence_confidence(self):
        p=forecast(history())
        self.assertAlmostEqual(p.active_range[0],1)
        self.assertAlmostEqual(p.active_range[1],1)
        self.assertAlmostEqual(p.wall_range[0],1)
        self.assertAlmostEqual(p.wall_range[1],1)
        self.assertEqual(p.confidence,'MEDIUM')
        self.assertEqual(p.wall_confidence,'MEDIUM')
        self.assertGreaterEqual(p.active_evidence['count'],10)

    def test_confidence_increases_with_useful_evidence(self):
        low=forecast(history(minutes=180))
        high=forecast(history(minutes=2400))
        self.assertEqual(low.confidence,'LOW')
        self.assertEqual(high.confidence,'HIGH')

    def test_long_sparse_history_cannot_become_confident(self):
        rows=history(minutes=2400,burn=0)
        rows=[replace(s,used_percent=10+min(i//500,4)) for i,s in enumerate(rows)]
        p=forecast(rows)
        self.assertIn(p.confidence,('COLLECTING','LOW'))
        self.assertIn(p.wall_confidence,('COLLECTING','LOW'))

    def test_reset_races_use_ranges(self):
        self.assertEqual(forecast(history(reset_in=2)).reset_race,'DEPLETION WINS')
        self.assertEqual(forecast(history(reset_in=.5)).reset_race,'RESET WINS')
        self.assertEqual(forecast(history(minutes=180,reset_in=2)).reset_race,'UNCERTAIN')

    def test_missing_wall_polling_does_not_become_zero_consumption(self):
        rows=history()
        # The first wall target starts after duty qualifies at two hours.
        rows=[s for s in rows if s.timestamp not in (BASE+140*60,BASE+141*60)]
        report=backtest(rows)
        partial=[e for e in report['evaluations'] if e['clock']=='wall' and e['coverage']<.999]
        self.assertTrue(partial)
        for e in partial:
            self.assertAlmostEqual(e['actual'],e['predicted']['sustained'])
            self.assertFalse(e.get('calibration_eligible',True))

    def test_quota_exhaustion_censors_future_rate_labels(self):
        rows=history(minutes=360,remaining=0)
        report=backtest(rows)
        self.assertFalse(any(e['available_at']==rows[-1].timestamp for e in report['evaluations']))

    def test_history_objects_are_never_modified(self):
        rows=history(); before=list(rows)
        backtest(rows)
        self.assertEqual(rows,before)

if __name__=='__main__':unittest.main()
