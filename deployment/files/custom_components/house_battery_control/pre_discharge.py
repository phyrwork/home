"""Pure pre-discharge headroom strategy.

This module is intentionally a calculation boundary.  It accepts the
commissioned power evidence and trusted tariff records produced by T0012 and
T0013, and returns an auditable minute-schedule decision.  It does not know
about Home Assistant, SOC conversion, or Solis writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from typing import Any, Sequence

from .domain_constants import (
    BATTERY_CYCLE_COST_PER_KWH,
    MAXIMUM_GRID_IMPORT_POWER_KW,
)
from .interval import TimeInterval
from .octopus_windows import (
    AdjustedRateInterval,
    CheapClassification,
    CheapWindow,
    CoverageStatus,
    ExportRateInterval,
    RateSourceObservation,
    TrustedImportResult,
    DispatchSourceObservation,
    evaluate_trusted_import_rates,
)
from .reserve_planner import (
    CommissionedPowerEnvelope,
    ReserveAuthority,
    ReserveInputInterval,
    ReservePlanResult,
    ReservePlanningStatus,
)
from .solis_state import MAXIMUM_FUTURE_CLOCK_SKEW, MAXIMUM_TELEMETRY_AGE


class PreDischargePlanningStatus(str, Enum):
    PLANNED = "PLANNED"
    NO_HEADROOM_NEEDED = "NO_HEADROOM_NEEDED"
    UNPROFITABLE = "UNPROFITABLE"
    INFEASIBLE = "INFEASIBLE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class BatteryEnergyObservation:
    """A point-in-time stored-energy reading and its source revision."""

    stored_energy_kwh: Decimal
    observed_at: datetime
    source: str
    revision: str


@dataclass(frozen=True, slots=True)
class HeadroomIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class PreDischargePlanResult:
    status: PreDischargePlanningStatus
    desired_window_start_energy_kwh: Decimal | None = None
    baseline_window_start_energy_kwh: Decimal | None = None
    reachable_window_start_energy_kwh: Decimal | None = None
    planned_stored_withdrawal_kwh: Decimal = Decimal(0)
    planned_ac_export_kwh: Decimal = Decimal(0)
    uncreated_headroom_kwh: Decimal = Decimal(0)
    proposed_start: datetime | None = None
    proposed_end: datetime | None = None
    expiry: datetime | None = None
    conservative_margin_per_stored_kwh: Decimal | None = None
    conservative_value: Decimal | None = None
    input_fingerprint: str | None = None
    fresh_until: datetime | None = None
    issues: tuple[HeadroomIssue, ...] = ()

    @property
    def decision_fingerprint(self) -> str | None:
        return self.input_fingerprint


# Descriptive aliases make the domain boundary convenient for callers without
# multiplying contracts.
PreDischargeResult = PreDischargePlanResult
PreDischargeStatus = PreDischargePlanningStatus
EnergyObservation = BatteryEnergyObservation

_ZERO = Decimal(0)
_ONE = Decimal(1)
_MINUTE = timedelta(minutes=1)
_DAY = timedelta(days=1)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be Decimal-compatible") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _aware(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _hours(start: datetime, end: datetime) -> Decimal:
    delta = _utc(end) - _utc(start)
    micros = ((delta.days * 86400 + delta.seconds) * 1_000_000) + delta.microseconds
    return Decimal(micros) / Decimal(3_600_000_000)


def _delta_hours(hours: Decimal) -> timedelta:
    """Convert an exact Decimal hour quantity to a conservative duration."""
    micros = int((hours * Decimal(3_600_000_000)).to_integral_value())
    return timedelta(microseconds=micros)


def _issue(status: PreDischargePlanningStatus, code: str, detail: str) -> PreDischargePlanResult:
    return PreDischargePlanResult(status, issues=(HeadroomIssue(code, detail),))


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return {"utc": _utc(value).isoformat(), "fold": value.fold}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, TimeInterval):
        return {"start": _canonical(value.start), "end": _canonical(value.end)}
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _canonical(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    return value


def _fingerprint(**inputs: Any) -> str:
    payload = json.dumps(_canonical(inputs), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fresh(observed_at: datetime, now: datetime) -> tuple[datetime, HeadroomIssue | None]:
    if observed_at > now + MAXIMUM_FUTURE_CLOCK_SKEW:
        return observed_at, HeadroomIssue("ENERGY_IN_FUTURE", "energy observation is beyond the allowed clock skew")
    fresh_until = observed_at + MAXIMUM_TELEMETRY_AGE
    if now > fresh_until:
        return fresh_until, HeadroomIssue("ENERGY_STALE", "energy observation is stale")
    return fresh_until, None


def _validate_energy(energy: BatteryEnergyObservation, now: datetime) -> tuple[Decimal, datetime] | HeadroomIssue:
    if type(energy) is not BatteryEnergyObservation:
        return HeadroomIssue("ENERGY_INVALID", "energy observation has an unexpected concrete type")
    try:
        value = _decimal(energy.stored_energy_kwh, "stored_energy_kwh")
        observed = _aware(energy.observed_at, "observed_at")
        if value < _ZERO:
            raise ValueError("stored energy must not be negative")
        if not isinstance(energy.source, str) or not energy.source:
            raise ValueError("energy source must be non-empty")
        if not isinstance(energy.revision, str) or not energy.revision:
            raise ValueError("energy revision must be non-empty")
    except (TypeError, ValueError) as exc:
        return HeadroomIssue("ENERGY_INVALID", str(exc))
    fresh_until, issue = _fresh(observed, now)
    if issue:
        return issue
    return value, fresh_until


def _validate_intervals(intervals: Sequence[ReserveInputInterval], start: datetime, end: datetime) -> HeadroomIssue | None:
    if not isinstance(intervals, Sequence) or isinstance(intervals, (str, bytes)) or not intervals:
        return HeadroomIssue("FORECAST_INVALID", "forecast intervals must be a non-empty sequence")
    previous: ReserveInputInterval | None = None
    for item in intervals:
        if type(item) is not ReserveInputInterval:
            return HeadroomIssue("FORECAST_INVALID", "forecast interval has an unexpected concrete type")
        try:
            left, right = _aware(item.interval.start, "forecast start"), _aware(item.interval.end, "forecast end")
            if _utc(right) <= _utc(left):
                raise ValueError("forecast interval must be ordered")
            _decimal(item.load_kwh, "load_kwh")
            _decimal(item.solar_kwh, "solar_kwh")
            if previous is not None and _utc(left) != _utc(previous.interval.end):
                raise ValueError("forecast intervals must be contiguous")
        except (TypeError, ValueError) as exc:
            return HeadroomIssue("FORECAST_INVALID", str(exc))
        previous = item
    if _utc(intervals[0].interval.start) != _utc(start) or _utc(intervals[-1].interval.end) < _utc(end):
        return HeadroomIssue("FORECAST_COVERAGE", "forecast does not cover the requested strategy horizon")
    return None


def _reserve_boundaries(reserve_plan: ReservePlanResult, intervals: Sequence[ReserveInputInterval]) -> tuple[list[Decimal], list[Decimal]] | HeadroomIssue:
    if type(reserve_plan) is not ReservePlanResult or reserve_plan.status is not ReservePlanningStatus.COMPLETE:
        return HeadroomIssue("RESERVE_UNAVAILABLE", "complete feasible reserve trajectory is required")
    if len(reserve_plan.trajectory) != len(intervals):
        return HeadroomIssue("RESERVE_ALIGNMENT", "reserve trajectory does not align with forecast")
    starts: list[Decimal] = []
    ends: list[Decimal] = []
    for reserve, forecast in zip(reserve_plan.trajectory, intervals, strict=True):
        if _utc(reserve.interval.start) != _utc(forecast.interval.start) or _utc(reserve.interval.end) != _utc(forecast.interval.end):
            return HeadroomIssue("RESERVE_ALIGNMENT", "reserve trajectory boundaries do not match forecast")
        starts.append(_decimal(reserve.start_energy_kwh, "reserve start"))
        ends.append(_decimal(reserve.end_energy_kwh, "reserve end"))
    return starts, ends


def _reserve_for_intervals(
    reserve_plan: ReservePlanResult,
    source_intervals: Sequence[ReserveInputInterval],
    clipped_intervals: Sequence[ReserveInputInterval],
) -> tuple[list[Decimal], list[Decimal]] | HeadroomIssue:
    """Project a reserve trajectory onto exact partial forecast intervals."""
    if len(reserve_plan.trajectory) != len(source_intervals):
        return HeadroomIssue("RESERVE_ALIGNMENT", "reserve trajectory does not align with forecast")
    result_start: list[Decimal] = []
    result_end: list[Decimal] = []
    for clipped in clipped_intervals:
        matches = [
            (index, reserve)
            for index, reserve in enumerate(reserve_plan.trajectory)
            if _utc(clipped.interval.start) >= _utc(reserve.interval.start)
            and _utc(clipped.interval.end) <= _utc(reserve.interval.end)
        ]
        if len(matches) != 1:
            return HeadroomIssue("RESERVE_ALIGNMENT", "partial forecast interval is not covered by one reserve interval")
        index, reserve = matches[0]
        source = source_intervals[index]
        source_duration = _hours(source.interval.start, source.interval.end)
        if source_duration <= _ZERO:
            return HeadroomIssue("RESERVE_ALIGNMENT", "reserve interval has no duration")
        fraction_start = _hours(source.interval.start, clipped.interval.start) / source_duration
        fraction_end = _hours(source.interval.start, clipped.interval.end) / source_duration
        reserve_start = _decimal(reserve.start_energy_kwh, "reserve start")
        reserve_end = _decimal(reserve.end_energy_kwh, "reserve end")
        slope = reserve_end - reserve_start
        result_start.append(reserve_start + slope * fraction_start)
        result_end.append(reserve_start + slope * fraction_end)
    return result_start, result_end


def _clip_interval(item: ReserveInputInterval, start: datetime, end: datetime) -> ReserveInputInterval | None:
    left = max(_utc(item.interval.start), _utc(start))
    right = min(_utc(item.interval.end), _utc(end))
    if left >= right:
        return None
    # Forecast values are totals for the full interval.  Pro-rate them exactly
    # for a clipped partial interval.
    ratio = _hours(left, right) / _hours(item.interval.start, item.interval.end)
    return ReserveInputInterval(
        TimeInterval(left, right),
        _decimal(item.load_kwh, "load_kwh") * ratio,
        _decimal(item.solar_kwh, "solar_kwh") * ratio,
        item.classification,
    )


def _slice_forecast(intervals: Sequence[ReserveInputInterval], start: datetime, end: datetime) -> tuple[ReserveInputInterval, ...]:
    return tuple(item for source in intervals if (item := _clip_interval(source, start, end)) is not None)


def _forward_baseline(
    *,
    energy: Decimal,
    intervals: Sequence[ReserveInputInterval],
    reserve_starts: Sequence[Decimal],
    reserve_ends: Sequence[Decimal],
    capacity: Decimal,
    minimum: Decimal,
    charge_eff: Decimal,
    discharge_eff: Decimal,
    maximum_charge: Decimal,
    maximum_discharge: Decimal,
) -> tuple[list[Decimal], list[Decimal], list[Decimal], list[Decimal]] | HeadroomIssue:
    starts: list[Decimal] = []
    ends: list[Decimal] = []
    baseline_ac_discharge: list[Decimal] = []
    for index, item in enumerate(intervals):
        duration = _hours(item.interval.start, item.interval.end)
        current = energy
        starts.append(current)
        if current < reserve_starts[index]:
            return HeadroomIssue("RESERVE_BREACH", "baseline trajectory starts below the commissioned reserve")
        deficit = _decimal(item.load_kwh, "load_kwh") - _decimal(item.solar_kwh, "solar_kwh")
        if deficit > _ZERO:
            grid = min(deficit, MAXIMUM_GRID_IMPORT_POWER_KW * duration)
            battery_ac = deficit - grid
            if battery_ac > maximum_discharge * duration:
                return HeadroomIssue("DISCHARGE_POWER_EXCEEDED", "baseline household demand exceeds commissioned discharge output")
            current -= battery_ac / discharge_eff
            baseline_ac_discharge.append(battery_ac / duration)
        else:
            pv_ac = min(-deficit, maximum_charge * duration)
            current += pv_ac * charge_eff
            baseline_ac_discharge.append(_ZERO)
        if current < minimum or current > capacity:
            return HeadroomIssue("ENERGY_BOUNDARY", "baseline trajectory crosses a physical energy boundary")
        if current < reserve_ends[index]:
            return HeadroomIssue("RESERVE_BREACH", "baseline trajectory is below the commissioned reserve")
        ends.append(current)
        energy = current
    return starts, ends, baseline_ac_discharge, list(energy for energy in ends)


def _window_import_rates(
    window: CheapWindow,
    trusted_import: TrustedImportResult,
    import_source: RateSourceObservation | None,
    dispatch_source: DispatchSourceObservation | None,
    now: datetime,
) -> tuple[tuple[AdjustedRateInterval, ...], HeadroomIssue | None]:
    if type(window) is not CheapWindow or not window.components:
        return (), HeadroomIssue("WINDOW_UNAVAILABLE", "a complete standard-cheap window is required")
    if trusted_import is None or trusted_import.coverage_status is not CoverageStatus.COMPLETE:
        return (), HeadroomIssue("IMPORT_UNAVAILABLE", "trusted import coverage is incomplete")
    if import_source is None or not isinstance(import_source.source, str) or not import_source.source:
        return (), HeadroomIssue("IMPORT_UNAVAILABLE", "import source observation is unavailable")
    if not isinstance(import_source.retrieved_at, datetime) or now - import_source.retrieved_at > timedelta(hours=26):
        return (), HeadroomIssue("IMPORT_UNAVAILABLE", "import source observation is stale")
    if import_source.retrieved_at > now + MAXIMUM_FUTURE_CLOCK_SKEW:
        return (), HeadroomIssue("IMPORT_UNAVAILABLE", "import source observation is in the future")
    evaluated = evaluate_trusted_import_rates(
        import_rates=trusted_import.intervals,
        start=window.start,
        end=window.end,
        now=now,
        import_source=import_source,
        dispatch_source=dispatch_source,
    )
    if evaluated.coverage_status is CoverageStatus.INVALID:
        return (), HeadroomIssue("IMPORT_INVALID", "; ".join(evaluated.issues))
    if evaluated.coverage_status is not CoverageStatus.COMPLETE:
        return (), HeadroomIssue("IMPORT_UNAVAILABLE", "; ".join(evaluated.issues))
    selected: list[AdjustedRateInterval] = []
    cursor = window.start
    for component in window.components:
        rate = component.rate_interval
        if rate.classification is not CheapClassification.STANDARD_CHEAP:
            return (), HeadroomIssue("BONUS_NOT_ALLOWED", "pre-discharge is restricted to STANDARD_CHEAP")
        if rate.retrieval_source_entity_id != import_source.source:
            return (), HeadroomIssue("IMPORT_PROVENANCE", "import rate source does not match observation")
        if _utc(component.interval.start) != _utc(cursor):
            return (), HeadroomIssue("WINDOW_INVALID", "standard-cheap components are not contiguous")
        if _utc(component.interval.end) <= _utc(component.interval.start):
            return (), HeadroomIssue("WINDOW_INVALID", "standard-cheap window is empty")
        selected.append(rate)
        cursor = component.interval.end
    if _utc(cursor) != _utc(window.end):
        return (), HeadroomIssue("WINDOW_INVALID", "standard-cheap components do not cover their window")
    for rate in selected:
        matches = [item for item in trusted_import.intervals if _utc(item.start) == _utc(rate.start) and _utc(item.end) == _utc(rate.end)]
        if len(matches) != 1 or matches[0] != rate:
            return (), HeadroomIssue("IMPORT_ALIGNMENT", "window rate is not bound to trusted import provenance")
    return tuple(selected), None


def _export_coverage(
    rates: Sequence[ExportRateInterval],
    start: datetime,
    end: datetime,
    source: RateSourceObservation | None,
    now: datetime,
) -> tuple[tuple[ExportRateInterval, ...], HeadroomIssue | None]:
    if start >= end:
        return (), HeadroomIssue("EXPORT_UNAVAILABLE", "discharge interval is empty")
    if source is None or not isinstance(source.source, str) or not source.source:
        return (), HeadroomIssue("EXPORT_UNAVAILABLE", "export source observation is unavailable")
    if not isinstance(source.retrieved_at, datetime) or now - source.retrieved_at > timedelta(hours=26):
        return (), HeadroomIssue("EXPORT_UNAVAILABLE", "export source observation is stale")
    if source.retrieved_at > now + MAXIMUM_FUTURE_CLOCK_SKEW:
        return (), HeadroomIssue("EXPORT_UNAVAILABLE", "export source observation is in the future")
    usable: list[ExportRateInterval] = []
    cursor = _utc(start)
    for rate in sorted(rates, key=lambda item: _utc(item.start)):
        if rate.retrieval_source_entity_id != source.source:
            return (), HeadroomIssue("EXPORT_PROVENANCE", "export rate source does not match observation")
        if rate.retrieved_at != source.retrieved_at:
            return (), HeadroomIssue("EXPORT_PROVENANCE", "export rate retrieval timestamp does not match observation")
        left, right = max(_utc(rate.start), _utc(start)), min(_utc(rate.end), _utc(end))
        if left >= right:
            continue
        if left != cursor:
            return (), HeadroomIssue("EXPORT_COVERAGE", "export coverage has a gap or overlap")
        usable.append(rate)
        cursor = right
    if cursor < _utc(end):
        return (), HeadroomIssue("EXPORT_COVERAGE", "export coverage does not span the planned discharge")
    return tuple(usable), None


def _wall_safe(start: datetime, end: datetime) -> HeadroomIssue | None:
    if start.fold or end.fold:
        return HeadroomIssue("DST_AMBIGUOUS", "ambiguous local wall time is not schedule representable")
    if start.utcoffset() != end.utcoffset():
        return HeadroomIssue("DST_TRANSITION", "schedule interval crosses a daylight-saving transition")
    for value, label in ((start, "start"), (end, "end")):
        roundtrip = value.astimezone(timezone.utc).astimezone(value.tzinfo)
        if roundtrip.replace(fold=0) != value.replace(fold=0):
            return HeadroomIssue("DST_NONEXISTENT", f"{label} local wall time is nonexistent")
    return None


def _minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _allocate_latest(
    *,
    intervals: Sequence[ReserveInputInterval],
    baseline_starts: Sequence[Decimal],
    baseline_ends: Sequence[Decimal],
    reserve_starts: Sequence[Decimal],
    reserve_ends: Sequence[Decimal],
    baseline_ac: Sequence[Decimal],
    target: Decimal,
    maximum_discharge: Decimal,
    discharge_eff: Decimal,
) -> tuple[Decimal, datetime | None, datetime | None, Decimal]:
    remaining = target
    used = _ZERO
    start: datetime | None = None
    end: datetime | None = None
    for index in range(len(intervals) - 1, -1, -1):
        item = intervals[index]
        duration = _hours(item.interval.start, item.interval.end)
        output_rate = max(_ZERO, maximum_discharge - baseline_ac[index])
        stored_rate = output_rate / discharge_eff
        if stored_rate <= _ZERO:
            continue
        available_end = baseline_ends[index] - reserve_ends[index]
        available_start = baseline_starts[index] - reserve_starts[index]
        available = max(_ZERO, min(available_end, available_start))
        possible = min(available, stored_rate * duration)
        amount = min(remaining, possible)
        if amount <= _ZERO:
            continue
        # Allocate the tail of this interval, retaining the exact proportional
        # rule for partial forecast intervals.
        span = amount / stored_rate
        piece_start = item.interval.end - _delta_hours(span)
        if span == duration:
            piece_start = item.interval.start
        piece_end = item.interval.end
        start = piece_start
        end = piece_end if end is None else end
        used += amount
        remaining -= amount
        if remaining <= _ZERO:
            break
    return used, start, end, remaining


def plan_pre_discharge_headroom(
    *,
    energy: BatteryEnergyObservation,
    forecast_intervals: Sequence[ReserveInputInterval],
    reserve_plan: ReservePlanResult,
    standard_window: CheapWindow,
    trusted_import: TrustedImportResult,
    export_rates: Sequence[ExportRateInterval],
    import_source: RateSourceObservation | None,
    export_source: RateSourceObservation | None,
    dispatch_source: DispatchSourceObservation | None = None,
    commissioned: CommissionedPowerEnvelope | None,
    authority: ReserveAuthority | None,
    now: datetime,
    capacity_kwh: Any,
    minimum_energy_kwh: Any,
    reserve_margin_kwh: Any,
    charge_efficiency: Any,
    discharge_efficiency: Any,
    cycle_cost_per_kwh: Any = BATTERY_CYCLE_COST_PER_KWH,
) -> PreDischargePlanResult:
    """Plan the latest safe, profitable standard-window pre-discharge."""
    try:
        now = _aware(now, "now")
        capacity = _decimal(capacity_kwh, "capacity_kwh")
        minimum = _decimal(minimum_energy_kwh, "minimum_energy_kwh")
        margin = _decimal(reserve_margin_kwh, "reserve_margin_kwh")
        charge_eff = _decimal(charge_efficiency, "charge_efficiency")
        discharge_eff = _decimal(discharge_efficiency, "discharge_efficiency")
        cycle_cost = _decimal(cycle_cost_per_kwh, "cycle_cost_per_kwh")
        if capacity < _ZERO or minimum < _ZERO or minimum > capacity or margin < _ZERO:
            raise ValueError("energy bounds are invalid")
        if not (_ZERO < charge_eff <= _ONE and _ZERO < discharge_eff <= _ONE):
            raise ValueError("efficiencies must be greater than zero and at most one")
        if cycle_cost < _ZERO:
            raise ValueError("cycle cost must not be negative")
        window_start, window_end = _aware(standard_window.start, "window start"), _aware(standard_window.end, "window end")
        if _utc(window_end) <= _utc(window_start):
            raise ValueError("standard window must be ordered")
    except (TypeError, ValueError) as exc:
        return _issue(PreDischargePlanningStatus.INVALID, "INPUT_INVALID", str(exc))

    energy_value = _validate_energy(energy, now)
    if isinstance(energy_value, HeadroomIssue):
        status = PreDischargePlanningStatus.UNAVAILABLE if energy_value.code in {"ENERGY_STALE", "ENERGY_IN_FUTURE"} else PreDischargePlanningStatus.INVALID
        return _issue(status, energy_value.code, energy_value.detail)
    observed_kwh, fresh_until = energy_value
    if _utc(energy.observed_at) > _utc(window_start):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, "ENERGY_AFTER_WINDOW_START", "energy observation is after the window start")
    forecast_issue = _validate_intervals(forecast_intervals, energy.observed_at, window_start)
    if forecast_issue:
        return _issue(PreDischargePlanningStatus.INVALID, forecast_issue.code, forecast_issue.detail)
    # Reserve trajectories may include the refill window.  For this strategy,
    # only boundaries through the window start are needed for the backward
    # overlay, but all supplied material inputs remain in the fingerprint.
    # The forecast may also contain refill intervals after the window start.
    # Clip it to the pre-window horizon and project the reserve trajectory with
    # exact linear interpolation for a partial boundary.
    pre_window_intervals = _slice_forecast(forecast_intervals, energy.observed_at, window_start)
    reserve_bounds = _reserve_for_intervals(reserve_plan, forecast_intervals, pre_window_intervals)
    if isinstance(reserve_bounds, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, reserve_bounds.code, reserve_bounds.detail)
    reserve_starts, reserve_ends = reserve_bounds
    if not pre_window_intervals:
        return _issue(PreDischargePlanningStatus.INFEASIBLE, "NO_PREWINDOW_TIME", "no pre-window forecast is available")
    if commissioned is None or authority is None:
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, "COMMISSIONING_MISSING", "commissioned power evidence is required")
    try:
        if type(commissioned) is not CommissionedPowerEnvelope or type(authority) is not ReserveAuthority:
            return _issue(PreDischargePlanningStatus.INVALID, "COMMISSIONING_INVALID", "commissioning authority has an unexpected concrete type")
        if commissioned.maximum_grid_import_power_kw != MAXIMUM_GRID_IMPORT_POWER_KW:
            return _issue(PreDischargePlanningStatus.UNAVAILABLE, "GRID_IMPORT_MISMATCH", "commissioned grid contribution does not equal MAXIMUM_GRID_IMPORT_POWER_KW")
        maximum_charge = _decimal(commissioned.maximum_charge_power_kw, "maximum_charge_power_kw")
        maximum_discharge = _decimal(commissioned.maximum_discharge_power_kw, "maximum_discharge_power_kw")
        if maximum_charge < _ZERO or maximum_discharge < _ZERO:
            raise ValueError("commissioned powers must not be negative")
        validated_at = _aware(commissioned.validated_at, "commissioned validated_at")
        if _utc(validated_at) > _utc(now):
            return _issue(PreDischargePlanningStatus.INVALID, "COMMISSIONING_INVALID", "commissioning evidence is in the future")
        for name in ("schema_version", "inverter_identity", "mapping_fingerprint", "candidate_policy_fingerprint", "manual_grid_fingerprint", "capability_fingerprint"):
            if not isinstance(getattr(commissioned, name), str) or not getattr(commissioned, name):
                return _issue(PreDischargePlanningStatus.INVALID, "COMMISSIONING_INVALID", f"commissioned {name} must be non-empty")
            if not isinstance(getattr(authority, name), str) or not getattr(authority, name):
                return _issue(PreDischargePlanningStatus.INVALID, "COMMISSIONING_INVALID", f"current {name} must be non-empty")
            if getattr(commissioned, name) != getattr(authority, name):
                return _issue(PreDischargePlanningStatus.UNAVAILABLE, "AUTHORITY_MISMATCH", f"{name} does not match commissioning evidence")
    except (AttributeError, TypeError, ValueError) as exc:
        return _issue(PreDischargePlanningStatus.INVALID, "COMMISSIONING_INVALID", str(exc))

    import_rates, issue = _window_import_rates(standard_window, trusted_import, import_source, dispatch_source, now)
    if issue:
        status = PreDischargePlanningStatus.UNAVAILABLE if issue.code.endswith("UNAVAILABLE") or issue.code in {"IMPORT_COVERAGE", "IMPORT_PROVENANCE"} else PreDischargePlanningStatus.INVALID
        return _issue(status, issue.code, issue.detail)
    # A standard-only window is also required to have source coverage exactly
    # over its refill duration, not merely a candidate component.
    if not import_rates:
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, "IMPORT_UNAVAILABLE", "refill import coverage is empty")

    baseline_result = _forward_baseline(
        energy=observed_kwh,
        intervals=pre_window_intervals,
        reserve_starts=reserve_starts,
        reserve_ends=reserve_ends,
        capacity=capacity,
        minimum=minimum,
        charge_eff=charge_eff,
        discharge_eff=discharge_eff,
        maximum_charge=maximum_charge,
        maximum_discharge=maximum_discharge,
    )
    if isinstance(baseline_result, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.INFEASIBLE, baseline_result.code, baseline_result.detail)
    baseline_starts, baseline_ends, baseline_ac, _ = baseline_result
    baseline_window_start = baseline_ends[-1]
    reserve_window_start = reserve_ends[-1]
    protected_floor = minimum + margin
    maximum_stored_refill = maximum_charge * _hours(window_start, window_end) * charge_eff
    desired = max(protected_floor, reserve_window_start, capacity - maximum_stored_refill)
    desired_withdrawal = max(_ZERO, baseline_window_start - desired)
    common_inputs = dict(
        schema="T0014-pre-discharge-headroom-v1",
        energy=energy,
        forecast_intervals=forecast_intervals,
        reserve_plan=reserve_plan,
        standard_window=standard_window,
        trusted_import=trusted_import,
        import_rates=import_rates,
        export_rates=export_rates,
        import_source=import_source,
        export_source=export_source,
        dispatch_source=dispatch_source,
        commissioned=commissioned,
        authority=authority,
        now=now,
        capacity_kwh=capacity,
        minimum_energy_kwh=minimum,
        reserve_margin_kwh=margin,
        charge_efficiency=charge_eff,
        discharge_efficiency=discharge_eff,
        cycle_cost_per_kwh=cycle_cost,
    )
    fingerprint = _fingerprint(**common_inputs)
    if desired_withdrawal == _ZERO:
        return PreDischargePlanResult(
            PreDischargePlanningStatus.NO_HEADROOM_NEEDED,
            desired_window_start_energy_kwh=desired,
            baseline_window_start_energy_kwh=baseline_window_start,
            reachable_window_start_energy_kwh=baseline_window_start,
            input_fingerprint=fingerprint,
            expiry=fresh_until,
            fresh_until=fresh_until,
        )

    # Quantize the end down first.  This is intentionally conservative: any
    # fractional cheap-window start is left free of discharge.
    quantized_end = _minute_floor(window_start)
    pre_window = _slice_forecast(forecast_intervals, energy.observed_at, quantized_end)
    if not pre_window:
        return _issue(PreDischargePlanningStatus.INFEASIBLE, "NO_PREWINDOW_TIME", "no minute-representable pre-window time remains")
    # Use the full-interval boundary arrays for aligned forecasts.  The normal
    # Solis/OCTOPUS path is minute-aligned; partial boundary support is handled
    # by the exact clipping and the conservative endpoint proof below.
    quantized_reserves = _reserve_for_intervals(reserve_plan, forecast_intervals, pre_window)
    if isinstance(quantized_reserves, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, quantized_reserves.code, quantized_reserves.detail)
    quantized_reserve_starts, quantized_reserve_ends = quantized_reserves
    prefix_intervals = _slice_forecast(forecast_intervals, energy.observed_at, quantized_end)
    prefix_reserves = _reserve_for_intervals(reserve_plan, forecast_intervals, prefix_intervals)
    if isinstance(prefix_reserves, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, prefix_reserves.code, prefix_reserves.detail)
    prefix_starts, prefix_ends = prefix_reserves
    prefix_baseline = _forward_baseline(
        energy=observed_kwh,
        intervals=prefix_intervals,
        reserve_starts=prefix_starts,
        reserve_ends=prefix_ends,
        capacity=capacity,
        minimum=minimum,
        charge_eff=charge_eff,
        discharge_eff=discharge_eff,
        maximum_charge=maximum_charge,
        maximum_discharge=maximum_discharge,
    )
    if isinstance(prefix_baseline, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.INFEASIBLE, prefix_baseline.code, prefix_baseline.detail)
    prefix_baseline_starts, prefix_baseline_ends, prefix_baseline_ac, _ = prefix_baseline
    # Locate the exact suffix represented by the quantized discharge interval.
    suffix_offset = len(prefix_intervals) - len(pre_window)
    allocation = _allocate_latest(
        intervals=pre_window,
        baseline_starts=prefix_baseline_starts[suffix_offset:],
        baseline_ends=prefix_baseline_ends[suffix_offset:],
        reserve_starts=quantized_reserve_starts,
        reserve_ends=quantized_reserve_ends,
        baseline_ac=prefix_baseline_ac[suffix_offset:],
        target=desired_withdrawal,
        maximum_discharge=maximum_discharge,
        discharge_eff=discharge_eff,
    )
    reachable_withdrawal, proposed_start, proposed_end, uncreated = allocation
    if reachable_withdrawal <= _ZERO or proposed_start is None or proposed_end is None:
        return _issue(PreDischargePlanningStatus.INFEASIBLE, "NO_REACHABLE_HEADROOM", "reserve or commissioned output leaves no safe export interval")
    # Round the start earlier, then repeat allocation in the rounded interval.
    quantized_start = proposed_start.replace(second=0, microsecond=0)
    quantized_end = min(_minute_floor(proposed_end), _minute_floor(window_start))
    if _utc(quantized_end) <= _utc(quantized_start) or quantized_end - quantized_start >= _DAY:
        return _issue(PreDischargePlanningStatus.INVALID, "SCHEDULE_NOT_REPRESENTABLE", "quantized interval is empty or at least 24 hours")
    wall_issue = _wall_safe(quantized_start, quantized_end)
    if wall_issue:
        return _issue(PreDischargePlanningStatus.INVALID, wall_issue.code, wall_issue.detail)
    # Re-run the complete forward proof and latest-start allocation after both
    # boundary quantizations.  This prevents a rounded schedule from relying
    # on energy or reserve that existed only in a fractional interval.
    final_intervals = _slice_forecast(forecast_intervals, quantized_start, quantized_end)
    final_reserves = _reserve_for_intervals(reserve_plan, forecast_intervals, final_intervals)
    if isinstance(final_reserves, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, final_reserves.code, final_reserves.detail)
    final_reserve_starts, final_reserve_ends = final_reserves
    before_intervals = _slice_forecast(forecast_intervals, energy.observed_at, quantized_start)
    before_reserves = _reserve_for_intervals(reserve_plan, forecast_intervals, before_intervals)
    if isinstance(before_reserves, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, before_reserves.code, before_reserves.detail)
    before_reserve_starts, before_reserve_ends = before_reserves
    before_baseline = _forward_baseline(
        energy=observed_kwh,
        intervals=before_intervals,
        reserve_starts=before_reserve_starts,
        reserve_ends=before_reserve_ends,
        capacity=capacity,
        minimum=minimum,
        charge_eff=charge_eff,
        discharge_eff=discharge_eff,
        maximum_charge=maximum_charge,
        maximum_discharge=maximum_discharge,
    )
    if isinstance(before_baseline, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.INFEASIBLE, before_baseline.code, before_baseline.detail)
    final_start_energy = before_baseline[1][-1] if before_baseline[1] else observed_kwh
    final_baseline = _forward_baseline(
        energy=final_start_energy,
        intervals=final_intervals,
        reserve_starts=final_reserve_starts,
        reserve_ends=final_reserve_ends,
        capacity=capacity,
        minimum=minimum,
        charge_eff=charge_eff,
        discharge_eff=discharge_eff,
        maximum_charge=maximum_charge,
        maximum_discharge=maximum_discharge,
    )
    if isinstance(final_baseline, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.INFEASIBLE, final_baseline.code, final_baseline.detail)
    final_allocation = _allocate_latest(
        intervals=final_intervals,
        baseline_starts=final_baseline[0],
        baseline_ends=final_baseline[1],
        reserve_starts=final_reserve_starts,
        reserve_ends=final_reserve_ends,
        baseline_ac=final_baseline[2],
        target=desired_withdrawal,
        maximum_discharge=maximum_discharge,
        discharge_eff=discharge_eff,
    )
    reachable_withdrawal, final_start, final_end, uncreated = final_allocation
    if reachable_withdrawal <= _ZERO or final_start is None or final_end is None:
        return _issue(PreDischargePlanningStatus.INFEASIBLE, "NO_REACHABLE_HEADROOM", "rounded interval leaves no safe export capacity")
    # The allocation is now exact for the quantized interval.  Restrict
    # economic authorization to that interval and desired energy.
    proposed_start = final_start
    proposed_end = final_end
    planned = min(reachable_withdrawal, desired_withdrawal)
    exports, issue = _export_coverage(export_rates, quantized_start, quantized_end, export_source, now)
    if issue:
        status = PreDischargePlanningStatus.UNAVAILABLE if issue.code.endswith("UNAVAILABLE") or issue.code in {"EXPORT_COVERAGE", "EXPORT_PROVENANCE"} else PreDischargePlanningStatus.INVALID
        return _issue(status, issue.code, issue.detail)
    margins = [
        export.export_price * discharge_eff - rate.import_price / charge_eff - cycle_cost
        for export in exports
        for rate in import_rates
    ]
    if not margins:
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, "PRICE_COVERAGE", "no complete export/refill price pair exists")
    conservative_margin = min(margins)
    if conservative_margin <= _ZERO:
        return _issue(PreDischargePlanningStatus.UNPROFITABLE, "MARGIN_NOT_POSITIVE", "worst-case export/refill margin is not strictly positive")
    planned_ac = planned * discharge_eff
    return PreDischargePlanResult(
        PreDischargePlanningStatus.PLANNED,
        desired_window_start_energy_kwh=desired,
        baseline_window_start_energy_kwh=baseline_window_start,
        reachable_window_start_energy_kwh=baseline_window_start - planned,
        planned_stored_withdrawal_kwh=planned,
        planned_ac_export_kwh=planned_ac,
        uncreated_headroom_kwh=max(_ZERO, desired_withdrawal - planned),
        # The schedule begins at the earlier minute boundary.  The target
        # energy stop may terminate it before the fractional latest-start
        # point, but the proof above includes this extra minute.
        proposed_start=quantized_start,
        proposed_end=quantized_end,
        expiry=fresh_until,
        conservative_margin_per_stored_kwh=conservative_margin,
        conservative_value=conservative_margin * planned,
        input_fingerprint=fingerprint,
        fresh_until=fresh_until,
    )


commissioned_pre_discharge_headroom = plan_pre_discharge_headroom

__all__ = [
    "BatteryEnergyObservation",
    "EnergyObservation",
    "HeadroomIssue",
    "PreDischargePlanResult",
    "PreDischargePlanningStatus",
    "PreDischargeResult",
    "PreDischargeStatus",
    "commissioned_pre_discharge_headroom",
    "plan_pre_discharge_headroom",
]
