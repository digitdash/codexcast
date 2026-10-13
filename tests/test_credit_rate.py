import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from codex_usage.tokens import TokenReader, TokenEvent
from codex_usage.storage import Store
from codex_usage.main import render, main
from codex_usage.models import UsageSample
from codex_usage.estimator import estimate
from test_tokens import BASE, row, record
try:
    from codex_usage.credits import credit_projection
except ImportError:
    credit_projection = None


class CreditTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(credit_projection, 'Credit calculation is missing')

    def read(self, text, now=BASE+60):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.jsonl';p.write_text(text);os.utime(p,(now,now))
            return TokenReader(d).read(now=now)[0]

    def native(self, tier=None, model='gpt-6.1-sol'):
        return self.read(row(0,'event_msg',{'type':'task_started','turn_id':'turn'})+
                         row(0,'turn_context',{'turn_id':'turn','model':model,'service_tier':tier})+
                         record(60,'r1'))

    def test_cache_priced_separately_without_waiting_for_quota(self):
        p=credit_projection(self.native(),[],now=BASE+60)
        # 400 uncached * 50 + 500 cached * 2.5 + 100 output * 250 per million.
        self.assertAlmostEqual(p['credits_per_active_hour'],2.775)
        self.assertAlmostEqual(p['total_credits'],0.04625)
        self.assertEqual(p['basis'],'estimated Standard-rate equivalent')

    def test_context_arriving_after_response_backfills_its_model(self):
        text=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})+record(60,'r1')
        text+=row(61,'turn_context',{'turn_id':'turn','model':'gpt-6.1-sol'})
        p=credit_projection(self.read(text,now=BASE+61),[],now=BASE+61)
        self.assertIsNotNone(p['credits_per_active_hour'])
        self.assertAlmostEqual(p['credits_per_active_hour'],2.775)

    def test_model_and_speed_follow_same_turn_not_most_recent_context(self):
        text=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})
        text+=row(0,'turn_context',{'turn_id':'turn','model':'gpt-6.1-sol','service_tier':'fast'})
        text+=row(1,'turn_context',{'turn_id':'other','model':'gpt-6-astra'})+record(60,'r1')
        p=credit_projection(self.read(text),[],now=BASE+60)
        self.assertAlmostEqual(p['credits_per_active_hour'],5.55)
        self.assertEqual(p['basis'],'estimated from published credit rates')

    def test_unknown_model_or_speed_does_not_invent_price(self):
        for model,tier in [('unknown',None),('gpt-6.1-sol','flex')]:
            p=credit_projection(self.native(tier,model),[],now=BASE+60)
            self.assertIsNone(p['credits_per_active_hour'])
            self.assertEqual(p['unpriced_responses'],1)

    def test_incomplete_breakdown_and_unknown_start_do_not_create_rate(self):
        for e in [TokenEvent('a',BASE+60,BASE,1000,'legacy'),
                  TokenEvent('a',BASE+60,BASE+60,1000,'native')]:
            p=credit_projection([e],[],now=BASE+60)
            self.assertIsNone(p['credits_per_active_hour'])

    def test_concurrent_responses_sum_credits_and_merge_active_time(self):
        from dataclasses import replace
        e=self.native()[0]
        p=credit_projection([e,replace(e,event_id='other')],[],now=BASE+60)
        self.assertAlmostEqual(p['credits_per_active_hour'],5.55)
        self.assertEqual(p['active_seconds'],60)

    def test_paid_mode_uses_actual_balance_units_not_tokens(self):
        p=credit_projection(self.native(),[(BASE,'100.25'),(BASE+60,'99.25')],now=BASE+60)
        self.assertEqual(p['mode'],'credits')
        self.assertEqual(p['basis'],'observed balance consumption')
        self.assertEqual(p['credits_per_active_hour'],60)
        self.assertEqual(p['total_credits'],1)

    def test_available_credits_and_topups_do_not_imply_spending(self):
        for ending in ['100','200']:
            p=credit_projection(self.native(),[(BASE,'100'),(BASE+60,ending)],now=BASE+60)
            self.assertEqual(p['mode'],'quota')
            self.assertAlmostEqual(p['credits_per_active_hour'],2.775)

    def test_paid_denominator_clips_to_polled_interval(self):
        p=credit_projection(self.native(),[(BASE+30,'100'),(BASE+60,'99')],now=BASE+60)
        self.assertEqual(p['credits_per_active_hour'],120)
        self.assertEqual(p['active_seconds'],30)

    def test_paid_gap_or_absent_activity_withholds_rate(self):
        for events,reads in [(self.native(),[(BASE-300,'100'),(BASE+60,'99')]),
                             ([],[(BASE,'100'),(BASE+60,'99')])]:
            p=credit_projection(events,reads,now=BASE+60)
            self.assertIsNone(p['credits_per_active_hour'])
            self.assertEqual(p['mode'],'credits')

    def test_storage_backfills_metadata_without_changing_original_rows(self):
        e=self.native()[0]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            with Store(path) as s:
                s.add([UsageSample(BASE,25)])
                s.add_token_events([TokenEvent(e.event_id,e.timestamp,e.started_at,e.tokens,e.source)])
                s.add_token_events([e]);s.add_token_events([e])
                s.add_account_credits([(BASE,'100'),(BASE+60,'99')])
                s.add_credits([(BASE+30,'200')]) # Delayed log must not affect paid rate.
            with Store(path) as s:
                self.assertEqual(s.load(),[UsageSample(BASE,25)])
                self.assertEqual(len(s.load_token_events()),1)
                p=credit_projection(s.load_token_events(),s.load_account_credits(since=BASE),now=BASE+60)
                self.assertEqual(p['credits_per_active_hour'],60)
                self.assertAlmostEqual(credit_projection(s.load_token_events(),[],now=BASE+60)['credits_per_active_hour'],2.775)

    def test_headline_shows_sustained_credits_and_indicative_usd(self):
        sample=UsageSample(BASE+60,25)
        p=credit_projection(self.native(),[],now=BASE+60)
        text=render([(sample,estimate([sample]))],credits=p)
        self.assertIn('Estimated sustained cost: 2.78 credits/hr (~$0.11/hr)',text)
        self.assertIn('USD basis:               $0.04/credit; indicative',text)
        self.assertIn('Standard-rate equivalent',text)
        self.assertNotIn('Average tokens',text)

    def test_observed_credit_burn_uses_the_same_indicative_usd_conversion(self):
        sample=UsageSample(BASE+60,25)
        p=credit_projection(self.native(),[(BASE,'100.25'),(BASE+60,'99.25')],now=BASE+60)
        text=render([(sample,estimate([sample]))],credits=p)
        self.assertIn('Estimated sustained cost: 60.00 credits/hr (~$2.40/hr)',text)
        self.assertIn('observed balance consumption',text)

    def test_no_usd_amount_is_shown_while_credit_rate_is_collecting(self):
        sample=UsageSample(BASE+60,25)
        p=credit_projection([],[],now=BASE+60)
        text=render([(sample,estimate([sample]))],credits=p)
        self.assertIn('Estimated sustained cost: collecting data...',text)
        self.assertNotIn('~$',text)

    def test_watch_json_adds_credits_and_retains_token_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.jsonl'
            p.write_text(row(0,'event_msg',{'type':'task_started','turn_id':'turn'})+
                         row(0,'turn_context',{'turn_id':'turn','model':'gpt-6.1-sol'})+record(60,'r1'))
            os.utime(p,(BASE+60,BASE+60))
            class Provider:
                supports_token_telemetry=True
                credit_balance='100'
                def sample(self):return [UsageSample(BASE+60,25)]
                def close(self):pass
            output=io.StringIO()
            with patch('codex_usage.main.CodexProvider',return_value=Provider()),patch('codex_usage.main.time.time',return_value=BASE+60),contextlib.redirect_stdout(output):
                self.assertEqual(main(['watch','--once','--json','--db',str(Path(d)/'db'),'--sessions-dir',d]),0)
            result=json.loads(output.getvalue())[0]
            self.assertIn('tokens',result)
            self.assertAlmostEqual(result['credits']['credits_per_active_hour'],2.775)

    def test_cache_writes_and_reasoning_are_not_billed_twice(self):
        from dataclasses import replace
        e=replace(self.native()[0],cache_write_input_tokens=100)
        p=credit_projection([e],[],now=BASE+60)
        self.assertAlmostEqual(p['credits_per_active_hour'],2.475)

    def test_expired_promotional_rate_is_withheld(self):
        p=credit_projection(self.native(model='gpt-5.6-sol'),[],now=BASE+60)
        self.assertIsNone(p['credits_per_active_hour'])

    def test_unpriced_response_does_not_silently_become_partial_average(self):
        from dataclasses import replace
        e=self.native()[0]
        p=credit_projection([e,replace(e,event_id='unknown',model='new-model')],[],now=BASE+60)
        self.assertIsNone(p['credits_per_active_hour'])
        self.assertEqual(p['unpriced_responses'],1)

    def test_paid_without_baseline_collects_instead_of_using_model_price(self):
        p=credit_projection(self.native(),[(BASE+60,'99')],now=BASE+60,mode='credits')
        self.assertIsNone(p['credits_per_active_hour'])
        self.assertEqual(p['basis'],'collecting')

    def test_paid_topup_interval_is_excluded_from_amount_and_time(self):
        from dataclasses import replace
        e=self.native()[0]
        events=[e,replace(e,event_id='second',started_at=BASE+60,timestamp=BASE+120)]
        p=credit_projection(events,[(BASE,'100'),(BASE+60,'200'),(BASE+120,'199')],now=BASE+120,mode='credits')
        self.assertEqual(p['credits_per_active_hour'],60)
        self.assertEqual(p['skipped_balance_intervals'],1)

    def test_duplicate_copy_with_known_model_fills_unknown_copy(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            text=row(0,'event_msg',{'type':'task_started','turn_id':'turn'})
            (root/'a.jsonl').write_text(text+record(60,'r1'))
            (root/'b.jsonl').write_text(text+row(0,'turn_context',{'turn_id':'turn','model':'gpt-6.1-sol'})+record(60,'r1'))
            for path in root.glob('*.jsonl'):os.utime(path,(BASE+60,BASE+60))
            with patch.object(Path,'rglob',return_value=iter([root/'a.jsonl',root/'b.jsonl'])):
                events,_,_=TokenReader(root).read(now=BASE+60)
            self.assertEqual(len(events),1)
            p=credit_projection(events,[],now=BASE+60)
            self.assertIsNotNone(p['credits_per_active_hour'])

    def test_present_malformed_speed_is_not_assumed_standard(self):
        for tier in [7, {'name':'fast'}, 'x'*41]:
            with self.subTest(tier=tier):
                p=credit_projection(self.native(tier),[],now=BASE+60)
                self.assertIsNone(p['credits_per_active_hour'])
                self.assertEqual(p['unpriced_responses'],1)


if __name__=='__main__':unittest.main()
