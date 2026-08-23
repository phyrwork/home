"""Read live inputs and build one strategy evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping, Sequence

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from . import load
from .config import Config
from .contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from .domain_constants import FULL_SOC_PERCENT, MINIMUM_SOC_PERCENT
from .energy import EnergyInterval
from .interval import TimeInterval
from .octopus_windows import (
    AdjustedRateInterval,
    CheapWindow,
    CoverageStatus,
    DispatchSourceObservation,
    ExportRateInterval,
    RateSourceObservation,
    evaluate_cheap_windows,
    evaluate_trusted_import_rates,
    parse_fused_export_rates,
    parse_fused_import_rates,
)
from .reserve_planner import ReserveInputInterval, ReservePlanResult, ReservePlanningStatus, plan_reserve
from .solis_reader import read_solis_state
from .solis_state import SolisStateReadResult, SolisStateSnapshot
from .strategy import CycleState, StrategyInputs


@dataclass(frozen=True, slots=True)
class RuntimeInputs:
    strategy: StrategyInputs
    solis: SolisStateReadResult
    reserve: ReservePlanResult
    current_window: CheapWindow | None
    next_window: CheapWindow | None
    maximum_charge_power_kw: Decimal
    maximum_discharge_power_kw: Decimal
    cycle_discharge_duration: timedelta


async def async_read_runtime_inputs(
    hass: HomeAssistant,
    config: Config,
    *,
    now: datetime,
    cycle_state: CycleState,
    cycle_deadline: datetime | None = None,
) -> RuntimeInputs:
    guard = _state(hass, config.control_disable_guard_entity_id)
    guard_off = guard.state == "off"
    solis = read_solis_state(config.solis, hass.states, now)
    if solis.health is not ControllerHealth.HEALTHY or solis.snapshot is None:
        raise ValueError("Solis state is not healthy: " + ", ".join(issue.code for issue in solis.issues))
    snapshot = solis.snapshot

    import_state = _state(hass, config.tariff.import_rates_entity_id)
    export_state = _state(hass, config.tariff.export_rates_entity_id)
    cycle_duration = _cycle_duration(_state(hass, config.cycle_discharge_duration_entity_id))
    import_rates = parse_fused_import_rates(_attribute(import_state, "rates"))
    export_rates = parse_fused_export_rates(_attribute(export_state, "rates"))
    horizon_end = min(max(item.end for item in import_rates), max(item.end for item in export_rates))
    if horizon_end <= now:
        raise ValueError("tariff forecast does not extend beyond now")
    windows_result = evaluate_cheap_windows(
        import_rates=import_rates,
        export_rates=export_rates,
        start=now,
        end=horizon_end,
        now=now,
        import_source=_rate_source(import_state, "rate_source_entity_id", "rate_source_last_retrieved"),
        export_source=_rate_source(export_state, "rate_source_entity_id", "rate_source_last_retrieved"),
        dispatch_source=_dispatch_source(import_state),
        charge_efficiency=config.battery.charge_efficiency,
        discharge_efficiency=config.battery.discharge_efficiency,
    )
    if windows_result.coverage_status not in (CoverageStatus.COMPLETE, CoverageStatus.TRUSTED_EMPTY):
        raise ValueError("tariff window input is not trusted: " + "; ".join(windows_result.issues))
    windows = windows_result.windows
    current_window = next((window for window in windows if window.start <= now < window.end), None)
    next_window = next((window for window in windows if window.start > now), None)

    reserve_end = next_window.start if next_window is not None else horizon_end
    # Solis schedules are real, bounded schedules.  Keep the reserve-export
    # intent safely below the device's 24-hour limit when a forecast horizon
    # has no subsequent cheap start.
    reserve_end = _minute_floor(min(reserve_end, now + timedelta(hours=23, minutes=59)))
    if reserve_end <= now:
        reserve_end = _minute_floor(now + timedelta(minutes=1))
    trusted_import = evaluate_trusted_import_rates(
        import_rates=import_rates,
        start=now,
        end=reserve_end,
        now=now,
        import_source=_rate_source(import_state, "rate_source_entity_id", "rate_source_last_retrieved"),
        dispatch_source=_dispatch_source(import_state),
    )
    if trusted_import.coverage_status is not CoverageStatus.COMPLETE:
        raise ValueError("reserve tariff input is not trusted: " + "; ".join(trusted_import.issues))

    maximum_charge_power, maximum_discharge_power = _runtime_powers(snapshot)
    forecast = await _forecast_intervals(hass, config, now, reserve_end, trusted_import.intervals)
    reserve = plan_reserve(
        intervals=forecast,
        capacity_kwh=config.battery.capacity_kwh,
        minimum_energy_kwh=config.battery.minimum_energy_kwh,
        reserve_margin_kwh=config.battery.reserve_margin_kwh,
        charge_efficiency=config.battery.charge_efficiency,
        discharge_efficiency=config.battery.discharge_efficiency,
        maximum_charge_power_kw=maximum_charge_power,
        maximum_discharge_power_kw=maximum_discharge_power,
    )
    if reserve.status is not ReservePlanningStatus.COMPLETE or reserve.reserve_energy_kwh is None:
        raise ValueError("household reserve is unavailable: " + "; ".join(issue.detail for issue in reserve.issues))

    reserve_soc = _soc_ceiling(reserve.reserve_energy_kwh, config.battery.capacity_kwh)
    reserve_soc = max(Decimal(MINIMUM_SOC_PERCENT), reserve_soc)
    telemetry = snapshot.telemetry
    current_energy = config.battery.capacity_kwh * telemetry.state_of_charge_percent / Decimal(FULL_SOC_PERCENT)
    charge_state = snapshot.slots[0].charge
    cycle_state_observed = snapshot.slots[0].discharge
    reserve_state = snapshot.slots[1].discharge
    cheap_intent = None
    reserve_intent = None
    cycle_intent = None

    if current_window is not None:
        start = _minute_floor(now)
        if start < current_window.start:
            start = current_window.start
        cheap_intent = SlotIntent(
            SlotOwner.CHEAP_CHARGING,
            1,
            SlotDirection.CHARGE,
            start,
            current_window.end,
            charge_state.current.maximum,
            min(Decimal(FULL_SOC_PERCENT), charge_state.target_soc.maximum),
            current_window.end,
        )
        if _positive_margin(current_window):
            cycle_start = start
            cycle_end = min(cycle_start + cycle_duration, current_window.end)
            if cycle_deadline is not None:
                cycle_end = min(cycle_deadline, current_window.end)
                cycle_start = cycle_end - cycle_duration
        else:
            cycle_start = start
            cycle_end = start
        if cycle_end > cycle_start:
            cycle_intent = SlotIntent(
                SlotOwner.FULL_SOC_CYCLING,
                1,
                SlotDirection.DISCHARGE,
                cycle_start,
                cycle_end,
                cycle_state_observed.current.maximum,
                max(reserve_soc, cycle_state_observed.target_soc.minimum),
                cycle_end,
            )

    if not _positive_margin(current_window) and current_energy > reserve.reserve_energy_kwh:
        reserve_target = _quantize_target(
            reserve_soc,
            reserve_state.target_soc.minimum,
            reserve_state.target_soc.maximum,
            reserve_state.target_soc.step,
        )
        reserve_intent = SlotIntent(
            SlotOwner.RESERVE_EXPORT,
            2,
            SlotDirection.DISCHARGE,
            _minute_floor(now),
            reserve_end,
            reserve_state.current.maximum,
            reserve_target,
            reserve_end,
        )

    recharge = timedelta(0)
    if cycle_intent is not None:
        withdrawn = maximum_discharge_power * Decimal(str(cycle_duration.total_seconds())) / Decimal(3600) / config.battery.discharge_efficiency
        recharge_hours = withdrawn / (maximum_charge_power * config.battery.charge_efficiency)
        recharge = timedelta(seconds=float(recharge_hours * Decimal(3600)))

    strategy = StrategyInputs(
        now=now,
        health=ControllerHealth.HEALTHY,
        control_enabled=config.dynamic_control_enabled,
        guard_off=guard_off,
        soc_percent=telemetry.state_of_charge_percent,
        reserve_soc_percent=reserve_soc,
        cheap_window=current_window,
        cheap_charge_intent=cheap_intent,
        reserve_discharge_intent=reserve_intent,
        cycle_window=current_window,
        cycle_discharge_intent=cycle_intent,
        recharge_duration=recharge,
        cycle_discharge_duration=cycle_duration,
        cycle_state=cycle_state,
        cycle_deadline=cycle_deadline,
    )
    return RuntimeInputs(strategy, solis, reserve, current_window, next_window, maximum_charge_power, maximum_discharge_power, cycle_duration)


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise ValueError(f"required entity is unavailable: {entity_id}")
    return state


def _attribute(state: State, name: str) -> Any:
    value = state.attributes.get(name)
    if value in (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise ValueError(f"{state.entity_id} has no usable {name} attribute")
    return value


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("source timestamp must be ISO text")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("source timestamp must be timezone-aware")
    return result


def _rate_source(state: State, id_attr: str, retrieved_attr: str) -> RateSourceObservation:
    return RateSourceObservation(_parse_datetime(_attribute(state, retrieved_attr)), str(_attribute(state, id_attr)))


def _dispatch_source(state: State) -> DispatchSourceObservation:
    return DispatchSourceObservation(
        _parse_datetime(_attribute(state, "dispatch_source_last_retrieved")),
        str(_attribute(state, "dispatch_source_entity_id")),
    )


def _runtime_powers(snapshot: SolisStateSnapshot) -> tuple[Decimal, Decimal]:
    voltage = snapshot.telemetry.battery_voltage_v
    charge_current = snapshot.slots[0].charge.current.maximum
    discharge_current = min(snapshot.slots[0].discharge.current.maximum, snapshot.slots[1].discharge.current.maximum)
    charge_current = min(charge_current, snapshot.capabilities.maximum_charge_current.maximum)
    discharge_current = min(discharge_current, snapshot.capabilities.maximum_discharge_current.maximum)
    charge = voltage * charge_current / Decimal(1000)
    discharge = voltage * discharge_current / Decimal(1000)
    if not Decimal("0.5") <= charge <= Decimal("10") or not Decimal("0.5") <= discharge <= Decimal("10"):
        raise ValueError("derived runtime charge/discharge power is implausible")
    return charge, discharge


def _cycle_duration(state: State) -> timedelta:
    try:
        value = Decimal(str(state.state))
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError("cycle discharge duration is not numeric") from None
    if not value.is_finite() or value != value.to_integral_value() or not Decimal("1") <= value <= Decimal("60"):
        raise ValueError("cycle discharge duration must be an integer from 1 to 60 minutes")
    return timedelta(minutes=int(value))


def _quantize_target(requested: Decimal, minimum: Decimal, maximum: Decimal, step: Decimal) -> Decimal:
    if step <= 0 or minimum > maximum:
        raise ValueError("target capability is invalid")
    target = minimum + ((max(minimum, requested) - minimum) / step).to_integral_value(rounding=ROUND_CEILING) * step
    if target > maximum:
        raise ValueError("target capability cannot represent the reserve")
    return target


def _positive_margin(window: CheapWindow | None) -> bool:
    return window is not None and any(item.margin_per_stored_kwh > 0 for item in window.components)


async def _forecast_intervals(
    hass: HomeAssistant,
    config: Config,
    start: datetime,
    end: datetime,
    rates: Sequence[AdjustedRateInterval],
) -> tuple[ReserveInputInterval, ...]:
    from homeassistant.components.forecast_solar.energy import async_get_solar_forecast

    timezone_value = dt_util.get_time_zone(hass.config.time_zone)
    if timezone_value is None:
        raise ValueError("Home Assistant timezone is invalid")
    load_items = load.forecast(now=start, horizon_end=end, timezone=timezone_value)
    raw = await async_get_solar_forecast(hass, config.solar.config_entry_id)
    solar_items = _solar_intervals(raw)
    boundaries = {start, end}
    for items in (load_items, solar_items, rates):
        for item in items:
            interval = item.interval if hasattr(item, "interval") else TimeInterval(item.start, item.end)
            if start < interval.start < end:
                boundaries.add(interval.start)
            if start < interval.end < end:
                boundaries.add(interval.end)
    ordered = sorted(boundaries)
    result: list[ReserveInputInterval] = []
    for left, right in zip(ordered, ordered[1:]):
        interval = TimeInterval(left, right)
        rate = next((item for item in rates if item.start <= left and item.end >= right), None)
        if rate is None:
            raise ValueError("import tariff does not cover the reserve interval")
        result.append(
            ReserveInputInterval(
                interval,
                _energy(interval, load_items, required=True),
                _energy(interval, solar_items, required=False),
                rate.classification,
            )
        )
    return tuple(result)


def _solar_intervals(raw: Mapping[str, Any] | None) -> tuple[EnergyInterval, ...]:
    if not raw or not isinstance(raw.get("wh_hours"), Mapping):
        raise ValueError("Forecast.Solar response is unavailable")
    periods = sorted((_parse_datetime(str(stamp)), Decimal(str(value))) for stamp, value in raw["wh_hours"].items())
    if len(periods) < 2:
        raise ValueError("Forecast.Solar returned too few periods")
    result = []
    for index, (start, wh) in enumerate(periods):
        end = periods[index + 1][0] if index + 1 < len(periods) else start + (start - periods[index - 1][0])
        result.append(EnergyInterval(TimeInterval(start, end), wh / Decimal(1000)))
    return tuple(result)


def _energy(interval: TimeInterval, items: Sequence[EnergyInterval], *, required: bool) -> Decimal:
    total = Decimal(0)
    covered = timedelta(0)
    for item in items:
        left, right = max(interval.start, item.interval.start), min(interval.end, item.interval.end)
        if right <= left:
            continue
        overlap = right - left
        covered += overlap
        total += item.energy_kwh * Decimal(str(overlap.total_seconds())) / Decimal(str((item.interval.end - item.interval.start).total_seconds()))
    if required and covered != interval.end - interval.start:
        raise ValueError("load forecast does not cover the reserve interval")
    return total


def _soc_ceiling(energy: Decimal, capacity: Decimal) -> Decimal:
    return (energy * Decimal(FULL_SOC_PERCENT) / capacity).to_integral_value(rounding=ROUND_CEILING)


def _minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


__all__ = ["RuntimeInputs", "async_read_runtime_inputs"]
