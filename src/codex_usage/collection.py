"""Shared collection and offline calculations for the console and local plugin."""
from dataclasses import asdict
import time
from .credits import credit_projection
from .overall import overall_projection
from .tokens import token_projection
from .uncertainty import forecast


def collect(provider, store, reader):
    if getattr(provider, 'supports_token_telemetry', False):
        events, quotas, credits = reader.read(now=time.time())
        store.add_token_events(events)
        store.add_token_quotas(quotas)
        store.add_credits(credits)
    samples = provider.sample()
    store.add(samples)
    now = max((s.timestamp for s in samples), default=time.time())
    credit = getattr(provider, 'credit_balance', None)
    if credit is not None:
        store.add_credits([(now, credit)])
        store.add_account_credits([(now, credit)])
    return samples


def calculate(store, samples, config, *, since=None, usage_mode='auto'):
    def cutoff(horizon):
        return max(horizon, since) if since is not None else horizon
    # An immediate reset can leave no post-cutoff readings until the next timer
    # tick. Keep the real quota snapshot with a single-point, unqualified forecast.
    results = [(s, forecast(store.load(limit_id=s.limit_id, window=s.window, since=since) or [s], config))
               for s in samples]
    now = max((s.timestamp for s in samples), default=time.time())
    events = [e for e in store.load_token_events(since=cutoff(now-86400))
              if since is None or (e.response_started_at is not None and e.response_started_at >= since)]
    tokens = token_projection(events,
                              store.load_token_quotas(since=cutoff(now-21600)) or store.load(since=cutoff(now-21600)),
                              store.load_credits(since=cutoff(now-1800)), now=now,
                              mode='tokens' if usage_mode == 'credits' else usage_mode,
                              idle_minutes=config.idle_minutes)
    credits = credit_projection(events, store.load_account_credits(since=cutoff(now-1800)),
                                now=now, mode=usage_mode, max_gap_seconds=config.max_gap_seconds)
    return results, tokens, credits


def statistics(store, samples, config, *, since=None):
    results, tokens, credits = calculate(store, samples, config, since=since)
    return {'windows': [{'sample': asdict(s), 'projection': asdict(p)} for s, p in results],
            'overall': asdict(overall_projection(results)), 'tokens': tokens, 'credits': credits}
