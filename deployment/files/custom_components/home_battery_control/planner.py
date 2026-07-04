"""Battery planning types."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Protocol

from . import battery, energy, tariff
from .interval import TimeInterval


@dataclass(frozen=True, slots=True)
class Input:
    """Provides source data required to construct and simulate a plan."""

    now: datetime
    """Timezone-aware time from which planning begins."""

    battery_spec: battery.Spec
    """Characteristics and operating limits of the battery."""

    battery_state: battery.State
    """Observed battery state at the planning time."""

    tariff_forecast: tuple[tariff.TariffInterval, ...]
    """Expected future import and export tariff intervals."""

    load_forecast: tuple[energy.EnergyInterval, ...]
    """Forecast household energy consumption."""

    solar_forecast: tuple[energy.EnergyInterval, ...]
    """Forecast solar energy generation."""


@dataclass(frozen=True, slots=True)
class InputInterval:
    """Provides aligned planner inputs for one time interval."""

    interval: TimeInterval
    """Time interval represented by these inputs."""

    load_kwh: Decimal
    """Forecast household consumption during the interval."""

    solar_kwh: Decimal
    """Forecast solar generation during the interval."""

    tariff: tariff.Tariff
    """Import and export prices during the interval."""


@dataclass(frozen=True, slots=True)
class State:
    """Represents simulation state after processing an input interval."""

    time: datetime
    """Timezone-aware time represented by this state."""

    battery: battery.State
    """Projected battery state at this time."""

    charge_power_kw: Decimal
    """Signed battery power during the preceding interval.

    Positive values represent charging; negative values represent discharging.
    """

    grid_import_kwh: Decimal
    """Energy imported during the preceding interval."""

    grid_export_kwh: Decimal
    """Energy exported during the preceding interval."""


def fuse_forecasts(
    *,
    now: datetime,
    tariff_forecast: Sequence[tariff.TariffInterval],
    load_forecast: Sequence[energy.EnergyInterval],
    solar_forecast: Sequence[energy.EnergyInterval],
) -> tuple[InputInterval, ...]:
    """Fuse tariff, load, and solar forecasts into planner input intervals."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Planning time must be timezone-aware")
    if not tariff_forecast:
        raise ValueError("A tariff forecast is required")
    if not load_forecast:
        raise ValueError("A load forecast is required")

    _validate_intervals(tariff_forecast)
    _validate_intervals(load_forecast)
    _validate_intervals(solar_forecast)

    horizon_end = min(
        max(item.interval.end for item in tariff_forecast),
        max(item.interval.end for item in load_forecast),
    )
    if horizon_end <= now:
        return ()

    boundaries = {now, horizon_end}
    for items in (tariff_forecast, load_forecast, solar_forecast):
        for item in items:
            if now < item.interval.start < horizon_end:
                boundaries.add(item.interval.start)
            if now < item.interval.end < horizon_end:
                boundaries.add(item.interval.end)

    ordered_boundaries = sorted(boundaries)
    result: list[InputInterval] = []
    for start, end in pairwise(ordered_boundaries):
        interval = TimeInterval(start=start, end=end)
        result.append(
            InputInterval(
                interval=interval,
                load_kwh=_energy_for_interval(
                    interval,
                    load_forecast,
                    require_coverage=True,
                ),
                solar_kwh=_energy_for_interval(
                    interval,
                    solar_forecast,
                    require_coverage=False,
                ),
                tariff=_tariff_for_interval(interval, tariff_forecast),
            )
        )
    return tuple(result)


class _HasInterval(Protocol):
    interval: TimeInterval


def _validate_intervals(items: Sequence[_HasInterval]) -> None:
    for item in items:
        interval = item.interval
        if interval.start.tzinfo is None or interval.start.utcoffset() is None:
            raise ValueError("Interval timestamps must be timezone-aware")
        if interval.end.tzinfo is None or interval.end.utcoffset() is None:
            raise ValueError("Interval timestamps must be timezone-aware")
        if interval.end <= interval.start:
            raise ValueError("Interval end must be after its start")


def _tariff_for_interval(
    interval: TimeInterval,
    tariffs: Sequence[tariff.TariffInterval],
) -> tariff.Tariff:
    matches = [
        item.tariff
        for item in tariffs
        if item.interval.start <= interval.start and item.interval.end >= interval.end
    ]
    if len(matches) != 1:
        raise ValueError("Tariff must cover each planner interval exactly once")
    return matches[0]


def _energy_for_interval(
    interval: TimeInterval,
    forecasts: Sequence[energy.EnergyInterval],
    *,
    require_coverage: bool,
) -> Decimal:
    result = Decimal()
    covered = timedelta()
    for forecast in forecasts:
        start = max(interval.start, forecast.interval.start)
        end = min(interval.end, forecast.interval.end)
        if end <= start:
            continue

        overlap = end - start
        covered += overlap
        result += (
            forecast.energy_kwh
            * _duration_decimal(overlap)
            / _duration_decimal(forecast.interval.end - forecast.interval.start)
        )

    duration = interval.end - interval.start
    if covered > duration:
        raise ValueError("Energy forecast intervals must not overlap")
    if require_coverage and covered != duration:
        raise ValueError("Load forecast must cover every planner interval")
    return result


def _duration_decimal(duration: timedelta) -> Decimal:
    microseconds = (
        (duration.days * 86_400 + duration.seconds) * 1_000_000
        + duration.microseconds
    )
    return Decimal(microseconds)
