"""Read-only, chronological backtest: python -m codex_usage.replay --db PATH."""
import argparse
import bisect
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
import statistics
from .estimator import EstimatorConfig, _epochs, _measure, _duty_cycle, estimate
from .models import UsageSample
from .storage import DEFAULT_DB


def _score(errors):
    return {'count': len(errors),
            'mae_pp': statistics.mean(abs(e) for e in errors) if errors else None,
            'bias_pp': statistics.mean(errors) if errors else None}


def replay(samples, *, horizon_minutes=60, step_minutes=60, warmup_minutes=120):
    """Rates use only each origin's prefix; future observations are scoring labels.

    Conditional-active errors isolate burn intensity using actual future active
    time (oracle activity, NOT a wall prediction). Wall errors use duty at origin.
    Score only future windows with >=80% observed coverage; exclude boundary deltas.
    """
    if any(not math.isfinite(x) or x <= 0 for x in (horizon_minutes, step_minutes, warmup_minutes)):
        raise ValueError('Replay durations must be positive and finite')
    if not samples:
        return {'samples': 0, 'origins': [], 'scores': {}, 'paired_vs_sustained': {}, 'skipped_origins': 0}
    config = EstimatorConfig()
    epochs = _epochs(samples, config)  # Validate ordering and single-window input.
    times = [s.timestamp for s in samples]
    target = times[0] + warmup_minutes * 60
    origins = []
    skipped = 0
    while target + horizon_minutes * 60 <= times[-1]:
        i = bisect.bisect_right(times, target) - 1
        j = bisect.bisect_right(times, times[i] + horizon_minutes * 60) - 1
        prefix = samples[:i+1]
        p = estimate(prefix)
        rates = {'intensity': p.smoothed_rate, '30m': p.rates[30], '60m': p.rates[60],
                 'session': p.session_rate, 'sustained': p.sustained_rate,
                 'anchored_30m': p.sustained_rates.get(30), 'anchored_60m': p.sustained_rates.get(60)}
        duty = _duty_cycle(_epochs(prefix, config), config, times[i])[0]
        future_segments = []
        # Include past context to classify the first future intervals' idle grace.
        for epoch in _epochs(samples[:j+1], config):
            future_segments.extend(s for s in _measure(epoch, config)[0] if s[0] >= times[i])
        observed = sum(s[1]-s[0] for s in future_segments)
        active = sum(s[3] for s in future_segments)
        actual = sum(s[2] for s in future_segments)
        crosses_reset = any(b.used_percent < a.used_percent or b.window_minutes != a.window_minutes
                            for a, b in zip(samples[i:j], samples[i+1:j+1]))
        if (target-times[i] > config.max_gap_seconds or observed < .8*horizon_minutes*60
                or crosses_reset):
            skipped += 1
        else:
            predictions = {name: {'conditional_active': value*active/3600,
                                  'wall': value*duty*observed/3600 if duty is not None else None}
                           for name, value in rates.items() if value is not None}
            origins.append({'timestamp': times[i], 'future_end': times[j],
                            'observed_seconds': observed, 'active_seconds': active,
                            'actual_consumed_pp': actual, 'rates': rates, 'duty_cycle': duty,
                            'predicted_pp': predictions})
        target += step_minutes*60
    names = ('intensity', '30m', '60m', 'session', 'sustained', 'anchored_30m', 'anchored_60m')
    def score_for(name, metric, paired=False):
        return _score([o['predicted_pp'][name][metric]-o['actual_consumed_pp'] for o in origins
                       if name in o['predicted_pp'] and o['predicted_pp'][name][metric] is not None
                       and (not paired or ('sustained' in o['predicted_pp']
                                           and o['predicted_pp']['sustained'][metric] is not None))])
    scores = {name: {metric: score_for(name, metric) for metric in ('conditional_active', 'wall')}
              for name in names}
    pairs = {}
    for name in names:
        if name == 'sustained':
            continue
        paired = {}
        for metric in ('conditional_active', 'wall'):
            subset = [o for o in origins if name in o['predicted_pp'] and 'sustained' in o['predicted_pp']
                      and o['predicted_pp'][name][metric] is not None
                      and o['predicted_pp']['sustained'][metric] is not None]
            paired[metric] = {n: _score([o['predicted_pp'][n][metric]-o['actual_consumed_pp'] for o in subset])
                              for n in (name, 'sustained')}
        pairs[name] = paired
    events = [e for epoch in epochs for e in _measure(epoch, config)[1]]
    current = estimate(samples)
    duty = current.duty_cycle
    old_wall_rate = current.smoothed_rate*duty if current.smoothed_rate is not None and duty is not None else None
    return {'samples': len(samples), 'first_timestamp': times[0], 'last_timestamp': times[-1],
            'observation_span_hours': (times[-1]-times[0])/3600, 'epochs': len(epochs),
            'positive_events': len(events),
            'event_rate_median': statistics.median(e[1] for e in events) if events else None,
            'event_rate_max': max((e[1] for e in events), default=None),
            'horizon_minutes': horizon_minutes, 'step_minutes': step_minutes,
            'current_sample': asdict(samples[-1]), 'current_projection': asdict(current),
            'burst_only_wall_rate': old_wall_rate,
            'burst_only_wall_hours': samples[-1].remaining_percent/old_wall_rate if old_wall_rate else None,
            'origins': origins, 'scores': scores, 'paired_vs_sustained': pairs, 'skipped_origins': skipped}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(DEFAULT_DB))
    parser.add_argument('--limit', default='codex')
    parser.add_argument('--window', default='primary', choices=['primary', 'secondary'])
    parser.add_argument('--horizon-minutes', type=float, default=60)
    parser.add_argument('--step-minutes', type=float, default=60)
    args = parser.parse_args(argv)
    # URI read-only mode neither creates a missing database nor changes its schema.
    with sqlite3.connect(Path(args.db).expanduser().resolve().as_uri()+'?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = [UsageSample(**dict(r)) for r in connection.execute(
            'SELECT * FROM samples WHERE limit_id=? AND window=? ORDER BY timestamp', (args.limit, args.window))]
    print(json.dumps(replay(rows, horizon_minutes=args.horizon_minutes, step_minutes=args.step_minutes),
                     indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
