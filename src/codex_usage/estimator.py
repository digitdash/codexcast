"""Pure, deterministic projections from observed quota changes, never token guesses."""
from dataclasses import dataclass, field
import math
import statistics
from .models import UsageSample

# The live source alternates its reset deadline by one or two seconds. These
# rounding corrections do not reset the quota or break an observed interval.
RESET_JITTER_SECONDS = 2


@dataclass(frozen=True)
class EstimatorConfig:
    idle_minutes: float = 5
    half_life_minutes: float = 10
    min_history_minutes: float = 5
    min_events: int = 2
    max_gap_seconds: float = 120
    duty_window_hours: float = 6
    min_wall_history_hours: float = 2
    min_wall_active_minutes: float = 30
    min_wall_events: int = 5
    min_wall_coverage: float = 0.8

    def __post_init__(self):
        for value in (self.idle_minutes, self.half_life_minutes,
                      self.min_history_minutes, self.max_gap_seconds, self.duty_window_hours,
                      self.min_wall_history_hours, self.min_wall_active_minutes):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Estimator durations must be positive and finite')
        if isinstance(self.min_events, bool) or not isinstance(self.min_events, int) or self.min_events < 1:
            raise ValueError('min_events must be a positive integer')
        if isinstance(self.min_wall_events, bool) or not isinstance(self.min_wall_events, int) or self.min_wall_events < 1:
            raise ValueError('min_wall_events must be a positive integer')
        if not math.isfinite(self.min_wall_coverage) or not 0 < self.min_wall_coverage <= 1:
            raise ValueError('min_wall_coverage must be in (0, 1]')
        if self.min_wall_history_hours > self.duty_window_hours:
            raise ValueError('Wall history requirement cannot exceed the rolling window')


@dataclass(frozen=True)
class Projection:
    active_range: tuple[float, float | None] | None = None
    wall_range: tuple[float, float | None] | None = None
    active_evidence: dict = field(default_factory=dict)
    wall_evidence: dict = field(default_factory=dict)
    smoothed_rate: float | None = None
    rates: dict[int, float | None] = field(default_factory=dict)
    session_rate: float | None = None
    hours_to_empty: float | None = None  # Compatibility alias: ACTIVE hours only.
    active_hours_to_empty: float | None = None
    wall_hours_to_empty: float | None = None
    duty_cycle: float | None = None
    effective_wall_rate: float | None = None
    sustained_rate: float | None = None
    sustained_rates: dict[int, float | None] = field(default_factory=dict)
    sustained_block_rates: tuple[float, ...] = ()
    sustained_events: int = 0
    sustained_as_of: float | None = None
    sustained_previous_cycle: bool = False
    intensity_confidence: str = 'collecting'
    reset_race: str = 'UNKNOWN'
    wall_confidence: str = 'collecting'
    reset_survival: str = 'UNKNOWN'
    duty_observed_seconds: float = 0
    duty_active_seconds: float = 0
    duty_events: int = 0
    duty_coverage: float = 0
    hours_until_reset: float | None = None
    projected_remaining: float | None = None
    survives_reset: bool | None = None
    status: str = 'COLLECTING DATA'
    confidence: str = 'collecting'
    idle: bool = False
    consumed: float = 0
    active_seconds: float = 0
    idle_seconds: float = 0
    events: int = 0
    history_seconds: float = 0
    coefficient_of_variation: float | None = None


def _reset_changed(previous, current):
    if previous.resets_at is None or current.resets_at is None:
        return previous.resets_at != current.resets_at
    return abs(current.resets_at - previous.resets_at) > RESET_JITTER_SECONDS


def _epochs(samples, config):
    """Reject mixed series; split resets and unknown observation gaps."""
    result = []
    for sample in samples:
        if not result:
            result.append([sample])
            continue
        previous = result[-1][-1]
        if sample.key != previous.key:
            raise ValueError('Estimate one limit/window at a time')
        if sample.timestamp <= previous.timestamp:
            raise ValueError('Samples must have strictly increasing timestamps')
        boundary = (_reset_changed(previous, sample)
                    or sample.window_minutes != previous.window_minutes
                    or sample.used_percent < previous.used_percent
                    or sample.timestamp - previous.timestamp > config.max_gap_seconds)
        if boundary:
            result.append([])
        result[-1].append(sample)
    return result


def _measure(rows, config):
    """Count short pauses; stop counting after the idle grace; rate on change events."""
    if not rows:
        return [], [], False
    last_change = rows[0].timestamp
    grace = config.idle_minutes * 60
    pending = 0.0
    segments, events = [], []
    for a, b in zip(rows, rows[1:]):
        dt = b.timestamp - a.timestamp
        delta = b.used_percent - a.used_percent
        was_idle = a.timestamp - last_change >= grace
        if delta > 1e-9:
            if was_idle:
                pending = 0.0
            active = dt
            pending += active
            events.append((b.timestamp, delta / pending * 3600, pending))
            pending = 0.0
            last_change = b.timestamp
        else:
            active = min(dt, max(0.0, last_change + grace - a.timestamp))
            pending += active
        segments.append((a.timestamp, b.timestamp, delta, active))
    return segments, events, rows[-1].timestamp - last_change >= grace


def _totals(segments):
    return (sum(s[2] for s in segments), sum(s[3] for s in segments),
            sum(s[1] - s[0] - s[3] for s in segments))


def _duty_cycle(epochs, config, now):
    """Rolling observed activity, reusing _measure; missing intervals are not idle."""
    cutoff = now - config.duty_window_hours * 3600
    observed = active = 0.0
    changes = 0
    for epoch in epochs:
        segments, events, _ = _measure(epoch, config)
        for start, end, _, seconds in segments:
            if end <= cutoff:
                continue
            clipped_start = max(start, cutoff)
            observed += end - clipped_start
            active += max(0.0, start + seconds - clipped_start)
        changes += sum(t > cutoff for t, _, _ in events)
    elapsed = now - max(cutoff, epochs[0][0].timestamp)
    coverage = observed / elapsed if elapsed else 0.0
    ready = (observed >= config.min_wall_history_hours * 3600
             and active >= config.min_wall_active_minutes * 60
             and changes >= config.min_wall_events
             and coverage >= config.min_wall_coverage)
    duty = min(1.0, max(0.0, active / observed)) if ready else None
    return duty, observed, active, changes, coverage


@dataclass(frozen=True)
class SustainedBurn:
    rate: float | None = None
    rates: dict[int, float | None] = field(default_factory=dict)
    blocks: tuple[float, ...] = ()
    events: int = 0
    as_of: float | None = None
    confidence: str = 'collecting'
    previous_cycle: bool = False


def _sustained_burn(samples, config):
    """Median of up to seven disjoint 15-active-minute blocks, ending at last use.

    Reuse observed segments, never consumption across missing/reset intervals.
    A confirmed refill can retain earlier same-duration rate observations at low
    confidence. Other decreases and duration changes invalidate prior evidence.
    Anchoring at last observed use preserves capacity on idle.
    """
    start = 0
    refill_at = None
    for i, (a, b) in enumerate(zip(samples, samples[1:]), 1):
        decrease = b.used_percent < a.used_percent
        refill = (decrease and a.resets_at is not None and b.resets_at is not None
                  and b.resets_at - a.resets_at > RESET_JITTER_SECONDS
                  and b.window_minutes is not None and b.window_minutes == a.window_minutes)
        if b.window_minutes != a.window_minutes or (decrease and not refill):
            start = i
            refill_at = None
        elif refill:
            refill_at = b.timestamp
    segments, events = [], []
    for epoch in _epochs(samples[start:], config):
        ss, ee, _ = _measure(epoch, config)
        segments.extend(ss)
        events.extend(ee)
    if not events:
        return SustainedBurn(rates={m: None for m in (15, 30, 60, 120)})
    as_of = events[-1][0]
    # A rolling active-time window survives overnight idle and short resumptions.
    segments = [s for s in segments if s[1] <= as_of]
    horizons = {}
    for minutes in (15, 30, 60, 120):
        boundary = as_of - minutes * 60
        amount = active = observed = 0.0
        for a, b, delta, seconds in segments:
            if b <= boundary:
                continue
            clipped = max(a, boundary)
            observed += b - clipped
            amount += delta * (b - clipped) / (b - a)
            active += max(0.0, a + seconds - clipped)
        horizons[minutes] = (amount / active * 3600 if active > 0
                             and observed >= minutes * 60 * .8 else None)
    # Walk backwards in active time; a quota jump can affect at most its blocks.
    blocks = []
    active = amount = 0.0
    oldest = as_of
    for a, b, delta, seconds in reversed(segments):
        end = a + seconds
        while seconds > 1e-7 and len(blocks) < 7:
            take = min(seconds, 900 - active)
            consumed = delta * take / seconds
            amount += consumed
            delta -= consumed
            active += take
            seconds -= take
            end -= take
            if active >= 900 - 1e-7:
                oldest = end
                blocks.append(amount * 4)
                active = amount = 0.0
        if len(blocks) == 7:
            break
    changes = sum(oldest < t <= as_of for t, _, _ in events)
    candidate = statistics.median(blocks) if blocks else 0.0
    ready = (len(blocks) >= 5 and changes >= max(5, config.min_events)
             and as_of - oldest >= config.min_history_minutes * 60 and candidate > 1e-9)
    confidence = 'collecting'
    previous_cycle = refill_at is not None and oldest < refill_at
    if ready:
        dispersion = statistics.median(abs(v - candidate) for v in blocks) / candidate
        confidence = ('variable' if dispersion > .35 else
                      'stable' if len(blocks) >= 5 and changes >= 10
                      and sum(v is not None for v in horizons.values()) >= 2 else 'low')
        if previous_cycle or samples[-1].timestamp - oldest > 6 * 3600:
            confidence = 'low'  # Retain capacity, but expose stale activity evidence.
    return SustainedBurn(candidate if ready else None, horizons, tuple(blocks),
                         changes, as_of, confidence, previous_cycle)


def estimate(samples: list[UsageSample], config: EstimatorConfig | None = None) -> Projection:
    config = config or EstimatorConfig()
    if not samples:
        raise ValueError('At least one usage sample is required')
    epochs = _epochs(samples, config)
    rows = epochs[-1]
    segments, events, idle = _measure(rows, config)
    consumed, active, idle_seconds = _totals(segments)
    current = rows[-1]
    history = current.timestamp - rows[0].timestamp
    rates = {}
    for minutes in (5, 15, 30, 60):
        cutoff = current.timestamp - minutes * 60
        if rows[0].timestamp > cutoff:
            rates[minutes] = None
            continue
        amount = denominator = 0.0
        for start, end, delta, seconds in segments:
            if end <= cutoff:
                continue
            fraction = (end - max(start, cutoff)) / (end - start)
            amount += delta * fraction
            # Active time in a zero interval occupies its leading portion.
            denominator += max(0.0, start + seconds - max(start, cutoff))
        rates[minutes] = amount / denominator * 3600 if denominator else 0.0
    smoothed = None
    for _, rate, duration in events:
        alpha = -math.expm1(-math.log(2) * duration / (config.half_life_minutes * 60))
        smoothed = rate if smoothed is None else smoothed + alpha * (rate - smoothed)
    recent = [e[1] for e in events if e[0] >= current.timestamp - 3600]
    cv = (statistics.pstdev(recent) / statistics.mean(recent)
          if len(recent) >= 2 and statistics.mean(recent) > 0 else None)
    reliable = history >= config.min_history_minutes * 60 and len(events) >= config.min_events
    confidence = ('variable' if cv is not None and cv > 0.35 else
                  'stable' if len(recent) >= 5 and history >= 900 else 'low') if reliable else 'collecting'
    reset_hours = (current.resets_at - current.timestamp) / 3600 if current.resets_at is not None else None
    duty, observed, duty_active, duty_events, coverage = _duty_cycle(epochs, config, current.timestamp)
    sustained = _sustained_burn(samples, config)
    eta = wall_eta = effective = projected = survives = None
    status = 'COLLECTING DATA'
    wall_confidence = 'collecting'
    reset_survival = 'UNKNOWN'
    # Historical sustained capacity persists while idle; burst intensity is diagnostic.
    if sustained.rate is not None:
        eta = current.remaining_percent / sustained.rate
    if history >= config.min_history_minutes * 60 and (idle or consumed < 1e-9):
        status = 'NO ACTIVE BURN'
    # Retained sustained burn can inform rolling wall behavior during a pause.
    if duty is not None and sustained.rate is not None:
        effective = sustained.rate * duty
        wall_eta = current.remaining_percent / effective if effective > 1e-9 else None
        wall_confidence = ('variable' if sustained.confidence == 'variable' else
                           'stable' if observed >= 4 * 3600 and coverage >= .95
                           and duty_events >= 10 and sustained.confidence == 'stable' else 'low')
    else:
        duty = None
    if reset_hours is not None and reset_hours <= 0:
        status, eta, wall_eta = 'AWAITING RESET', None, None
    elif current.remaining_percent <= 0:
        # Direct observation of empty quota requires no extrapolation or duty estimate.
        status, eta, wall_eta, projected = 'CRITICAL', 0.0, 0.0, 0.0
        wall_confidence, reset_survival, survives = 'observed', 'EXHAUSTED', False
    elif wall_eta is not None:
        if reset_hours is None:
            status = 'RESET UNKNOWN'
        else:
            raw_remaining = current.remaining_percent - effective * reset_hours
            projected = max(0.0, min(100.0, raw_remaining))
            survives = wall_eta >= reset_hours
            reset_survival = 'SAFE' if survives else 'PROJECTED EMPTY BEFORE RESET'
            if not survives and wall_eta <= .25:
                status = 'CRITICAL'
            elif not survives:
                status = 'PROJECTED EMPTY BEFORE RESET'
            elif projected < 10:
                status = 'CAUTION'
            else:
                status = 'SAFE'
    race = ('DEPLETION WINS' if survives is False else 'RESET WINS' if survives else 'UNKNOWN')
    return Projection(smoothed_rate=smoothed, rates=rates,
                      sustained_rate=sustained.rate, sustained_rates=sustained.rates,
                      sustained_block_rates=sustained.blocks, sustained_events=sustained.events,
                      sustained_as_of=sustained.as_of, intensity_confidence=confidence, reset_race=race,
                      sustained_previous_cycle=sustained.previous_cycle,
                      session_rate=consumed / active * 3600 if active else None,
                      active_hours_to_empty=eta, wall_hours_to_empty=wall_eta,
                      duty_cycle=duty, effective_wall_rate=effective, wall_confidence=wall_confidence,
                      reset_survival=reset_survival, duty_observed_seconds=observed,
                      duty_active_seconds=duty_active, duty_events=duty_events, duty_coverage=coverage,
                      hours_to_empty=eta, hours_until_reset=reset_hours,
                      projected_remaining=projected, survives_reset=survives,
                      status=status, confidence=sustained.confidence, idle=idle,
                      consumed=consumed, active_seconds=active, idle_seconds=idle_seconds,
                      events=len(events), history_seconds=history, coefficient_of_variation=cv)


def summarize(samples: list[UsageSample], config: EstimatorConfig | None = None) -> dict:
    config = config or EstimatorConfig()
    consumption = active = idle = 0.0
    rates = []
    epochs = _epochs(samples, config)
    for rows in epochs:
        segments, events, _ = _measure(rows, config)
        c, a, i = _totals(segments)
        consumption += c
        active += a
        idle += i
        rates.extend(e[1] for e in events)
    return {'samples': len(samples), 'epochs': len(epochs), 'consumed': consumption,
            'active_seconds': active, 'idle_seconds': idle,
            'average_active_rate': consumption / active * 3600 if active else None,
            'median_active_rate': statistics.median(rates) if rates else None,
            'peak_rate': max(rates) if rates else None}
