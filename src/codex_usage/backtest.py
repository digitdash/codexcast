"""Chronological multi-horizon evaluation and empirical, horizon-matched ranges."""
import bisect
import math
import statistics
from .estimator import EstimatorConfig, _epochs, _measure, estimate

HORIZONS = (15, 30, 60, 120, 240, 480, 960)
PREDICTORS = ('15m', '30m', '60m', '120m', 'session', 'sustained', 'intensity')
MIN_EVALUATIONS = 10
MAX_CALIBRATION = 64


def percentile(values, fraction):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)]


def error_metrics(evaluations, name):
    errors, relative = [], []
    for e in evaluations:
        if name not in e['predicted']:
            continue
        predicted = e['predicted'][name]
        error = abs(e['actual']-predicted)
        errors.append(error)
        if predicted > 0:
            relative.append(error/predicted)
    return {'count': len(errors), 'relative_count': len(relative),
            'calibration_count': sum(e['calibration_eligible'] for e in evaluations if name in e['predicted']),
            'median_absolute_error_pp': statistics.median(errors) if errors else None,
            'mean_absolute_error_pp': statistics.mean(errors) if errors else None,
            'p80_absolute_error_pp': percentile(errors, .8),
            'p90_absolute_error_pp': percentile(errors, .9),
            'median_relative_error': statistics.median(relative) if relative else None,
            'p80_relative_error': percentile(relative, .8)}


def backtest(samples, config=None):
    """Each target uses disjoint outcomes; only origin prefixes produce predictions.

    Active endpoints interpolate to exact active exposure. A fractional final poll
    label becomes available only at that poll's timestamp. Wall outcomes measure
    elapsed time and reject <80% observation coverage. Gaps are never idle labels.
    """
    config = config or EstimatorConfig()
    epochs = _epochs(samples, config)
    segments = {b: (a, b, delta, active) for epoch in epochs
                for a, b, delta, active in _measure(epoch, config)[0]}
    active = [0.0]; observed = [0.0]; consumed = [0.0]; changes = [0]; resets = [0]; exhausted = [int(bool(samples and samples[0].remaining_percent == 0))]
    for a, b in zip(samples, samples[1:]):
        segment = segments.get(b.timestamp)
        active.append(active[-1]+(segment[3] if segment else 0))
        observed.append(observed[-1]+(segment[1]-segment[0] if segment else 0))
        consumed.append(consumed[-1]+(segment[2] if segment else 0))
        changes.append(changes[-1]+int(bool(segment and segment[2] > 1e-9)))
        resets.append(resets[-1]+int(b.used_percent < a.used_percent or b.window_minutes != a.window_minutes))
        exhausted.append(exhausted[-1]+int(b.remaining_percent == 0))
    times = [s.timestamp for s in samples]
    forecasts = {}
    def at(index):
        if index not in forecasts:
            # Same bounded point-estimator context as watch, with no future rows.
            p = estimate(samples[max(0, index+1-10000):index+1], config)
            rates = {f'{m}m': p.sustained_rates.get(m) for m in (15, 30, 60, 120)}
            rates.update(session=p.session_rate, sustained=p.sustained_rate, intensity=p.smoothed_rate)
            forecasts[index] = (p, {k: v for k, v in rates.items() if v is not None and v >= 0})
        return forecasts[index]
    evaluations = []
    skipped = 0
    if changes[-1]:
        # Begin at a historically available sustained forecast, not at an empty
        # startup sample that would waste the first entire long target horizon.
        first = next((i for i in range(len(samples)) if active[i] >= 75*60
                      and changes[i] >= 5 and at(i)[0].sustained_rate is not None), len(samples))
        first_wall = next((i for i in range(first, len(samples)) if observed[i] >= 7200
                           and at(i)[0].duty_cycle is not None), len(samples))
        for clock in ('active', 'wall'):
            axis = active if clock == 'active' else times
            for horizon in HORIZONS:
                i = first if clock == 'active' else first_wall
                while i < len(samples)-1:
                    target = axis[i]+horizon*60
                    j = bisect.bisect_left(axis, target, lo=i+1)
                    if j >= len(samples):
                        break
                    dt = times[j]-times[j-1]
                    if clock == 'active':
                        tail_active = target-active[j-1]
                        fraction = tail_active/(active[j]-active[j-1])
                        end = times[j-1]+tail_active
                        tail_observed = tail_active
                    else:
                        fraction = (target-times[j-1])/dt
                        end = target
                        tail_observed = (observed[j]-observed[j-1])*fraction
                        tail_active = min(active[j]-active[j-1], dt*fraction)
                    exposure = observed[j-1]-observed[i]+tail_observed
                    work = active[j-1]-active[i]+tail_active
                    actual = consumed[j-1]-consumed[i]+(consumed[j]-consumed[j-1])*fraction
                    elapsed = end-times[i]
                    p, rates = at(i)
                    if (exposure < .8*elapsed or resets[j] != resets[i] or exhausted[j] != exhausted[i]
                            or samples[i].remaining_percent == 0):
                        skipped += 1
                    else:
                        multiplier = work/3600 if clock == 'active' else (
                            p.duty_cycle*exposure/3600 if p.duty_cycle is not None else None)
                        predicted = {name: rate*multiplier for name, rate in rates.items()} if multiplier is not None else {}
                        if predicted:
                            evaluations.append({'clock': clock, 'horizon_minutes': horizon,
                                                'origin': times[i], 'end': end, 'available_at': times[j],
                                                'active_seconds': work, 'observed_seconds': exposure,
                                                'coverage': exposure/elapsed, 'actual': actual, 'predicted': predicted,
                                                'calibration_eligible': clock == 'active' or exposure >= elapsed-1e-6,
                                                'origin_duty': p.duty_cycle, 'actual_duty': work/exposure,
                                                'usage_changes': changes[j]-changes[i]})
                    # Starts at the last label poll, never overlapping the prior outcome.
                    i = j
    metrics = {}; comparisons = {}
    for clock in ('active', 'wall'):
        for horizon in HORIZONS:
            key = f'{clock}/{horizon}'
            group = [e for e in evaluations if e['clock'] == clock and e['horizon_minutes'] == horizon]
            metrics[key] = {name: error_metrics(group, name) for name in PREDICTORS}
            # Paired comparisons avoid rewarding an estimator for missing hard origins.
            comparisons[key] = {}
            for name in PREDICTORS:
                if name == 'sustained':
                    continue
                paired = [e for e in group if name in e['predicted'] and 'sustained' in e['predicted']]
                comparisons[key][name] = {n: error_metrics(paired, n) for n in (name, 'sustained')}
    return {'samples': len(samples), 'usage_changes': changes[-1],
            'active_minutes': active[-1]/60, 'observed_hours': observed[-1]/3600,
            'evaluations': evaluations, 'metrics': metrics, 'paired_vs_sustained': comparisons,
            'skipped_outcomes': skipped}


def empirical_bounds(point_hours, log_errors):
    """Finite-sample P80 |log(actual/predicted)|; never invent a default width."""
    n = len(log_errors)
    if n < MIN_EVALUATIONS:
        return None
    q = sorted(log_errors)[math.ceil(.8*(n+1))-1]
    if not math.isfinite(q) or q > 700:
        return (0, None)  # Zero future consumption can leave depletion unbounded.
    return (point_hours*math.exp(-q), point_hours*math.exp(q))


def calibrate(evaluations, *, clock, point_hours, now):
    evidence = {'range': None, 'count': 0, 'horizon_minutes': None,
                'median_relative_error': None, 'p80_relative_error': None,
                'usage_changes': 0, 'duty_variability': None, 'supported_horizon_minutes': None}
    groups = {}
    for horizon in HORIZONS:
        groups[horizon] = [e for e in evaluations if e['clock'] == clock
                           and e['horizon_minutes'] == horizon and e['available_at'] <= now
                           and e['predicted'].get('sustained', 0) > 0
                           and e['calibration_eligible']][-MAX_CALIBRATION:]
    supported = [h for h, es in groups.items() if len(es) >= MIN_EVALUATIONS]
    evidence['supported_horizon_minutes'] = max(supported, default=None)
    if point_hours is None:
        return evidence
    eligible = [h for h in HORIZONS if h+1e-6 >= point_hours*60]
    if not eligible:
        return evidence
    horizon = next((h for h in eligible if len(groups[h]) >= MIN_EVALUATIONS), eligible[0])
    records = groups[horizon]
    scores = [abs(math.log(e['actual'])-math.log(e['predicted']['sustained']))
              if e['actual'] > 0 else math.inf for e in records]
    stats = error_metrics(records, 'sustained')
    duties = [e['actual_duty'] for e in records]
    median_duty = statistics.median(duties) if duties else 0
    variability = (statistics.median(abs(v-median_duty) for v in duties)/median_duty
                   if median_duty else None)
    evidence.update(range=empirical_bounds(point_hours, scores), count=len(records), horizon_minutes=horizon,
                    median_relative_error=stats['median_relative_error'], p80_relative_error=stats['p80_relative_error'],
                    usage_changes=sum(e['usage_changes'] for e in records), duty_variability=variability)
    return evidence


def read_history(path, *, limit_id=None, window=None):
    """Do not create, chmod, migrate, or write a database during backtesting."""
    from pathlib import Path
    from contextlib import closing
    import sqlite3
    from .models import UsageSample
    with closing(sqlite3.connect(Path(path).expanduser().resolve().as_uri()+'?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        clauses, values = [], []
        for column, value in (('limit_id', limit_id), ('window', window)):
            if value is not None:
                clauses.append(column+'=?')
                values.append(value)
        query = 'SELECT * FROM samples'+(' WHERE '+' AND '.join(clauses) if clauses else '')
        return [UsageSample(**dict(r)) for r in connection.execute(query+' ORDER BY timestamp', values)]


def render_backtest(sample, report):
    def number(v):
        return f'{v:.3f}' if v is not None else 'unavailable'
    label = ''.join(c for c in str(sample.limit_name or sample.limit_id) if c.isprintable())[:120]
    lines = ['CODEX FORECAST BACKTEST', '',
             f'{label} / {sample.window} ({sample.window_minutes or "?"} min window)', '',
             f'Samples:                  {report["samples"]}',
             f'Observed usage changes:   {report["usage_changes"]}',
             f'Observed activity blocks: {int(report["active_minutes"]//15)} (15 active min)',
             f'Forecast evaluations:     {len(report["evaluations"])} (across clocks/horizons; do not pool counts)',
             'Errors are percentage points. Relative errors divide by the forecast, not actual consumption.',
             'Wall diagnostics with gaps score observed exposure only; only complete wall targets calibrate ranges.']
    for key, methods in report['metrics'].items():
        if not any(m['count'] for m in methods.values()):
            continue
        clock, horizon = key.split('/')
        lines += ['', f'NEXT {horizon} {clock.upper()} MINUTES', '',
                  'Predictor    N  Calib  Median AE   P80 AE    Median relative error']
        for name, stats in methods.items():
            rel = f'{stats["median_relative_error"]:.0%}' if stats['median_relative_error'] is not None else 'unavailable'
            lines.append(f'{name:<10} {stats["count"]:3} {stats["calibration_count"]:6} {number(stats["median_absolute_error_pp"]):>10} {number(stats["p80_absolute_error_pp"]):>10}  {rel}')
        eligible = [n for n, m in methods.items() if m['count'] >= 5 and n != 'intensity']
        common = [e for e in report['evaluations'] if e['clock'] == clock and e['horizon_minutes'] == int(horizon)
                  and all(n in e['predicted'] for n in eligible)]
        if len(eligible) >= 2 and len(common) >= 5:
            ranked = sorted(eligible, key=lambda n: (error_metrics(common, n)['median_absolute_error_pp'],
                                                     error_metrics(common, n)['p80_absolute_error_pp']))
            lines.append(f'Best on {len(common)} common origins: {ranked[0]} (median AE, then P80; not a universal winner)')
        else:
            lines.append('Best predictor: insufficient matched evaluations (five required)')
    return '\n'.join(lines)
