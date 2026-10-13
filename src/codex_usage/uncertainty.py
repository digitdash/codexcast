"""Enrich the unchanged point estimator with empirical uncertainty and reset races."""
from dataclasses import replace
from .backtest import backtest, calibrate
from .estimator import estimate


def confidence(point, evidence, base, *, wall=False):
    if point is None:
        return 'COLLECTING'
    bounds = evidence['range']
    if (bounds is None or bounds[1] is None or base != 'stable'
            or evidence['usage_changes'] < 10):
        return 'LOW'
    median = evidence['median_relative_error']
    tail = evidence['p80_relative_error']
    duty = evidence['duty_variability']
    if wall and (duty is None or duty > .25):
        return 'LOW'
    if evidence['count'] >= 30 and evidence['usage_changes'] >= 50 and median <= .15 and tail <= .35 and (not wall or duty <= .1):
        return 'HIGH'
    if median <= .30 and tail <= .75:
        return 'MEDIUM'
    return 'LOW'


def with_uncertainty(samples, p, report):
    now = samples[-1].timestamp
    active = calibrate(report['evaluations'], clock='active', point_hours=p.active_hours_to_empty, now=now)
    wall = calibrate(report['evaluations'], clock='wall', point_hours=p.wall_hours_to_empty, now=now)
    ac = confidence(p.active_hours_to_empty, active, p.confidence)
    wc = confidence(p.wall_hours_to_empty, wall, p.wall_confidence, wall=True)
    race = 'UNKNOWN'
    projected = survives = None
    status = p.status
    survival = 'UNKNOWN'
    if p.status == 'CRITICAL' and samples[-1].remaining_percent == 0 and p.wall_hours_to_empty == 0:
        # Already empty is observed state, independent of calibration.
        active['range'] = wall['range'] = (0, 0)
        ac = wc = 'HIGH'
        race, survival, survives, projected = 'DEPLETION WINS', 'EXHAUSTED', False, 0
    elif p.wall_hours_to_empty is not None and p.hours_until_reset is not None and p.hours_until_reset > 0:
        bounds = wall['range']
        if bounds is None:
            race = 'UNCERTAIN'
        elif bounds[1] is not None and bounds[1] < p.hours_until_reset:
            race = 'DEPLETION WINS'
        elif bounds[0] > p.hours_until_reset:
            race = 'RESET WINS'
        else:
            race = 'UNCERTAIN'
        if race == 'UNCERTAIN':
            status = 'RESET RACE UNCERTAIN'
        else:
            projected = p.projected_remaining
            survives = race == 'RESET WINS'
            survival = 'SAFE' if survives else 'PROJECTED EMPTY BEFORE RESET'
    elif p.status not in ('AWAITING RESET', 'NO ACTIVE BURN', 'RESET UNKNOWN'):
        status = 'COLLECTING DATA'
    return replace(p, active_range=active['range'], wall_range=wall['range'],
                   active_evidence=active, wall_evidence=wall, confidence=ac, wall_confidence=wc,
                   reset_race=race, reset_survival=survival, survives_reset=survives,
                   projected_remaining=projected, status=status)


def forecast(samples, config=None):
    p = estimate(samples[-10000:], config)
    return with_uncertainty(samples, p, backtest(samples, config))
