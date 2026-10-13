"""Read allowlisted response counters and calculate local token burn without guesses."""
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
from .models import UsageSample


@dataclass(frozen=True)
class TokenEvent:
    event_id: str
    timestamp: float
    started_at: float
    tokens: int
    source: str
    model: str | None = None
    service_tier: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    response_started_at: float | None = None

    def __post_init__(self):
        if not self.event_id or self.source not in ('native', 'legacy'):
            raise ValueError('Invalid token event source')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (self.timestamp, self.started_at)) or self.started_at > self.timestamp:
            raise ValueError('Invalid token event time')
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int) or not 0 <= self.tokens <= 2**63-1:
            raise ValueError('Invalid token count')
        if self.response_started_at is not None and (
                isinstance(self.response_started_at,bool) or not isinstance(self.response_started_at,(int,float))
                or not math.isfinite(self.response_started_at) or self.response_started_at > self.started_at):
            raise ValueError('Invalid response boundary')


def balance(value):
    try:
        n = Decimal(str(value))
        return str(n) if n.is_finite() and n >= 0 else None
    except (InvalidOperation, ValueError):
        return None


class TokenReader:
    """Incremental, read-only rollout reader. Never store messages or tool payloads.

    Native response IDs deduplicate copied fork/resume history. Older rollouts use
    positive cumulative counter deltas, explicitly labeled estimated in paid mode.
    Only complete lines advance the cursor; an interrupted write is retried.
    """
    def __init__(self, root=None, *, idle_minutes=5):
        self.root = Path(root) if root is not None else Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'sessions'
        self.grace = idle_minutes*60
        self.files = {}

    def read(self, *, now):
        cutoff = now-86400
        events, quotas, credits = {}, {}, {}
        try:
            paths = self.root.rglob('*.jsonl')
            for path in paths:
                try:
                    stat = path.stat()
                    state = self.files.get(path)
                    if stat.st_mtime < cutoff:
                        self.files.pop(path, None)
                        continue
                    if state is None or state['inode'] != stat.st_ino or stat.st_size < state['offset']:
                        state = {'inode':stat.st_ino,'offset':0,'turns':{},'thread':path.stem,
                                 'counter':None,'native':{},'has_native':False,'provider':'openai',
                                 'legacy':{},'quotas':{},'credits':{},'models':{},'native_turns':{}}
                        self.files[path] = state
                    with path.open('rb') as stream:
                        stream.seek(state['offset'])
                        while True:
                            line = stream.readline()
                            if not line or not line.endswith(b'\n'):
                                break
                            state['offset'] = stream.tell()
                            if not any(marker in line for marker in (b'"token_usage_record"', b'"token_count"', b'"task_started"', b'"session_meta"', b'"turn_context"')):
                                continue
                            try:
                                item = json.loads(line)
                                self._accept(item, state)
                            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                                continue
                    # Native records and legacy notifications describe the same calls.
                    for name in ('native', 'legacy'):
                        state[name] = {k:e for k,e in state[name].items() if e.timestamp >= cutoff}
                    state['quotas'] = {k:s for k,s in state['quotas'].items() if s.timestamp >= cutoff}
                    state['credits'] = {t:v for t,v in state['credits'].items() if t >= cutoff}
                    if state['provider'] != 'openai':
                        continue
                    for key, e in (state['native'] if state['has_native'] else state['legacy']).items():
                        if cutoff <= e.timestamp <= now:
                            previous = events.get(key)
                            if previous is None:
                                events[key] = e
                            else:
                                chosen,other = (e,previous) if e.started_at < previous.started_at else (previous,e)
                                if chosen.model is None and other.model is not None:
                                    chosen = replace(chosen,model=other.model,service_tier=other.service_tier)
                                boundaries = [v for v in (chosen.response_started_at,other.response_started_at) if v is not None]
                                if boundaries:
                                    chosen = replace(chosen,response_started_at=min(boundaries))
                                events[key] = chosen
                    quotas.update({k:s for k,s in state['quotas'].items() if cutoff <= s.timestamp <= now})
                    credits.update({t:v for t,v in state['credits'].items() if cutoff <= t <= now})
                except OSError:
                    continue
        except OSError:
            pass
        return (sorted(events.values(),key=lambda e:(e.timestamp,e.event_id)),
                sorted(quotas.values(),key=lambda s:(s.timestamp,s.key)),sorted(credits.items()))

    def _accept(self, item, state):
        payload = item.get('payload')
        if not isinstance(payload, dict):
            return
        kind = item.get('type')
        if kind == 'session_meta':
            state['thread'] = str(payload.get('id') or state['thread'])
            state['provider'] = payload.get('model_provider', 'openai')
            return
        stamp = datetime.fromisoformat(item['timestamp'].replace('Z','+00:00'))
        if stamp.tzinfo is None:
            return
        timestamp = stamp.timestamp()
        if kind == 'turn_context':
            model = payload.get('model')
            tier = payload.get('service_tier')
            state['models'][payload.get('turn_id')] = (
                model if isinstance(model,str) and len(model)<=120 else None,
                tier if isinstance(tier,str) and len(tier)<=40 else
                None if tier is None else 'unsupported')
            # In real rollouts a turn_context may trail its first usage record.
            # Backfill only that turn's missing model; never reprice known responses.
            model,tier = state['models'][payload.get('turn_id')]
            for key,e in state['native'].items():
                if e.model is None and state['native_turns'].get(key) == payload.get('turn_id'):
                    state['native'][key] = replace(e,model=model,service_tier=tier)
        elif kind == 'event_msg' and payload.get('type') == 'task_started':
            state['turns'][payload.get('turn_id')] = timestamp
        elif kind == 'token_usage_record':
            turn = payload.get('turn_id')
            end = timestamp
            start = max(state['turns'].get(turn,end),end-self.grace)
            boundary = state['turns'].get(turn)
            response = payload.get('response_id')
            if not isinstance(response,str) or not response:
                return
            key = hashlib.sha256(response.encode()).hexdigest()
            usage = payload.get('usage') or {}
            if key not in state['native']:
                model,tier = state['models'].get(turn,(None,None))
                counters = [usage.get(k) for k in ('input_tokens','cached_input_tokens','output_tokens','cache_write_input_tokens')]
                # Missing cache-write counters predate that optional protocol field.
                if counters[3] is None:
                    counters[3] = 0
                counters = [v if isinstance(v,int) and not isinstance(v,bool) and 0<=v<=2**63-1 else None for v in counters]
                state['native'][key] = TokenEvent(key,end,start,usage['total_tokens'],'native',model,tier,*counters,response_started_at=boundary)
                state['native_turns'][key] = turn
                state['turns'][turn] = end
            state['has_native'] = True
        elif kind == 'event_msg' and payload.get('type') == 'token_count':
            info = payload.get('info')
            if isinstance(info,dict):
                total = (info.get('total_token_usage') or {}).get('total_tokens')
                if isinstance(total,int) and not isinstance(total,bool) and total >= 0:
                    previous = state['counter']
                    if previous is not None and total > previous[1]:
                        key = hashlib.sha256(f'{state["thread"]}/{timestamp}/{total}'.encode()).hexdigest()
                        state['legacy'][key] = TokenEvent(key,timestamp,max(previous[0],timestamp-self.grace),total-previous[1],'legacy',response_started_at=previous[0])
                    if previous is None or total != previous[1]:
                        state['counter'] = (timestamp,total)
            limits = payload.get('rate_limits')
            if not isinstance(limits,dict):
                return
            credit_data = limits.get('credits') or {}
            credit = balance(credit_data.get('balance')) if isinstance(credit_data,dict) else None
            if credit is not None and (limits.get('limit_id') or 'codex') == 'codex':
                state['credits'][timestamp] = credit
            limit_id = limits.get('limit_id') or 'codex'
            for window in ('primary','secondary'):
                v = limits.get(window)
                if isinstance(v,dict):
                    s = UsageSample(timestamp,v['used_percent'],limit_id,window,
                                    v.get('window_minutes'),v.get('resets_at'))
                    state['quotas'][(s.key,s.timestamp)] = s


def _active_seconds(events, *, cutoff=-math.inf):
    intervals = sorted((max(e.started_at,cutoff),e.timestamp) for e in events if e.timestamp > max(e.started_at,cutoff))
    total = 0; end = -math.inf
    for start, stop in intervals:
        total += max(0,stop-max(start,end))
        end = max(end,stop)
    return total


def token_projection(events, quotas, credits, *, now, mode='auto', quota_rate=None, idle_minutes=5):
    """Six-hour local average; quota conversion from matched observations, never prices.

    Cached input is already in total_tokens; reasoning is already in output. Neither
    is added twice. Concurrent responses sum tokens but share elapsed active time.
    """
    events = sorted({e.event_id:e for e in events if e.timestamp <= now}.values(),key=lambda e:e.timestamp)
    recent = [e for e in events if e.started_at >= now-6*3600 and e.timestamp > e.started_at]
    seconds = _active_seconds(recent)
    total = sum(e.tokens for e in recent)
    credits = sorted((t,v) for t,v in credits if now-1800 <= t <= now and balance(v) is not None)
    spending = any(Decimal(b[1]) < Decimal(a[1]) for a,b in zip(credits,credits[1:]))
    if mode == 'auto':
        mode = 'tokens' if spending else 'quota'
    p = {'mode':mode,'basis':'collecting','tokens_per_active_hour':None,
         'total_tokens':total,'active_seconds':seconds,'observations':len(recent),
         'tokens_per_percentage_point':None,'calibration_observations':0,
         'scope':'local Codex responses; last 6h; active time inferred','credit_balance':credits[-1][1] if credits else None}
    if mode == 'tokens':
        if seconds > 0:
            p.update(basis='measured' if all(e.source == 'native' for e in recent) else 'estimated',
                     tokens_per_active_hour=total/seconds*3600)
        return p
    # Pick one quota series. A token count is never summed separately for each window.
    groups = {}
    # Local counters have no reliable bucket attribution. A separate bucket
    # consuming quota prevents a defensible conversion for ordinary Codex.
    other = {}
    for s in quotas:
        if now-6*3600 <= s.timestamp <= now and s.limit_id != 'codex':
            other.setdefault(s.key, []).append(s)
    if any(any(b.used_percent > a.used_percent for a,b in zip(sorted(rows,key=lambda s:s.timestamp), sorted(rows,key=lambda s:s.timestamp)[1:]))
           for rows in other.values()):
        return p
    for s in quotas:
        if now-6*3600 <= s.timestamp <= now and s.limit_id == 'codex':
            groups.setdefault(s.key,[]).append(s)
    ratios=[]; consumed=active=0
    events = [e for e in recent]
    times=[e.timestamp for e in events]
    prefix=[0]
    for e in events:prefix.append(prefix[-1]+e.tokens)
    for rows in groups.values():
        rows=sorted({s.timestamp:s for s in rows}.values(),key=lambda s:s.timestamp)
        candidates=[]; amount=work=0
        anchor=rows[0] if rows else None
        for a,b in zip(rows,rows[1:]):
            dt=b.timestamp-a.timestamp
            changed_reset=(a.resets_at is None or b.resets_at is None or abs(b.resets_at-a.resets_at)>2)
            if b.used_percent < a.used_percent or changed_reset or b.window_minutes != a.window_minutes:
                candidates=[]; amount=work=0
                anchor=b
                continue
            if dt > idle_minutes*60 or b.remaining_percent == 0:
                anchor=b
                continue
            if b.used_percent <= anchor.used_percent:
                continue
            left=bisect_right(times,anchor.timestamp);right=bisect_right(times,b.timestamp)
            observed_tokens=prefix[right]-prefix[left]
            exposure=_active_seconds(events[left:right],cutoff=anchor.timestamp)
            delta=b.used_percent-anchor.used_percent
            if observed_tokens > 0 and exposure > 0:
                candidates.append(observed_tokens/delta)
                amount+=delta;work+=exposure
            anchor=b
        if len(candidates)>len(ratios):
            ratios=candidates[-64:];consumed=amount;active=work
    p['calibration_observations']=len(ratios)
    if len(ratios) >= 3 and seconds > 0:
        conversion=statistics.median(ratios)
        rate=quota_rate if quota_rate is not None else consumed/active*3600
        p.update(basis='estimated',tokens_per_active_hour=conversion*rate,
                 tokens_per_percentage_point=conversion)
    return p
