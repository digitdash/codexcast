"""Codex credit equivalents and observed balance burn, never API-dollar prices."""
from datetime import datetime, timezone
from decimal import Decimal
import math
from .tokens import balance

RATE_CARD_URL = 'https://help.openai.com/en/articles/11481834-chatgpt-rate-card-business-enterpriseedu-credit-based-pricing'
RATE_CARD_DATE = '2026-10-03'
INDICATIVE_USD_PER_CREDIT = Decimal('0.04')
# Credits per million uncached input, cached input and output tokens (Standard).
RATES = {
    'gpt-6-astra': ('250','25','1250'),
    'gpt-6.1-sol': ('50','2.5','250'),
    'gpt-6-sol': ('50','5','250'),
    'gpt-6-luna': ('2.5','0.25','12.5'),
    'gpt-5.6-sol': ('100','10','500'),
    'gpt-5.6-terra': ('50','5','300'),
    'gpt-5.6-luna': ('5','0.5','30'),
    'gpt-5.5': ('125','12.5','750'),
    'gpt-5.3-codex': ('43.75','4.375','350'),
    'gpt-5.2': ('43.75','4.375','350'),
}
FAST_MODELS = {'gpt-6-astra','gpt-6.1-sol','gpt-6-sol','gpt-6-luna'}
PROMOTION_END = datetime(2026,11,22,tzinfo=timezone.utc).timestamp()


def response_credits(event, *, now):
    """Missing tier means Standard equivalent, not proof of actual charged speed."""
    counts = (event.input_tokens,event.cached_input_tokens,event.output_tokens,event.cache_write_input_tokens)
    rates = RATES.get(event.model)
    if event.source != 'native' or rates is None or any(
            isinstance(n,bool) or not isinstance(n,int) or n<0 or n>2**63-1 for n in counts):
        return None
    input_count,cached,output,writes = counts
    if cached+writes > input_count or input_count+output != event.tokens:
        return None
    # This promotional rate is guaranteed only through Nov 21. Do not silently
    # carry it forward as current pricing after that date.
    if event.model == 'gpt-5.6-sol' and now >= PROMOTION_END:
        return None
    tier = event.service_tier
    multiplier = 1
    if tier in ('fast','priority') and event.model in FAST_MODELS:
        multiplier = 2
    elif tier == 'ultrafast' and event.model == 'gpt-6-astra':
        multiplier = 6
    elif tier not in (None,'default','standard'):
        return None
    uncached_rate,cached_rate,output_rate = map(Decimal,rates)
    return ((input_count-cached-writes)*uncached_rate+cached*cached_rate+output*output_rate)*multiplier/Decimal(1000000)


def active_between(events, start, stop):
    intervals=sorted((max(e.started_at,start),min(e.timestamp,stop)) for e in events
                     if min(e.timestamp,stop)>max(e.started_at,start))
    total=0;end=-math.inf
    for left,right in intervals:
        total+=max(0,right-max(left,end));end=max(end,right)
    return total


def credit_projection(events, readings, *, now, mode='auto', max_gap_seconds=120):
    events=sorted({e.event_id:e for e in events if now-21600<=e.started_at<e.timestamp<=now}.values(),key=lambda e:e.timestamp)
    readings=sorted({t:balance(v) for t,v in readings if now-1800<=t<=now and balance(v) is not None}.items())
    spending=any(Decimal(b[1])<Decimal(a[1]) for a,b in zip(readings,readings[1:]))
    if mode=='auto':
        mode='credits' if spending else 'quota'
    if mode=='tokens': # Compatibility alias for the old forced paid display.
        mode='credits'
    p={'mode':mode,'basis':'collecting','credits_per_active_hour':None,'total_credits':None,
       'active_seconds':0,'observations':len(events),'unpriced_responses':0,
       'balance_intervals':0,'skipped_balance_intervals':0,
       'credit_balance':readings[-1][1] if readings else None,
       'scope':'local Codex responses; last 6h; inferred active time; credit equivalent, not allowance balance',
       'rate_card_date':RATE_CARD_DATE,'rate_card_url':RATE_CARD_URL,
       'reason':'waiting for a completed response with model, counters and activity timing'}
    if mode=='credits':
        p['scope']='account balance consumption / local inferred active time; matched polls in last 30m; other devices/features may contribute'
        total=Decimal(0);seconds=0;unmatched_spending=False
        for (start,a),(stop,b) in zip(readings,readings[1:]):
            delta=Decimal(a)-Decimal(b)
            if stop-start>max_gap_seconds or delta<0:
                p['skipped_balance_intervals']+=1
                continue
            exposure=active_between(events,start,stop)
            if delta>0 and exposure==0:
                unmatched_spending=True
                continue
            total+=delta;seconds+=exposure;p['balance_intervals']+=1
        p.update(total_credits=float(total),active_seconds=seconds,
                 reason='waiting for credit spending and matching local activity between consecutive account polls')
        if total>0 and seconds>0 and not unmatched_spending:
            p.update(basis='observed balance consumption',credits_per_active_hour=float(total/Decimal(str(seconds))*3600),reason=None)
        return p
    costs=[response_credits(e,now=now) for e in events]
    p['unpriced_responses']=sum(c is None for c in costs)
    seconds=active_between(events,now-21600,now)
    p['active_seconds']=seconds
    if p['unpriced_responses']:
        p['reason']='unsupported model/speed or incomplete response counters; credit estimate withheld'
    elif events and seconds>0:
        total=sum(costs,Decimal(0))
        p.update(total_credits=float(total),credits_per_active_hour=float(total/Decimal(str(seconds))*3600),
                 basis='estimated Standard-rate equivalent' if any(e.service_tier is None for e in events) else 'estimated from published credit rates',reason=None)
    return p
