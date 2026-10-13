import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from codex_usage.backtest import backtest
from codex_usage.estimator import estimate
from codex_usage.main import main, render
from codex_usage.models import UsageSample
from codex_usage.overall import overall_projection
from codex_usage.storage import Store
from codex_usage.uncertainty import forecast, with_uncertainty
from test_empirical import history, BASE


class EmpiricalCliTests(unittest.TestCase):
    def test_backtest_command_is_read_only_and_reports_all_predictors(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            with Store(path) as store:store.add(history())
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            out=io.StringIO()
            with contextlib.redirect_stdout(out),patch('codex_usage.main.CodexProvider',side_effect=AssertionError('backtest must be offline')):
                self.assertEqual(main(['backtest','--db',str(path),'--json']),0)
            report=json.loads(out.getvalue())[0]['report']
            self.assertEqual(set(report['metrics']['active/15']),{'15m','30m','60m','120m','session','sustained','intensity'})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_watch_does_not_claim_binary_race_without_empirical_support(self):
        rows=history(minutes=180)
        spark=replace(rows[-1],limit_id='codex_bengalfox',used_percent=0)
        class Provider:
            def sample(self):return [rows[-1],spark]
            def close(self):pass
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            with Store(path) as store:store.add(rows[:-1])
            out=io.StringIO()
            with contextlib.redirect_stdout(out),patch('codex_usage.main.CodexProvider',return_value=Provider()):
                self.assertEqual(main(['watch','--once','--db',str(path)]),0)
            text=out.getvalue()
            self.assertRegex(text,r'Active capacity remaining: +~1h 00m')
            self.assertRegex(text,r'Likely active range: +collecting data')
            self.assertRegex(text,r'Reset race: +UNCERTAIN')
            self.assertRegex(text,r'Active confidence: +LOW')
            self.assertNotIn('DEPLETION WINS',text)
            self.assertNotIn('\ncodex_bengalfox /',text)
            self.assertIn('[####################]',text)
            self.assertIn('OVERALL\n\nScope:',text)
            with Store(path) as store:self.assertEqual(len(store.load()),len(rows)+1)

    def test_range_crossing_reset_is_uncertain_in_window_and_overall(self):
        rows=history(reset_in=1.1)
        report=backtest(rows)
        for e in report['evaluations']:
            if 'sustained' in e['predicted']:e['actual']=2*e['predicted']['sustained']
        p=with_uncertainty(rows,estimate(rows),report)
        self.assertAlmostEqual(p.wall_range[0],.5)
        self.assertAlmostEqual(p.wall_range[1],2)
        self.assertEqual(p.reset_race,'UNCERTAIN')
        self.assertEqual(overall_projection([(rows[-1],p)]).reset_race,'UNCERTAIN')
        self.assertIn('UNCERTAIN',render([(rows[-1],p)]))
        self.assertIsNone(p.projected_remaining)

    def test_missing_reset_preserves_unknown_status_with_valid_estimate(self):
        rows=[replace(s,resets_at=None) for s in history()]
        p=forecast(rows)
        self.assertEqual(p.status,'RESET UNKNOWN')
        self.assertEqual(p.reset_race,'UNKNOWN')
        self.assertIsNotNone(p.wall_range)

    def test_idle_outcomes_widen_wall_range_without_erasing_active_capacity(self):
        rows=[];used=0
        for minute in range(80*60+1):
            if minute and ((minute-1)//60)%3==0:used+=1/60
            rows.append(UsageSample(BASE+minute*60,used,resets_at=BASE+130*3600))
        rows=[replace(s,used_percent=s.used_percent+99.75-used) for s in rows]
        p=forecast(rows)
        self.assertTrue(p.idle)
        self.assertIsNotNone(p.active_hours_to_empty)
        self.assertIsNotNone(p.active_range[1])
        self.assertEqual(p.wall_range,(0,None))
        self.assertEqual(p.wall_confidence,'LOW')
        self.assertEqual(p.reset_race,'UNCERTAIN')
        json.dumps(p.wall_evidence,allow_nan=False)

    def test_changing_consumption_has_measured_forecast_error(self):
        rows=[];used=0
        for minute in range(30*60+1):
            if minute:used+=(1 if ((minute-1)//60)%2==0 else 3)/60
            rows.append(UsageSample(BASE+minute*60,used,resets_at=BASE+100*3600))
        stats=backtest(rows)['metrics']['active/60']['sustained']
        self.assertGreater(stats['p80_absolute_error_pp'],.5)
        self.assertGreater(stats['count'],10)

    def test_overall_range_accounts_for_other_forecastable_windows(self):
        rows=history()
        a=forecast(rows)
        b=replace(a,active_hours_to_empty=2,active_range=(.5,3),wall_hours_to_empty=2,wall_range=(.5,3))
        o=overall_projection([(rows[-1],a),(replace(rows[-1],window='secondary'),b)])
        self.assertEqual(o.active_limiting_window,('codex','primary'))
        self.assertAlmostEqual(o.active_range[0],.5)
        self.assertAlmostEqual(o.active_range[1],1)

    def test_overall_distinguishes_possible_first_limit_from_known_failure(self):
        rows=history()
        p=forecast(rows)
        possible=replace(p,wall_hours_to_empty=2,wall_range=(1,10),hours_until_reset=3,reset_race='UNCERTAIN')
        known=replace(p,wall_hours_to_empty=5,wall_range=(4,6),hours_until_reset=7,reset_race='DEPLETION WINS')
        results=[(rows[-1],possible),(replace(rows[-1],window='secondary'),known)]
        text=render(results)
        self.assertRegex(text,r'Cause: +codex / primary .* possible quota exhaustion')
        self.assertRegex(text,r'Known risk: +codex / secondary .* exhausts before reset')
        self.assertEqual(overall_projection(results).natural_survival,'FAIL')

    def test_unknown_range_after_reset_point_withholds_overall_range(self):
        rows=history()
        p=forecast(rows)
        known=replace(p,wall_hours_to_empty=10,wall_range=(9,11),hours_until_reset=20)
        unknown=replace(p,wall_hours_to_empty=5,wall_range=None,hours_until_reset=4)
        o=overall_projection([(rows[-1],known),(replace(rows[-1],window='secondary'),unknown)])
        self.assertEqual(o.wall_hours_to_empty,10)
        self.assertIsNone(o.wall_range)
        self.assertEqual(o.natural_survival,'FAIL')


if __name__=='__main__':unittest.main()
