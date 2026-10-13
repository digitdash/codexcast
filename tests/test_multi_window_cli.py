import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from codex_usage.models import UsageSample
from codex_usage.estimator import estimate
from codex_usage.monitor import UsageMonitor
from codex_usage.main import main, render
from codex_usage.storage import Store

NOW=1800001800

def reading(i):
    return [UsageSample(NOW+i*30,10+i*.1,window_minutes=300,resets_at=NOW+7200,banked_reset_count=1),
            UsageSample(NOW+i*30,20+i*.2,window='secondary',window_minutes=10080,resets_at=NOW+7200,banked_reset_count=1),
            UsageSample(NOW+i*30,30+i*.3,limit_id='other',window_minutes=777,resets_at=NOW+7200,banked_reset_count=1)]

class SequenceProvider:
    def __init__(self): self.i=0
    def sample(self):
        result=reading(self.i);self.i+=1
        return result
    def close(self): pass

class MultiWindowCliTests(unittest.TestCase):
    def test_monitor_retains_three_independent_histories_and_rates_on_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'db'
            with Store(path) as store:
                monitor=UsageMonitor(SequenceProvider(),store)
                for _ in range(61): results=monitor.tick()
                self.assertEqual(len(store.load()),183)
                self.assertEqual({s.key:round(p.smoothed_rate,3) for s,p in results},
                                 {('codex','primary'):12,('codex','secondary'):24,('other','primary'):36})
            with Store(path) as store:
                self.assertEqual(len(store.load(window='primary',limit_id='other')),61)

    def test_default_watch_displays_every_bucket_and_overall(self):
        with tempfile.TemporaryDirectory() as temp, patch('codex_usage.main.CodexProvider',return_value=SequenceProvider()):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['watch','--once','--db',str(Path(temp)/'db')]),0)
        text=output.getvalue()
        for title in ('codex / primary (300 min','codex / secondary (10080 min','other / primary (777 min','OVERALL'):
            self.assertIn(title,text)
        self.assertEqual(text.count('Banked reset:'),1)
        self.assertEqual(text.count('Sampled:'),1)

    def test_json_keeps_existing_records_and_adds_overall(self):
        with tempfile.TemporaryDirectory() as temp, patch('codex_usage.main.CodexProvider',return_value=SequenceProvider()):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['watch','--once','--json','--db',str(Path(temp)/'db')]),0)
        rows=json.loads(output.getvalue())
        self.assertEqual(len(rows),3)
        for row in rows:
            self.assertIn('sample',row)
            self.assertIn('projection',row)
            self.assertEqual(row['overall']['status'],'COLLECTING DATA')

    def test_filtered_display_cannot_hide_unknown_window_from_overall(self):
        known=[replace(reading(i)[0],timestamp=NOW-1800+i*30) for i in range(61)]
        unknown=reading(0)[2]
        all_results=[(known[-1],estimate(known)),(unknown,estimate([unknown]))]
        text=render(all_results[:1],overall_results=all_results)
        self.assertIn('COLLECTING DATA',text)
        self.assertIn('UNKNOWN',text)
        self.assertNotIn('SAFE THROUGH NEXT RESET',text)
        self.assertIn('2 exposed windows',text)

    def test_render_limiting_window_and_cause(self):
        rows=[replace(reading(0)[0],timestamp=NOW+i*30,used_percent=72+i*.1,resets_at=NOW+6*3600) for i in range(241)]
        text=render([(rows[-1],estimate(rows))])
        self.assertRegex(text,r'Wall limiting window: +codex / primary \(300 min window\)')
        self.assertRegex(text,r'Wall-clock depletion: +~0h 20m')
        self.assertRegex(text,r'Natural reset survival: +UNKNOWN')
        self.assertIn('quota exhaustion',text)
        self.assertIn('YES (1)',text)

if __name__=='__main__': unittest.main()
