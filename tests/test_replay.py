import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from codex_usage.storage import Store
from test_estimator import samples
try:
    from codex_usage.replay import replay
except ImportError:
    replay=None


class ReplayTests(unittest.TestCase):
    def test_constant_rate_backtest_has_zero_error(self):
        self.assertIsNotNone(replay,'Replay capability not implemented')
        result=replay(samples(480,rate=3,reset_hours=24))
        self.assertEqual(len(result['origins']),6)
        for metric in ('conditional_active','wall'):
            self.assertAlmostEqual(result['scores']['sustained'][metric]['mae_pp'],0)

    def test_forecast_at_origin_does_not_read_future_samples(self):
        self.assertIsNotNone(replay,'Replay capability not implemented')
        rows=samples(480,rate=3,reset_hours=24)
        modified=[replace(s,used_percent=s.used_percent+(2 if i>240 else 0))
                  for i,s in enumerate(rows)]
        a,b=replay(rows)['origins'][0],replay(modified)['origins'][0]
        self.assertEqual(a['rates'],b['rates'])
        self.assertEqual(a['duty_cycle'],b['duty_cycle'])
        self.assertAlmostEqual(b['actual_consumed_pp']-a['actual_consumed_pp'],2)

    def test_missing_future_observation_is_not_scored_as_idle(self):
        self.assertIsNotNone(replay,'Replay capability not implemented')
        rows=samples(480,rate=3,reset_hours=24)
        origin=rows[0].timestamp+7200
        rows=[s for s in rows if not origin+600<s.timestamp<origin+3500]
        result=replay(rows)
        self.assertNotIn(origin,[r['timestamp'] for r in result['origins']])
        self.assertGreater(result['skipped_origins'],0)

    def test_actual_future_quota_reset_is_not_scored_as_consumption(self):
        self.assertIsNotNone(replay,'Replay capability not implemented')
        rows=samples(480,rate=3,reset_hours=24)
        origin=rows[240].timestamp
        rows=[replace(s,used_percent=s.used_percent-16) if i>240 else s for i,s in enumerate(rows)]
        result=replay(rows)
        self.assertNotIn(origin,[r['timestamp'] for r in result['origins']])
        self.assertGreater(result['skipped_origins'],0)

    def test_replay_cli_reads_database_without_mutation(self):
        self.assertIsNotNone(replay,'Replay capability not implemented')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'history.sqlite3'
            with Store(path) as st:st.add(samples(480,rate=3,reset_hours=24))
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            result=subprocess.run([sys.executable,'-m','codex_usage.replay','--db',str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['samples'],961)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

if __name__=='__main__':unittest.main()
