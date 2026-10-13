import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_usage.models import UsageSample
from codex_usage.estimator import estimate
from codex_usage.storage import Store
from codex_usage.main import main, render
try:
    from codex_usage.tokens import TokenEvent, TokenReader, token_projection
except ImportError:
    TokenEvent = TokenReader = token_projection = None

BASE=1800000000

def row(seconds, kind, payload):
    from datetime import datetime, timezone
    return json.dumps({'timestamp':datetime.fromtimestamp(BASE+seconds,timezone.utc).isoformat(),
                       'type':kind,'payload':payload})+'\n'

def record(seconds, response, total=1000):
    return row(seconds,'token_usage_record',{'thread_id':'thread','turn_id':'turn','response_id':response,
               'usage':{'input_tokens':total-100,'cached_input_tokens':500,'output_tokens':100,
                        'reasoning_output_tokens':20,'total_tokens':total}})


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TokenReader,'Token measurement is not implemented')

    def test_native_records_use_totals_once_across_copied_rollouts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            text=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})+record(60,'r1')+record(120,'r2')
            (root/'a.jsonl').write_text(text)
            (root/'copy.jsonl').write_text(text)
            for path in root.glob('*.jsonl'):os.utime(path,(BASE+120,BASE+120))
            events, quotas, credits=TokenReader(root).read(now=BASE+120)
            self.assertEqual(sum(e.tokens for e in events),2000)
            p=token_projection(events,[],[],now=BASE+120,mode='tokens')
            self.assertEqual(p['basis'],'measured')
            self.assertAlmostEqual(p['tokens_per_active_hour'],60000)
            self.assertEqual(p['total_tokens'],2000)

    def test_concurrent_tasks_merge_active_time_without_double_counting(self):
        events=[TokenEvent('a',BASE+60,BASE,1000,'native'),TokenEvent('b',BASE+60,BASE,2000,'native')]
        p=token_projection(events,[],[],now=BASE+60,mode='tokens')
        self.assertAlmostEqual(p['active_seconds'],60)
        self.assertAlmostEqual(p['tokens_per_active_hour'],180000)

    def test_long_idle_gaps_do_not_dilute_average(self):
        events=[TokenEvent('a',BASE+60,BASE,1000,'native'),
                TokenEvent('b',BASE+7260,BASE+7200,1000,'native')]
        p=token_projection(events,[],[],now=BASE+9000,mode='tokens')
        self.assertAlmostEqual(p['active_seconds'],120)
        self.assertAlmostEqual(p['tokens_per_active_hour'],60000)

    def test_quota_estimate_uses_observed_conversion(self):
        events=[TokenEvent(str(i),BASE+i*60,BASE+(i-1)*60,1000,'native') for i in range(1,7)]
        quotas=[UsageSample(BASE+i*60,i,window_minutes=300,resets_at=BASE+18000) for i in range(7)]
        p=token_projection(events,quotas,[],now=BASE+360,mode='quota',quota_rate=12)
        self.assertEqual(p['basis'],'estimated')
        self.assertAlmostEqual(p['tokens_per_active_hour'],12000)
        self.assertAlmostEqual(p['tokens_per_percentage_point'],1000)

    def test_credit_decrease_switches_to_actual_tokens(self):
        events=[TokenEvent('a',BASE+60,BASE,1000,'native')]
        p=token_projection(events,[],[(BASE,'100'),(BASE+60,'99')],now=BASE+60)
        self.assertEqual(p['mode'],'tokens')
        self.assertEqual(p['basis'],'measured')
        self.assertAlmostEqual(p['tokens_per_active_hour'],60000)
        available=token_projection(events,[],[(BASE,'100'),(BASE+60,'100')],now=BASE+60)
        self.assertNotEqual(available['mode'],'tokens')

    def test_refill_gap_and_missing_evidence_do_not_fabricate_conversion(self):
        events=[TokenEvent('a',BASE+60,BASE,1000,'native')]
        quotas=[UsageSample(BASE,90,window_minutes=300,resets_at=BASE+10000),
                UsageSample(BASE+60,0,window_minutes=300,resets_at=BASE+20000)]
        p=token_projection(events,quotas,[],now=BASE+60,mode='quota',quota_rate=12)
        self.assertIsNone(p['tokens_per_active_hour'])
        self.assertEqual(p['basis'],'collecting')

    def test_incremental_reader_waits_for_complete_line_and_ignores_future(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.jsonl'
            text=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})+record(60,'r1')
            path.write_text(text+record(120,'r2')[:-1])
            os.utime(path,(BASE+120,BASE+120))
            reader=TokenReader(Path(d))
            self.assertEqual(len(reader.read(now=BASE+90)[0]),1)
            with path.open('a') as f:f.write('\n')
            os.utime(path,(BASE+120,BASE+120))
            self.assertEqual(len(reader.read(now=BASE+120)[0]),2)
            self.assertEqual(len(reader.read(now=BASE+120)[0]),2)

    def test_store_token_records_survive_restart_without_touching_quota_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            sample=UsageSample(BASE,25)
            events=[TokenEvent('a',BASE+60,BASE,1000,'native')]
            with Store(path) as store:
                store.add([sample]);store.add_token_events(events);store.add_token_events(events)
            with Store(path) as store:
                self.assertEqual(store.load(),[sample])
                self.assertEqual(store.load_token_events(),events)

    def test_native_duplicates_and_legacy_notifications_are_counted_once(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.jsonl'
            text=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})
            text+=record(60,'r1')+record(60,'r1')
            text+=row(60,'event_msg',{'type':'token_count','info':{'total_token_usage':{'total_tokens':1000}}})
            text+=record(120,'r2')
            text+=row(120,'event_msg',{'type':'token_count','info':{'total_token_usage':{'total_tokens':2000}}})
            path.write_text(text);os.utime(path,(BASE+120,BASE+120))
            events,_,_=TokenReader(d).read(now=BASE+120)
            self.assertEqual(len(events),2)
            self.assertEqual(sum(e.tokens for e in events),2000)
            self.assertEqual(sum(e.timestamp-e.started_at for e in events),120)

    def test_malformed_records_do_not_stop_valid_telemetry(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.jsonl'
            text='["token_count"]\n'
            text+=row(0,'event_msg',{'type':'token_count','info':{'total_token_usage':'bad'}})
            text+=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})
            text+=row(20,'token_usage_record',{'turn_id':'turn','response_id':'bad','usage':{'total_tokens':2**64}})
            text+=record(60,'r1')
            path.write_text(text);os.utime(path,(BASE+60,BASE+60))
            events,_,_=TokenReader(d).read(now=BASE+60)
            self.assertEqual(len(events),1)
            self.assertEqual(events[0].tokens,1000)

    def test_non_openai_provider_is_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.jsonl'
            path.write_text(row(0,'session_meta',{'id':'thread','model_provider':'other'})+
                            row(0,'event_msg',{'type':'task_started','turn_id':'turn'})+record(60,'r1'))
            os.utime(path,(BASE+60,BASE+60))
            self.assertEqual(TokenReader(d).read(now=BASE+60),([],[],[]))

    def test_unknown_start_does_not_invent_active_time_or_rate(self):
        p=token_projection([TokenEvent('a',BASE,BASE,1000,'native')],[],[],now=BASE,mode='tokens')
        self.assertIsNone(p['tokens_per_active_hour'])
        self.assertEqual(p['active_seconds'],0)

    def test_legacy_counter_reset_and_duplicate_do_not_count_as_spending(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.jsonl'
            text=''
            for t,total in ((0,100),(30,100),(60,1100),(90,10),(120,1010)):
                text+=row(t,'event_msg',{'type':'token_count','info':{'total_token_usage':{'total_tokens':total}}})
            path.write_text(text);os.utime(path,(BASE+120,BASE+120))
            events,_,_=TokenReader(d).read(now=BASE+120)
            p=token_projection(events,[],[],now=BASE+120,mode='tokens')
            self.assertEqual(p['basis'],'estimated')
            self.assertEqual(p['total_tokens'],2000)
            self.assertEqual(p['active_seconds'],90)

    def test_calibration_does_not_mix_bucket_consumption(self):
        events=[TokenEvent(str(i),BASE+i*60,BASE+(i-1)*60,1000,'native') for i in range(1,7)]
        quotas=[UsageSample(BASE+i*60,i,window_minutes=300,resets_at=BASE+18000) for i in range(7)]
        quotas+=[UsageSample(BASE,0,limit_id='other'),UsageSample(BASE+60,1,limit_id='other')]
        p=token_projection(events,quotas,[],now=BASE+360,mode='quota')
        self.assertIsNone(p['tokens_per_active_hour'])

    def test_calibration_is_invalidated_by_duration_change_and_censoring(self):
        events=[TokenEvent(str(i),BASE+i*60,BASE+(i-1)*60,1000,'native') for i in range(1,7)]
        quotas=[UsageSample(BASE+i*60,96+i,window_minutes=300,resets_at=BASE+18000) for i in range(5)]
        quotas+=[UsageSample(BASE+300,50,window_minutes=10080,resets_at=BASE+18000)]
        p=token_projection(events,quotas,[],now=BASE+360,mode='quota')
        self.assertIsNone(p['tokens_per_active_hour'])
        self.assertEqual(p['calibration_observations'],0)

    def test_log_quota_observations_are_separate_from_forecast_history(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d)/'db') as store:
            samples=[UsageSample(BASE,0),UsageSample(BASE+300,5)]
            logs=[UsageSample(BASE+i*60,i) for i in range(1,5)]
            store.add(samples);store.add_token_quotas(logs);store.add_token_quotas(logs)
            self.assertEqual(store.load(),samples)
            self.assertEqual(store.load_token_quotas(since=BASE),logs)
            self.assertEqual(estimate(store.load()),estimate(samples))

    def test_watch_collects_local_counters_and_retains_them_on_restart(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'a.jsonl';db=root/'db'
            text=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})
            for i in range(7):
                if i:text+=record(i*60,f'r{i}')
                text+=row(i*60,'event_msg',{'type':'token_count','rate_limits':{
                    'limit_id':'codex','primary':{'used_percent':i,'window_minutes':300,'resets_at':BASE+18000},
                    'credits':{'balance':'100' if i<6 else '99'}}})
            path.write_text(text);os.utime(path,(BASE+360,BASE+360))
            class Provider:
                supports_token_telemetry=True
                credit_balance='99'
                # The account poll can be ahead of the delayed log quota snapshot.
                def sample(self):return [UsageSample(BASE+360,7,window_minutes=300,resets_at=BASE+18000)]
                def close(self):pass
            for mode,basis in (('quota','estimated'),('auto','measured')):
                output=io.StringIO()
                with patch('codex_usage.main.CodexProvider',return_value=Provider()), \
                     patch('codex_usage.main.time.time',return_value=BASE+360),contextlib.redirect_stdout(output):
                    self.assertEqual(main(['watch','--once','--json','--db',str(db),'--sessions-dir',d,'--usage-mode',mode]),0)
                result=json.loads(output.getvalue())[0]
                self.assertEqual(result['tokens']['basis'],basis)
                self.assertAlmostEqual(result['tokens']['tokens_per_active_hour'],60000)
                with Store(db) as store:
                    self.assertEqual(len(store.load()),1)
                    self.assertEqual(len(store.load_token_events()),6)
                    self.assertEqual(len(store.load_token_quotas(since=BASE)),7)

    def test_provider_keeps_legacy_fallback_with_malformed_optional_map(self):
        from codex_usage.provider import CodexProvider
        for result in ({'rateLimitsByLimitId':'bad','rateLimits':{'primary':{'usedPercent':25},'credits':{'balance':'99'}}},
                       {'rateLimitsByLimitId':{'other':{'primary':{'usedPercent':25}}},'rateLimits':'bad'}):
            provider=CodexProvider()
            with patch.object(provider,'_start'),patch.object(provider,'_request',return_value=result):
                self.assertEqual(provider.sample()[0].used_percent,25)


class CombinedUiTests(unittest.TestCase):
    def test_default_render_has_one_overall_and_no_duplicate_panel(self):
        s=UsageSample(BASE,40,window_minutes=300,resets_at=BASE+10000)
        text=render([(s,estimate([s]))])
        self.assertEqual(text.count('OVERALL'),1)
        self.assertEqual(text.count('Current state:'),1)
        self.assertEqual(text.count('Usage:'),1)
        self.assertIn('60.0% remaining',text)
        self.assertIn('Estimated sustained cost:',text)

    def test_meter_color_boundaries(self):
        import codex_usage.main as cli
        meter=getattr(cli,'usage_meter',None)
        self.assertIsNotNone(meter,'Colored meter is missing')
        for remaining, code in ((50,'\033[32m'),(49.9,'\033[38;5;208m'),(10,'\033[38;5;208m'),(9.9,'\033[31m')):
            with self.subTest(remaining=remaining):
                value=meter(100-remaining,color=True)
                self.assertIn(code,value)
                self.assertIn('\033[0m',value)
                self.assertNotIn('\033[',meter(100-remaining,color=False))

if __name__=='__main__':unittest.main()
