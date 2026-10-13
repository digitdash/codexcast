from __future__ import annotations

from dataclasses import dataclass
import math


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class UsageSample:
    timestamp: float
    used_percent: float
    limit_id: str = "codex"
    window: str = "primary"
    window_minutes: int | None = None
    resets_at: float | None = None
    model: str | None = None
    banked_reset_count: int | None = None
    limit_name: str | None = None

    def __post_init__(self) -> None:
        _finite_number(self.timestamp, "timestamp")
        used = _finite_number(self.used_percent, "used_percent")
        if not 0.0 <= used <= 100.0:
            raise ValueError("used_percent must be between 0 and 100")
        _nonempty_string(self.limit_id, "limit_id")
        _nonempty_string(self.window, "window")
        if self.window_minutes is not None:
            if isinstance(self.window_minutes, bool) or not isinstance(self.window_minutes, int):
                raise TypeError("window_minutes must be an integer")
            if self.window_minutes <= 0:
                raise ValueError("window_minutes must be positive")
        if self.resets_at is not None:
            _finite_number(self.resets_at, "resets_at")
        if self.model is not None:
            _nonempty_string(self.model, "model")
        if self.banked_reset_count is not None:
            if isinstance(self.banked_reset_count, bool) or not isinstance(self.banked_reset_count, int):
                raise TypeError("banked_reset_count must be an integer")
            if self.banked_reset_count < 0:
                raise ValueError("banked_reset_count must not be negative")
        if self.limit_name is not None:
            _nonempty_string(self.limit_name, "limit_name")

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @property
    def key(self) -> tuple[str, str]:
        return (self.limit_id, self.window)
