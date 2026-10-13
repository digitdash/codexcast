import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from codex_usage.estimator import estimate
from codex_usage.main import main, render
from codex_usage.overall import overall_projection
from codex_usage.storage import Store
from codex_usage.monitor import has_recent_usage
from test_duty_cycle import trace


class WallForecastCliTests(unittest.TestCase):
    def test_short_active_history_cannot_create_wall_failure(self):
        rows=trace(90,60,60,remaining=50,rate=26.6,reset_in_hours=142)
        results=[(rows[-1],estimate(rows))]
        o=overall_projection(results)
        self.assertAlmostEqual(o.active_hours_to_empty,50/26.6)
        self.assertIsNone(o.wall_hours_to_empty)
        self.assertEqual(o.natural_survival,'UNKNOWN')
        text=render(results)
        self.assertIn('Active capacity remaining:',text)
        self.assertIn('Wall-clock depletion:',text)
        self.assertNotIn('Estimated empty:',text)
        self.assertNotIn('Estimated death:',text)
        self.assertNotIn('FAIL',text)
        self.assertIn('Active confidence:',text)
        self.assertIn('Wall-clock confidence:',text)

    def test_active_and_wall_limiting_windows_can_differ(self):
        low=trace(361,10,60,remaining=12,reset_in_hours=10)
        high=[replace(s,window='secondary') for s in trace(361,60,60,remaining=24,reset_in_hours=10)]
        o=overall_projection([(low[-1],estimate(low)),(high[-1],estimate(high))])
        self.assertEqual(o.active_limiting_window,('codex','primary'))
        self.assertEqual(o.limiting_window,('codex','secondary'))
        self.assertAlmostEqual(o.active_hours_to_empty,1.5)
        self.assertAlmostEqual(o.wall_hours_to_empty,2)

    def test_negative_reset_projections_are_never_rendered(self):
        rows=trace(240,40,60,remaining=10,reset_in_hours=142)
        text=render([(rows[-1],estimate(rows))])
        self.assertRegex(text,r'Projected at reset: +EXHAUSTED')
        self.assertNotRegex(text,r'Projected at reset: +-')
        self.assertIn('BEFORE RESET',text)

    def test_hidden_spark_history_can_establish_wall_limiting_window(self):
        spark=[replace(s,limit_id='codex_bengalfox',limit_name='GPT-5.3-Codex-Spark')
               for s in trace(240,90,240,remaining=1,reset_in_hours=10)]
        codex=trace(240,60,60,remaining=50,rate=3,reset_in_hours=1)
        class Provider:
            def sample(self): return [codex[-1],spark[-1]]
            def close(self): pass
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'db'
            with Store(path) as store:
                store.add(spark[:-1]+codex[:-1])
                self.assertFalse(has_recent_usage(store,'codex_bengalfox',spark[-1].timestamp))
            out=io.StringIO()
            with patch('codex_usage.main.CodexProvider',return_value=Provider()),contextlib.redirect_stdout(out):
                self.assertEqual(main(['watch','--once','--db',str(path)]),0)
            with Store(path) as store:
                self.assertEqual(len(store.load()),len(spark)+len(codex))
            text=out.getvalue()
            self.assertNotIn('\nGPT-5.3-Codex-Spark / primary',text)
            self.assertRegex(text,r'Wall limiting window: +GPT-5.3-Codex-Spark')
            self.assertRegex(text,r'Natural reset survival: +UNKNOWN')
            self.assertRegex(text,r'Reset race: +UNCERTAIN')

if __name__=='__main__': unittest.main()
