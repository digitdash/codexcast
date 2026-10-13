"""Orchestration stays independent of terminal UI and Codex transport."""
from typing import Protocol
from .models import UsageSample
from .estimator import EstimatorConfig
from .uncertainty import forecast


class UsageProvider(Protocol):
    def sample(self) -> list[UsageSample]: ...
    def close(self) -> None: ...


class UsageMonitor:
    def __init__(self, provider: UsageProvider, store, config=None, *, since=None):
        self.provider = provider
        self.store = store
        self.config = config or EstimatorConfig()
        self.since = since

    def tick(self):
        samples = self.provider.sample()
        self.store.add(samples)
        return [(sample, forecast(self.store.load(limit_id=sample.limit_id,
                                                 window=sample.window, since=self.since), self.config))
                for sample in samples]


def has_recent_usage(store, limit_id, now, *, max_gap_seconds=120):
    """Observed quota increase in either window in the last hour, across restarts.

    A nonzero balance alone cannot establish when usage happened. Compare adjacent
    readings within the same reset epoch, also recognizing first use from zero when
    an unused window's rolling reset timestamp changes. Gaps/reset metadata alone
    are not evidence of activity.
    """
    cutoff = now - 3600
    previous = {}
    for sample in store.load(limit_id=limit_id, since=cutoff - max_gap_seconds):
        if sample.timestamp > now:
            continue
        before = previous.get(sample.window)
        if (before is not None and sample.timestamp >= cutoff
                and 0 < sample.timestamp - before.timestamp <= max_gap_seconds
                and (sample.resets_at == before.resets_at or before.used_percent == 0)
                and sample.window_minutes == before.window_minutes
                and sample.used_percent > before.used_percent + 1e-9):
            return True
        previous[sample.window] = sample
    return False
