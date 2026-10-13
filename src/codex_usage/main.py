"""Watch quota, inspect history, or export allowlisted observations."""
import argparse
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import sqlite3
import sys
import time
from .estimator import EstimatorConfig, summarize
from .backtest import backtest, read_history, render_backtest
from .provider import CodexProvider, ProviderError
from .collection import collect, calculate
from .lifecycle import CollectionLock
from .overall import overall_projection
from .storage import DEFAULT_DB, Store, export_samples
from .tokens import TokenReader
from .credits import INDICATIVE_USD_PER_CREDIT
from .console import ConsoleControls


def duration(hours, *, days=False):
    if hours is None:
        return 'unavailable'
    minutes = max(0, round(hours * 60))
    if minutes == 0 and hours > 0:
        return '<1m'
    if days and minutes >= 24 * 60:
        return f'{minutes // 1440}d {minutes // 60 % 24}h {minutes % 60:02d}m'
    return f'{minutes // 60}h {minutes % 60:02d}m'


def likely_range(bounds):
    if bounds is None:
        return 'collecting data...'
    return f'{duration(bounds[0])} - {duration(bounds[1]) if bounds[1] is not None else "unbounded"}'


def evidence_text(evidence):
    if not evidence or evidence.get('horizon_minutes') is None:
        return 'no matching horizon yet'
    count = evidence['count']
    text = f'{count} evaluation{"s" if count != 1 else ""} at {evidence["horizon_minutes"]}m'
    if count < 10:
        return text+' (10 required for a range)'
    error = evidence.get('median_relative_error')
    return text+(f'; median error {error:.0%}' if error is not None else '')


def rate(value):
    return 'collecting data...' if value is None else f'{value:.1f} pp / active hr'


def safe_text(value):
    return ''.join(c for c in str(value) if c.isprintable())[:120]


def window_label(sample):
    return f'{safe_text(sample.limit_name or sample.limit_id)} / {sample.window} ({sample.window_minutes or "?"} min window)'


def render_overall(results):
    o = overall_projection(results)
    labels = {s.key: window_label(s) for s, _ in results}
    banked = ('unavailable' if o.banked_reset_count is None else
              f'YES ({o.banked_reset_count})' if o.banked_reset_count else 'NO')
    limiting = (labels[o.limiting_window] if o.limiting_window else
                'none' if o.natural_survival == 'PASS' else 'undetermined')
    active = next((p for s, p in results if s.key == o.active_limiting_window), None)
    wall_key = o.limiting_window or o.active_limiting_window
    wall = next((p for s, p in results if s.key == wall_key), None)
    lines = ['', 'OVERALL', '', f'Scope:                    {len(results)} exposed windows (all returned buckets)',
             f'Active limiting window:   {labels[o.active_limiting_window] if o.active_limiting_window else "undetermined"}',
             f'Current state:            {"IDLE" if active and active.idle else "ACTIVE" if active else "UNKNOWN"}',
             f'Current intensity:        {rate(active.smoothed_rate) if active else "unavailable"}',
             f'Sustained forecast burn:  {rate(active.sustained_rate) if active else "collecting data..."}',
             f'Forecast basis:           {"includes previous quota cycle (LOW confidence)" if active and active.sustained_previous_cycle else "current quota cycle" if active else "collecting data..."}',
             f'Active capacity remaining: {"~"+duration(o.active_hours_to_empty) if o.active_hours_to_empty is not None else "collecting data..."}',
             f'Likely active range:      {likely_range(o.active_range)}',
             f'Active forecast scope:    {o.active_forecast_count}/{len(results)} windows (lowest available estimate)',
             f'Active confidence:        {o.active_confidence}',
             f'Active backtest evidence: {evidence_text(o.active_evidence)}',
             f'Wall limiting window:     {limiting}' + (' (among forecastable windows)' if o.unresolved_windows and o.limiting_window else ''),
             f'Wall forecast source:     {labels[wall_key] if wall and wall.wall_hours_to_empty is not None else "undetermined"}',
             f'Observed duty cycle:      {f"{wall.duty_cycle:.0%}" if wall and wall.duty_cycle is not None else "unavailable"}',
             f'Effective wall burn:      {f"{wall.effective_wall_rate:.1f} pp / wall hr" if wall and wall.effective_wall_rate is not None else "unavailable"}',
             f'Wall-clock depletion:     {"~"+duration(o.wall_hours_to_empty) if o.wall_hours_to_empty is not None else "collecting data..." if o.natural_survival != "PASS" else "not before next resets"}',
             f'Likely wall range:        {likely_range(o.wall_range) if o.natural_survival != "PASS" else "no depletion before next resets"}',
             f'Reset race:               {o.reset_race}',
             f'Natural reset survival:   {o.natural_survival}',
             f'Forecast coverage:        {o.wall_forecast_count}/{len(results)} windows (wall/reset point forecast)',
             f'Banked reset:             {banked}',
             f'Status:                   {o.status}',
             f'Wall-clock confidence:    {o.wall_confidence}',
             f'Wall backtest evidence:   {evidence_text(o.wall_evidence)}']
    if o.limiting_window:
        lines.append(f'Cause:                    {labels[o.limiting_window]} ' + ('quota exhaustion' if wall and wall.reset_race == 'DEPLETION WINS' else 'possible quota exhaustion'))
    if o.known_risk and o.known_risk != o.limiting_window:
        lines.append(f'Known risk:               {labels[o.known_risk]} exhausts before reset; earliest unresolved')
    if o.unresolved_windows:
        lines.append(f'Unresolved windows:       {len(o.unresolved_windows)} (empirical range or reset unresolved)')
    if results:
        stamp = max(s.timestamp for s, _ in results)
        lines.append(f'Sampled:                  {datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}')
    return lines


def usage_meter(used, *, color=False):
    remaining = 100-used
    filled = max(0, min(20, round(used/5)))
    ink = '\033[32m' if remaining >= 50 else '\033[38;5;208m' if remaining >= 10 else '\033[31m'
    symbols = '#' * filled
    if color and symbols:
        symbols = ink + symbols + '\033[0m'
    return f'{used:.1f}% used [{symbols}{"-" * (20-filled)}] {remaining:.1f}% remaining'


def render(results, verbose=False, *, overall_results=None, tokens=None, credits=None, color=False):
    all_results = results if overall_results is None else overall_results
    lines = ['CODEX USAGE', '', 'Active capacity uses sustained burn; wall depletion also uses observed duty cycle. Ctrl-C to stop.']
    lines.extend(render_overall(all_results))
    credit_rate = credits.get('credits_per_active_hour') if credits else None
    if credit_rate is None:
        lines.append('Estimated sustained cost: collecting data...')
    else:
        credit_amount = Decimal(str(credit_rate))
        shown_credits = credit_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        shown_usd = (credit_amount * INDICATIVE_USD_PER_CREDIT).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        lines.append(f'Estimated sustained cost: {shown_credits:,} credits/hr (~${shown_usd:,.2f}/hr) ({credits["basis"]})')
    if credits:
        lines.append('USD basis:               $0.04/credit; indicative, purchase prices may vary')
        lines.append('Credit scope:             ' + credits['scope'])
        lines.append(f'Credit evidence:          {credits["observations"]} responses; {credits["active_seconds"]/60:.1f} active min; {credits["unpriced_responses"]} unpriced')
        if credits['mode'] == 'credits':
            lines.append(f'Balance evidence:         {credits["balance_intervals"]} matched intervals; {credits["skipped_balance_intervals"]} gaps/top-ups skipped')
        else:
            lines.append(f'Credit rate card:         {credits["rate_card_date"]}; estimate, not a quota-to-credit conversion')
        if credits.get('reason'):
            lines.append('Credit status:            ' + credits['reason'])
    for sample, p in results:
        reset = datetime.fromtimestamp(sample.resets_at).astimezone().strftime('%a %b %d %I:%M %p %Z') if sample.resets_at is not None else 'unavailable'
        lines.extend([
            '',
            f'Quota window:             {window_label(sample)}',
            f'Usage:                    {usage_meter(sample.used_percent, color=color)}',
            f'Scheduled reset:          {reset}',
            f'Reset in:                 {duration(p.hours_until_reset, days=True)}',
            f'Projected at reset:       {"EXHAUSTED" if p.survives_reset is False else "UNCERTAIN" if p.reset_race == "UNCERTAIN" else f"{p.projected_remaining:.1f}%" if p.projected_remaining is not None else "unavailable"}',
        ])
        if p.survives_reset is False and p.hours_until_reset is not None and p.wall_hours_to_empty is not None:
            lines.append(f'Dead before reset:        {duration(p.hours_until_reset-p.wall_hours_to_empty)} BEFORE RESET')
        if verbose:
            lines.append('DEBUG (allowlisted): ' + json.dumps({'sample': asdict(sample), 'estimator': asdict(p)}, allow_nan=False))
    models = {s.model for s, _ in results}
    lines.append('Model:                    ' + (safe_text(next(iter(models))) if len(models) == 1 and None not in models else 'unavailable (account-level telemetry)'))
    return '\n'.join(lines)


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be positive and finite')
    return number


def interval(value):
    number = positive(value)
    if number < 10:
        raise argparse.ArgumentTypeError('minimum interval is 10 seconds')
    return number


def count(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument('--version', action='version', version='codex-usage 0.1.0')
    commands = root.add_subparsers(dest='command', required=True)
    for name in ('watch', 'history', 'export', 'backtest'):
        p = commands.add_parser(name)
        p.add_argument('--db', default=str(DEFAULT_DB), help='SQLite usage history path')
        p.add_argument('--limit', default=None, help='filter displayed bucket; watch defaults to all; overall still covers all returned windows')
        p.add_argument('--window', choices=['primary', 'secondary', 'all'], default='all')
        if name in ('watch', 'history', 'backtest'):
            p.add_argument('--idle-minutes', type=positive, default=5)
            p.add_argument('--max-gap-seconds', type=positive, default=120,
                           help='maximum observed gap; watch uses at least 3x poll interval')
        if name == 'watch':
            p.add_argument('--interval', type=interval, default=30)
            p.add_argument('--codex-bin', default='codex')
            p.add_argument('--timeout', type=positive, default=20)
            p.add_argument('--half-life-minutes', type=positive, default=10)
            p.add_argument('--min-history-minutes', type=positive, default=5)
            p.add_argument('--min-events', type=count, default=2)
            p.add_argument('--once', action='store_true')
            p.add_argument('--samples', type=count, help='stop after this many successful reads')
            p.add_argument('--verbose', '--debug', action='store_true')
            p.add_argument('--json', action='store_true', help='newline-delimited JSON observations and projections')
            p.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
            p.add_argument('--usage-mode', choices=['auto', 'quota', 'credits', 'tokens'], default='auto',
                           help='auto detects recent balance spending; quota estimates credit equivalents; credits observes balance burn; tokens is a legacy alias')
            p.add_argument('--sessions-dir', help='local Codex rollout directory; defaults to CODEX_HOME/sessions')
        elif name == 'backtest':
            p.add_argument('--json', action='store_true', help='detailed metrics and chronological evaluation records')
        elif name == 'export':
            p.add_argument('--format', choices=['csv', 'json'], default='csv')
    return root


def watch(args, store):
    with CollectionLock(store.path):
        return _watch(args, store)


def _watch(args, store):
    config = EstimatorConfig(idle_minutes=args.idle_minutes, half_life_minutes=args.half_life_minutes,
                             min_history_minutes=args.min_history_minutes, min_events=args.min_events,
                             max_gap_seconds=max(args.max_gap_seconds, 3 * args.interval))
    provider = None
    completed = failures = 0
    target = 1 if args.once else args.samples
    interactive = sys.stdout.isatty() and not args.json
    color = args.color == 'always' or (args.color == 'auto' and interactive)
    reader = TokenReader(args.sessions_dir, idle_minutes=args.idle_minutes)
    controls = ConsoleControls(enabled=interactive and not args.once)
    session_start = None

    def pause(seconds):
        nonlocal session_start
        if controls.wait(seconds):
            session_start = time.time()

    try:
        controls.open()
        if interactive:
            print('\033[?1049h\033[?25l', end='', flush=True)
        while target is None or completed < target:
            try:
                if provider is None:
                    provider = CodexProvider(args.codex_bin, timeout=args.timeout)
                samples = collect(provider, store, reader)
                results, tokens, credits = calculate(store, samples, config, since=session_start,
                                                     usage_mode=args.usage_mode)
                selected = [(s, p) for s, p in results if (args.limit is None or s.limit_id == args.limit)
                            and (args.window == 'all' or s.window == args.window)]
                if not selected:
                    raise ProviderError('Requested quota window is unavailable; inspect export for available limits')
                if args.json:
                    print(json.dumps([{'sample': asdict(s), 'projection': asdict(p),
                                       'overall': asdict(overall_projection(results)), 'tokens': tokens, 'credits': credits}
                                      for s, p in selected], allow_nan=False), flush=True)
                else:
                    text = render(selected, args.verbose, overall_results=results, credits=credits, color=color)
                    if session_start is not None:
                        text += '\nSession averages since:    '+datetime.fromtimestamp(session_start).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
                    if controls.active:
                        text += '\n'+controls.footer
                    print(('\033[H\033[2J' if interactive else '') + text, flush=True)
                completed += 1
                failures = 0
                if target is None or completed < target:
                    pause(args.interval)
            except (ProviderError, OSError) as error:
                failures += 1
                if provider is not None:
                    provider.close()
                    provider = None
                message = f'Usage unavailable; forecast stale. {safe_text(error)}'
                if interactive:
                    if controls.active:
                        message += '\n'+controls.footer
                    print('\033[H\033[2J' + message, flush=True)
                else:
                    print(message, file=sys.stderr, flush=True)
                if args.once or (target is not None and failures >= 3):
                    return 1
                delay = min(300, args.interval * 2 ** min(failures - 1, 8))
                pause(delay)
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            try:
                controls.close()
            finally:
                if interactive:
                    print('\033[?25h\033[?1049l', end='', flush=True)
    return 0


def history(rows, config):
    if not rows:
        print('No usage history yet. Run codex-usage watch.')
        return
    groups = {}
    for row in rows:
        groups.setdefault(row.key, []).append(row)
    for (limit_id, window), samples in sorted(groups.items()):
        h = summarize(samples, config)
        print(f'\n{safe_text(limit_id)} / {window} ({h["samples"]} samples; {h["epochs"]} epochs)\n')
        print(f'Average active burn:      {rate(h["average_active_rate"])}')
        print(f'Median active event burn: {rate(h["median_active_rate"])}')
        print(f'Peak observed event burn: {rate(h["peak_rate"])}')
        print(f'Total measured consumption: {h["consumed"]:.2f} percentage points')
        print(f'Inferred active time:     {duration(h["active_seconds"]/3600)}')
        print(f'Inferred idle time:       {duration(h["idle_seconds"]/3600)}')
    print('\nModel attribution unavailable from account-level usage; no model multipliers inferred.')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ['watch']
    args = parser().parse_args(argv)
    try:
        if args.command == 'backtest':
            rows = read_history(args.db, limit_id=args.limit, window=None if args.window == 'all' else args.window)
            groups = {}
            for row in rows:
                groups.setdefault(row.key, []).append(row)
            reports = []
            config = EstimatorConfig(idle_minutes=args.idle_minutes, max_gap_seconds=args.max_gap_seconds)
            for samples in groups.values():
                report = backtest(samples, config)
                reports.append({'window': asdict(samples[-1]), 'report': report})
                if not args.json:
                    print(render_backtest(samples[-1], report))
            if args.json:
                print(json.dumps(reports, indent=2, allow_nan=False))
            elif not reports:
                print('No usage history available for backtesting.')
            return 0
        with Store(args.db) as store:
            if args.command == 'watch':
                return watch(args, store)
            rows = store.load(limit_id=args.limit, window=None if args.window == 'all' else args.window)
            if args.command == 'export':
                export_samples(rows, sys.stdout, args.format)
            else:
                history(rows, EstimatorConfig(idle_minutes=args.idle_minutes, max_gap_seconds=args.max_gap_seconds))
        return 0
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f'codex-usage:              {safe_text(error)}', file=sys.stderr)
        return 1
