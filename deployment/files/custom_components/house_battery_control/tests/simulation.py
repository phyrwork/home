"""Test-only simulation of the house battery controller."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

from custom_components.house_battery_control import battery, controller, planner
from custom_components.house_battery_control.interval import TimeInterval


@dataclass(frozen=True, slots=True)
class State:
    """Records controller and plant output after applying one command."""

    interval: TimeInterval
    """Time interval during which the command was applied."""

    battery: battery.State
    """Battery state at the end of the interval."""

    command: controller.Command
    """Command applied during the interval."""

    battery_power_kw: Decimal
    """Average battery power, positive while charging."""

    grid_import_kwh: Decimal
    """Grid energy imported during the interval."""

    peak_grid_import_kwh: Decimal
    """Peak-rate grid energy imported during the interval."""

    grid_export_kwh: Decimal
    """Grid energy exported during the interval."""

    grid_import_cost: Decimal
    """Import cost incurred during the interval."""

    grid_export_revenue: Decimal
    """Export revenue earned during the interval."""


@dataclass(frozen=True, slots=True)
class _Interval:
    """Combines planner inputs for one simulation interval."""

    input: planner.InputInterval
    """Forecast inputs applied during the interval."""

    reserve: planner.ReserveInterval
    """Required battery energy across the interval."""


def simulate(
    *,
    spec: battery.Spec,
    initial_state: battery.State,
    intervals: Sequence[planner.InputInterval],
    reserve_margin_kwh: Decimal,
    export_hysteresis_kwh: Decimal,
) -> Iterator[State]:
    """Run the real controller against a deterministic test battery."""
    reserves = planner.reserve_intervals(
        spec=spec,
        intervals=intervals,
        reserve_margin_kwh=reserve_margin_kwh,
    )
    battery_state = initial_state
    previous_command: controller.Command | None = None

    for input_interval, reserve_interval in zip(
        intervals,
        reserves,
        strict=True,
    ):
        interval: _Interval | None = _Interval(
            input=input_interval,
            reserve=reserve_interval,
        )

        while interval is not None:
            source = interval.input
            command = controller.select_command(
                spec=spec,
                state=battery_state,
                tariff=source.tariff,
                reserve=interval.reserve,
                export_hysteresis_kwh=export_hysteresis_kwh,
                previous_command=previous_command,
            )
            result = _apply(
                spec=spec,
                state=battery_state,
                source=source,
                command=command,
            )
            duration_hours = _duration_hours(result.applied)
            battery_power_kw = (
                result.battery.energy_kwh - battery_state.energy_kwh
            ) / duration_hours

            yield State(
                interval=result.applied.interval,
                battery=result.battery,
                command=command,
                battery_power_kw=battery_power_kw,
                grid_import_kwh=result.grid_import_kwh,
                peak_grid_import_kwh=(
                    Decimal()
                    if source.tariff.import_price_is_off_peak
                    else result.grid_import_kwh
                ),
                grid_export_kwh=result.grid_export_kwh,
                grid_import_cost=(
                    result.grid_import_kwh
                    * source.tariff.import_price_per_kwh
                ),
                grid_export_revenue=(
                    result.grid_export_kwh
                    * source.tariff.export_price_per_kwh
                ),
            )

            battery_state = result.battery
            previous_command = command
            interval = (
                None
                if result.remaining is None
                else _Interval(
                    input=result.remaining,
                    reserve=reserve_interval,
                )
            )


@dataclass(frozen=True, slots=True)
class _PlantResult:
    """Records physical output from applying one command."""

    applied: planner.InputInterval
    """Inputs consumed while applying the command."""

    remaining: planner.InputInterval | None
    """Inputs left after the command reaches its target."""

    battery: battery.State
    """Battery state after applying the command."""

    grid_import_kwh: Decimal
    """Grid energy imported while applying the command."""

    grid_export_kwh: Decimal
    """Grid energy exported while applying the command."""


def _apply(
    *,
    spec: battery.Spec,
    state: battery.State,
    source: planner.InputInterval,
    command: controller.Command,
) -> _PlantResult:
    applied = source
    remaining: planner.InputInterval | None = None
    duration_hours = _duration_hours(source)

    if isinstance(command, controller.GridCharge):
        stored_kwh = min(
            max(Decimal(), command.target_energy_kwh - state.energy_kwh),
            spec.maximum_charge_power_kw
            * duration_hours
            * spec.charge_efficiency,
        )
        charge_input_kwh = stored_kwh / spec.charge_efficiency
        end_energy_kwh = state.energy_kwh + stored_kwh
    elif isinstance(command, controller.ForceExport):
        available_kwh = max(
            Decimal(),
            state.energy_kwh - command.target_energy_kwh,
        )
        possible_kwh = spec.maximum_discharge_power_kw * duration_hours
        withdrawn_kwh = min(available_kwh, possible_kwh)
        if withdrawn_kwh < possible_kwh:
            applied, remaining = _split(source, withdrawn_kwh / possible_kwh)
        supplied_kwh = withdrawn_kwh * spec.discharge_efficiency
        end_energy_kwh = state.energy_kwh - withdrawn_kwh
    elif isinstance(command, controller.SelfConsumption):
        net_load_kwh = source.load_kwh - source.solar_kwh
        if net_load_kwh >= 0:
            supplied_kwh = min(
                net_load_kwh,
                spec.maximum_discharge_power_kw * duration_hours,
                max(Decimal(), state.energy_kwh - command.minimum_energy_kwh)
                * spec.discharge_efficiency,
            )
            end_energy_kwh = state.energy_kwh - (
                supplied_kwh / spec.discharge_efficiency
            )
        else:
            charge_input_kwh = min(
                -net_load_kwh,
                spec.maximum_charge_power_kw * duration_hours,
                (spec.capacity_kwh - state.energy_kwh)
                / spec.charge_efficiency,
            )
            end_energy_kwh = (
                state.energy_kwh
                + charge_input_kwh * spec.charge_efficiency
            )
    else:
        end_energy_kwh = state.energy_kwh

    grid_balance_kwh = applied.load_kwh - applied.solar_kwh
    if isinstance(command, controller.GridCharge):
        grid_balance_kwh += charge_input_kwh
    elif isinstance(command, controller.ForceExport):
        grid_balance_kwh -= supplied_kwh
    elif isinstance(command, controller.SelfConsumption):
        if grid_balance_kwh >= 0:
            grid_balance_kwh -= supplied_kwh
        else:
            grid_balance_kwh += charge_input_kwh

    return _PlantResult(
        applied=applied,
        remaining=remaining,
        battery=battery.State(energy_kwh=end_energy_kwh),
        grid_import_kwh=max(Decimal(), grid_balance_kwh),
        grid_export_kwh=max(Decimal(), -grid_balance_kwh),
    )


def _split(
    source: planner.InputInterval,
    fraction: Decimal,
) -> tuple[planner.InputInterval, planner.InputInterval | None]:
    duration = source.interval.end - source.interval.start
    boundary = source.interval.start + duration * float(fraction)
    applied = planner.InputInterval(
        interval=TimeInterval(source.interval.start, boundary),
        load_kwh=source.load_kwh * fraction,
        solar_kwh=source.solar_kwh * fraction,
        tariff=source.tariff,
    )
    remaining_fraction = Decimal(1) - fraction
    if remaining_fraction == 0:
        return applied, None
    return applied, planner.InputInterval(
        interval=TimeInterval(boundary, source.interval.end),
        load_kwh=source.load_kwh * remaining_fraction,
        solar_kwh=source.solar_kwh * remaining_fraction,
        tariff=source.tariff,
    )


def _duration_hours(source: planner.InputInterval) -> Decimal:
    duration = source.interval.end - source.interval.start
    microseconds = (
        (duration.days * 86_400 + duration.seconds) * 1_000_000
        + duration.microseconds
    )
    return Decimal(microseconds) / Decimal(3_600_000_000)
