from __future__ import annotations

from datetime import datetime


def parse_float(value: object | None) -> float | None:
    if value in (None, "", "unknown", "unavailable", "none"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def savings_rate(current_rate: float | None, baseline_rate: float | None) -> float | None:
    if current_rate is None or baseline_rate is None:
        return None
    return baseline_rate - current_rate


def integrate_left(
    power_watts: float | None,
    started_at: datetime | None,
    ended_at: datetime,
    active_power_threshold: float,
) -> float:
    if power_watts is None or started_at is None:
        return 0.0
    if power_watts < active_power_threshold:
        return 0.0
    delta_seconds = (ended_at - started_at).total_seconds()
    if delta_seconds <= 0:
        return 0.0
    return (power_watts / 1000.0) * (delta_seconds / 3600.0)

