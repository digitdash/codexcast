"""Combine independent forecasts without confusing quota burn rates or reset times."""
from dataclasses import dataclass, field
from .models import UsageSample
from .estimator import Projection


@dataclass(frozen=True)
class OverallProjection:
    active_range: tuple[float, float | None] | None = None
    wall_range: tuple[float, float | None] | None = None
    active_evidence: dict = field(default_factory=dict)
    wall_evidence: dict = field(default_factory=dict)
    wall_forecast_count: int = 0
    reset_race: str = 'UNKNOWN'
    status: str = 'COLLECTING DATA'
    limiting_window: tuple[str, str] | None = None
    hours_to_empty: float | None = None  # Compatibility alias: wall-clock hours.
    wall_hours_to_empty: float | None = None
    wall_confidence: str = 'COLLECTING'
    duty_cycle: float | None = None
    active_limiting_window: tuple[str, str] | None = None
    active_hours_to_empty: float | None = None
    active_confidence: str = 'COLLECTING'
    active_forecast_count: int = 0
    natural_survival: str = 'UNKNOWN'
    banked_reset_count: int | None = None
    confidence: str = 'COLLECTING'
    known_risk: tuple[str, str] | None = None
    unresolved_windows: tuple[tuple[str, str], ...] = ()


def _weakest(projections, field):
    levels = {'COLLECTING': 0, 'LOW': 1, 'MEDIUM': 2, 'HIGH': 3}
    aliases = {'collecting': 'COLLECTING', 'low': 'LOW', 'variable': 'LOW', 'stable': 'LOW', 'observed': 'HIGH'}
    values = [aliases.get(getattr(p, field), getattr(p, field)) for p in projections]
    return min(values, key=levels.get) if values else 'COLLECTING'


def _range_of_minimum(projections, field):
    ranges = [getattr(p, field) for p in projections]
    if not ranges or any(r is None for r in ranges):
        return None
    finite_upper = [r[1] for r in ranges if r[1] is not None]
    return (min(r[0] for r in ranges), min(finite_upper) if finite_upper else None)


def overall_projection(results: list[tuple[UsageSample, Projection]]) -> OverallProjection:
    """Available point limits stay visible; reset certainty requires empirical bounds.

    Interval envelopes describe forecastable windows only, not calibrated joint
    account coverage. Missing ranges never establish all-window reset safety.
    """
    counts = {s.banked_reset_count for s, _ in results}
    banked = counts.pop() if len(counts) == 1 else None
    by_key = {s.key: p for s, p in results}
    active = [(p.active_hours_to_empty, s.key) for s, p in results if p.active_hours_to_empty is not None]
    active_first = min(active) if active else None
    active_ps = [by_key[k] for _, k in active]
    candidates, unresolved, certain_failures, wall_ps = [], [], [], []
    safe = 0
    for sample, p in results:
        empty = sample.remaining_percent == 0 and p.wall_hours_to_empty == 0
        if empty:
            candidates.append((0.0, sample.key))
            certain_failures.append((0.0, sample.key))
            wall_ps.append(p)
            continue
        if p.wall_hours_to_empty is None or p.hours_until_reset is None or p.hours_until_reset <= 0:
            unresolved.append(sample.key)
            continue
        wall_ps.append(p)
        bounds = p.wall_range
        if bounds is not None and bounds[0] > p.hours_until_reset:
            safe += 1
            continue
        if bounds is not None and bounds[1] is not None and bounds[1] < p.hours_until_reset:
            certain_failures.append((p.wall_hours_to_empty, sample.key))
        else:
            unresolved.append(sample.key)
        if p.wall_hours_to_empty < p.hours_until_reset or (bounds is not None and bounds[0] < p.hours_until_reset):
            candidates.append((p.wall_hours_to_empty, sample.key))
    first = min(candidates) if candidates else None
    candidate_ps = [by_key[k] for _, k in candidates]
    if certain_failures:
        race, survival = 'DEPLETION WINS', 'FAIL'
    elif results and safe == len(results):
        race, survival = 'RESET WINS', 'PASS'
    else:
        race, survival = ('UNCERTAIN' if wall_ps else 'UNKNOWN'), 'UNKNOWN'
    status = ('QUOTA EXHAUSTED' if first and first[0] == 0 else
              'PROJECTED QUOTA EXHAUSTION' if certain_failures else
              'SAFE THROUGH NEXT RESET' if survival == 'PASS' else
              'RESET RACE UNCERTAIN' if wall_ps else
              'NO ACTIVE BURN' if results and all(p.status == 'NO ACTIVE BURN' for _, p in results) else
              'COLLECTING DATA')
    confidence = _weakest(wall_ps, 'wall_confidence')
    return OverallProjection(status=status, reset_race=race, natural_survival=survival,
                             banked_reset_count=banked, wall_forecast_count=len(wall_ps),
                             active_limiting_window=active_first[1] if active_first else None,
                             active_hours_to_empty=active_first[0] if active_first else None,
                             active_forecast_count=len(active), active_confidence=_weakest(active_ps, 'confidence'),
                             active_range=_range_of_minimum(active_ps, 'active_range'),
                             active_evidence=by_key[active_first[1]].active_evidence if active_first else {},
                             limiting_window=first[1] if first else None,
                             wall_hours_to_empty=first[0] if first else None, hours_to_empty=first[0] if first else None,
                             wall_confidence=confidence, confidence=confidence,
                             wall_range=(_range_of_minimum(candidate_ps, 'wall_range')
                                         if all(p.wall_range is not None for p in wall_ps) else None),
                             wall_evidence=by_key[first[1]].wall_evidence if first else {},
                             duty_cycle=by_key[first[1]].duty_cycle if first else None,
                             known_risk=min(certain_failures)[1] if certain_failures else None,
                             unresolved_windows=tuple(unresolved))
