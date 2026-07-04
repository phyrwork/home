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


def simulate_interval(
    *,
    spec: battery.Spec,
    state: battery.State,
    source: InputInterval,
) -> State:
    """Simulate battery and grid energy flows for one planner interval."""
    duration = source.interval.end - source.interval.start
    if duration <= timedelta():
        raise ValueError("Interval end must be after its start")

    duration_hours = _duration_decimal(duration) / Decimal(3_600_000_000)
    if source.tariff.import_price_is_off_peak:
        charge_kwh = min(
            spec.maximum_charge_power_kw * duration_hours,
            (spec.capacity_kwh - state.energy_kwh) / spec.charge_efficiency,
        )
        net_grid_kwh = source.load_kwh + charge_kwh - source.solar_kwh
        return State(
            time=source.interval.end,
            battery=battery.State(
                energy_kwh=state.energy_kwh
                + charge_kwh * spec.charge_efficiency,
            ),
            charge_power_kw=charge_kwh / duration_hours,
            grid_import_kwh=max(net_grid_kwh, Decimal()),
            grid_export_kwh=max(-net_grid_kwh, Decimal()),
        )

    net_solar_kwh = source.solar_kwh - source.load_kwh
    if net_solar_kwh >= 0:
        charge_kwh = min(
            net_solar_kwh,
            spec.maximum_charge_power_kw * duration_hours,
            (spec.capacity_kwh - state.energy_kwh) / spec.charge_efficiency,
        )
        return State(
            time=source.interval.end,
            battery=battery.State(
                energy_kwh=state.energy_kwh
                + charge_kwh * spec.charge_efficiency,
            ),
            charge_power_kw=charge_kwh / duration_hours,
            grid_import_kwh=Decimal(),
            grid_export_kwh=net_solar_kwh - charge_kwh,
        )

    deficit_kwh = -net_solar_kwh
    discharge_kwh = min(
        deficit_kwh,
        spec.maximum_discharge_power_kw * duration_hours,
        (state.energy_kwh - spec.minimum_energy_kwh)
        * spec.discharge_efficiency,
    )
    return State(
        time=source.interval.end,
        battery=battery.State(
            energy_kwh=state.energy_kwh
            - discharge_kwh / spec.discharge_efficiency,
        ),
        charge_power_kw=-discharge_kwh / duration_hours,
        grid_import_kwh=deficit_kwh - discharge_kwh,
        grid_export_kwh=Decimal(),
    )


def simulate(
    *,
    spec: battery.Spec,
    initial_state: battery.State,
    intervals: Sequence[InputInterval],
) -> tuple[State, ...]:
    """Simulate a battery trajectory across contiguous planner intervals."""
    result: list[State] = []
    state = initial_state
    previous_end: datetime | None = None
    for source in intervals:
        if previous_end is not None and source.interval.start != previous_end:
            raise ValueError("Planner intervals must be contiguous and ordered")
        interval_state = simulate_interval(
            spec=spec,
            state=state,
            source=source,
        )
        result.append(interval_state)
        state = interval_state.battery
        previous_end = source.interval.end
    return tuple(result)


def reserve(
    *,
    spec: battery.Spec,
    intervals: Sequence[InputInterval],
    reserve_margin_kwh: Decimal,
) -> Decimal:
    """Calculate forecast reserve including a battery-energy margin."""
    if reserve_margin_kwh < 0:
        raise ValueError("Reserve margin must not be negative")
    _validate_contiguous_intervals(intervals)

    required_kwh = spec.minimum_energy_kwh
    for source in reversed(intervals):
        duration_hours = (
            _duration_decimal(source.interval.end - source.interval.start)
            / Decimal(3_600_000_000)
        )
        net_solar_kwh = source.solar_kwh - source.load_kwh

        if source.tariff.import_price_is_off_peak:
            stored_kwh = (
                spec.maximum_charge_power_kw
                * duration_hours
                * spec.charge_efficiency
            )
            required_kwh = max(
                spec.minimum_energy_kwh,
                required_kwh - stored_kwh,
            )
        elif net_solar_kwh >= 0:
            stored_kwh = (
                min(
                    net_solar_kwh,
                    spec.maximum_charge_power_kw * duration_hours,
                )
                * spec.charge_efficiency
            )
            required_kwh = max(
                spec.minimum_energy_kwh,
                required_kwh - stored_kwh,
            )
        else:
            supplied_kwh = min(
                -net_solar_kwh,
                spec.maximum_discharge_power_kw * duration_hours,
            )
            required_kwh = min(
                spec.capacity_kwh,
                required_kwh
                + supplied_kwh / spec.discharge_efficiency,
            )

    return min(
        spec.capacity_kwh,
        required_kwh + reserve_margin_kwh,
    )


def _validate_contiguous_intervals(
    intervals: Sequence[InputInterval],
) -> None:
    for source in intervals:
        if source.interval.end <= source.interval.start:
            raise ValueError("Interval end must be after its start")
    for previous, current in pairwise(intervals):
        if current.interval.start != previous.interval.end:
            raise ValueError("Planner intervals must be contiguous and ordered")


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
