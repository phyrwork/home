"""Pure pre-discharge headroom strategy.

This module is intentionally a calculation boundary.  It accepts the
commissioned power evidence and trusted tariff records produced by T0012 and
T0013, and returns an auditable minute-schedule decision.  It does not know
about Home Assistant, SOC conversion, or Solis writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from typing import Any, Sequence

from .domain_constants import (
    BATTERY_CYCLE_COST_PER_KWH,
    FORECAST_SOURCE_MAX_AGE,
    MAXIMUM_GRID_IMPORT_POWER_KW,
    MAXIMUM_SOURCE_FUTURE_SKEW,
    OCTOPUS_DISPATCH_SOURCE_MAX_AGE,
    OCTOPUS_EXPORT_SOURCE_MAX_AGE,
    OCTOPUS_RATE_SOURCE_MAX_AGE,
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
    plan_commissioned_reserve,
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
    family: str = "solis.telemetry.stored_energy"


@dataclass(frozen=True, slots=True)
class EnergyObservationAuthority:
    """T0004-bound identity for a stored-energy observation.

    The reader/coordinator supplies these values from the authoritative T0004
    snapshot.  A strategy must never accept an arbitrary non-empty source as
    proof of battery energy.
    """

    source: str
    family: str
    revision: str


@dataclass(frozen=True, slots=True)
class ForecastObservation:
    """Immutable provenance for the load/external-PV forecast pair."""

    source: str
    revision: str
    retrieved_at: datetime
    fresh_until: datetime


@dataclass(frozen=True, slots=True)
class HeadroomIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ScheduleLedgerEntry:
    """One exact simulated segment of the encoded continuous slot."""

    start: datetime
    end: datetime
    start_energy_kwh: Decimal
    end_energy_kwh: Decimal
    discretionary_ac_export_kwh: Decimal


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
    schedule_ledger: tuple[ScheduleLedgerEntry, ...] = ()
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
    """Convert hours to a duration, rounding outward for safety."""
    # A duration is used to choose the earlier start of a discharge.  Ceiling
    # the sub-microsecond remainder prevents silently losing a required slice
    # of reserve when Decimal and datetime resolutions differ.
    from decimal import ROUND_CEILING
    micros = int((hours * Decimal(3_600_000_000)).to_integral_value(rounding=ROUND_CEILING))
    return timedelta(microseconds=micros)


def _issue(status: PreDischargePlanningStatus, code: str, detail: str) -> PreDischargePlanResult:
    return PreDischargePlanResult(status, issues=(HeadroomIssue(code, detail),))


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return {"utc": _utc(value).isoformat(), "fold": value.fold}
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, tzinfo):
        return str(value)
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
        if not isinstance(energy.family, str) or not energy.family:
            raise ValueError("energy family must be non-empty")
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
            load = _decimal(item.load_kwh, "load_kwh")
            solar = _decimal(item.solar_kwh, "solar_kwh")
            if solar < _ZERO:
                raise ValueError("solar_kwh must not be negative")
            if type(item.classification) is not CheapClassification:
                raise ValueError("forecast classification is not allowlisted")
            if previous is not None and _utc(left) != _utc(previous.interval.end):
                raise ValueError("forecast intervals must be contiguous")
        except (TypeError, ValueError) as exc:
            return HeadroomIssue("FORECAST_INVALID", str(exc))
        previous = item
    if _utc(intervals[0].interval.start) != _utc(start) or _utc(intervals[-1].interval.end) < _utc(end):
        return HeadroomIssue("FORECAST_COVERAGE", "forecast does not cover the requested strategy horizon")
    return None


def _validate_forecast_observation(
    observation: ForecastObservation | None,
    *,
    now: datetime,
) -> tuple[datetime, HeadroomIssue | None]:
    """Validate forecast provenance without trusting caller-supplied freshness."""
    if observation is None:
        return now, HeadroomIssue("FORECAST_UNAVAILABLE", "forecast observation authority is required")
    if type(observation) is not ForecastObservation:
        return now, HeadroomIssue("FORECAST_INVALID", "forecast observation has an unexpected concrete type")
    try:
        retrieved = _aware(observation.retrieved_at, "forecast retrieved_at")
        fresh_until = _aware(observation.fresh_until, "forecast fresh_until")
        if not isinstance(observation.source, str) or not observation.source:
            raise ValueError("forecast source must be non-empty")
        if not isinstance(observation.revision, str) or not observation.revision:
            raise ValueError("forecast revision must be non-empty")
        if retrieved > now + MAXIMUM_SOURCE_FUTURE_SKEW:
            raise ValueError("forecast retrieval is in the future")
        if fresh_until < retrieved:
            raise ValueError("forecast freshness deadline precedes retrieval")
        if fresh_until != retrieved + FORECAST_SOURCE_MAX_AGE:
            raise ValueError("forecast freshness deadline does not match FORECAST_SOURCE_MAX_AGE")
    except (TypeError, ValueError) as exc:
        return now, HeadroomIssue("FORECAST_INVALID", str(exc))
    if now > fresh_until:
        return fresh_until, HeadroomIssue("FORECAST_STALE", "forecast observation is stale")
    return fresh_until, None


def _validate_energy_authority(
    energy: BatteryEnergyObservation,
    authority: EnergyObservationAuthority | None,
) -> HeadroomIssue | None:
    if type(authority) is not EnergyObservationAuthority:
        return HeadroomIssue("ENERGY_UNAVAILABLE", "T0004 energy observation authority is required")
    try:
        for label in ("source", "family", "revision"):
            value = getattr(authority, label)
            if not isinstance(value, str) or not value:
                raise ValueError(f"energy authority {label} must be non-empty")
        if energy.source != authority.source:
            raise ValueError("energy source does not match T0004 authority")
        if energy.family != authority.family:
            raise ValueError("energy family does not match T0004 authority")
        if energy.revision != authority.revision:
            raise ValueError("energy revision does not match T0004 authority")
    except (AttributeError, TypeError, ValueError) as exc:
        return HeadroomIssue("ENERGY_INVALID", str(exc))
    return None


def _source_until(
    source: RateSourceObservation | None,
    now: datetime,
    max_age: timedelta,
    label: str,
) -> tuple[datetime, HeadroomIssue | None]:
    if source is None or type(source) is not RateSourceObservation:
        return now, HeadroomIssue(f"{label.upper()}_UNAVAILABLE", f"{label} source observation is unavailable")
    try:
        retrieved = _aware(source.retrieved_at, f"{label} retrieved_at")
        if not isinstance(source.source, str) or not source.source:
            raise ValueError(f"{label} source must be non-empty")
        if retrieved > now + MAXIMUM_SOURCE_FUTURE_SKEW:
            raise ValueError(f"{label} source observation is in the future")
    except (TypeError, ValueError) as exc:
        return now, HeadroomIssue(f"{label.upper()}_INVALID", str(exc))
    fresh_until = retrieved + max_age
    if now > fresh_until:
        return fresh_until, HeadroomIssue(f"{label.upper()}_STALE", f"{label} source observation is stale")
    return fresh_until, None


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
    if not isinstance(import_source.retrieved_at, datetime) or now - import_source.retrieved_at > OCTOPUS_RATE_SOURCE_MAX_AGE:
        return (), HeadroomIssue("IMPORT_UNAVAILABLE", "import source observation is stale")
    if import_source.retrieved_at > now + MAXIMUM_SOURCE_FUTURE_SKEW:
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
    if not isinstance(source.retrieved_at, datetime) or now - source.retrieved_at > OCTOPUS_EXPORT_SOURCE_MAX_AGE:
        return (), HeadroomIssue("EXPORT_UNAVAILABLE", "export source observation is stale")
    if source.retrieved_at > now + MAXIMUM_SOURCE_FUTURE_SKEW:
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


def _wall_safe(start: datetime, end: datetime, zone: tzinfo | None = None) -> HeadroomIssue | None:
    """Reject ambiguous/nonexistent endpoints and transitions in a slot."""
    zone = zone or start.tzinfo
    if zone is None:
        return HeadroomIssue("DST_INVALID", "schedule timezone is missing")
    left, right = start.astimezone(zone), end.astimezone(zone)

    def classify(value: datetime, label: str) -> HeadroomIssue | None:
        naive = value.replace(tzinfo=None)
        candidates = (naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1))
        valid = [candidate for candidate in candidates if candidate.astimezone(timezone.utc).astimezone(zone).replace(fold=0) == candidate.replace(fold=0)]
        if not valid:
            return HeadroomIssue("DST_NONEXISTENT", f"{label} local wall time is nonexistent")
        if len({candidate.utcoffset() for candidate in valid}) > 1 or value.fold:
            return HeadroomIssue("DST_AMBIGUOUS", f"{label} local wall time is ambiguous")
        return None

    for value, label in ((left, "start"), (right, "end")):
        issue = classify(value, label)
        if issue:
            return issue
    if _utc(right) <= _utc(left):
        return HeadroomIssue("SCHEDULE_INVALID", "schedule interval is not ordered")
    cursor = left
    while cursor < right:
        probe = min(cursor + timedelta(minutes=1), right)
        if probe.utcoffset() != left.utcoffset():
            return HeadroomIssue("DST_TRANSITION", "schedule interval crosses a daylight-saving transition")
        cursor = probe
    return None


def _minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _minute_ceil(value: datetime) -> datetime:
    floor = _minute_floor(value)
    return floor if floor == value else floor + _MINUTE


@dataclass(frozen=True, slots=True)
class _Simulation:
    safe: bool
    withdrawal_kwh: Decimal
    target_stop: datetime | None
    ledger: tuple[tuple[datetime, datetime, Decimal, Decimal, Decimal], ...]
    issue: HeadroomIssue | None = None


def _reserve_at(reserve_plan: ReservePlanResult, intervals: Sequence[ReserveInputInterval], instant: datetime) -> Decimal | HeadroomIssue:
    for source, reserve in zip(intervals, reserve_plan.trajectory, strict=True):
        if _utc(source.interval.start) <= _utc(instant) <= _utc(source.interval.end):
            duration = _hours(source.interval.start, source.interval.end)
            fraction = _hours(source.interval.start, instant) / duration
            left = _decimal(reserve.start_energy_kwh, "reserve start")
            right = _decimal(reserve.end_energy_kwh, "reserve end")
            return left + (right - left) * fraction
    return HeadroomIssue("RESERVE_ALIGNMENT", "reserve trajectory does not cover simulation boundary")


def _source_at(intervals: Sequence[ReserveInputInterval], instant: datetime) -> ReserveInputInterval | None:
    for item in intervals:
        if _utc(item.interval.start) <= _utc(instant) < _utc(item.interval.end):
            return item
    return intervals[-1] if intervals and _utc(instant) == _utc(intervals[-1].interval.end) else None


def _simulate_continuous(
    *, intervals: Sequence[ReserveInputInterval], reserve_plan: ReservePlanResult,
    start_energy: Decimal, action_start: datetime, action_end: datetime,
    maximum_charge: Decimal, maximum_discharge: Decimal, charge_eff: Decimal,
    discharge_eff: Decimal, capacity: Decimal, minimum: Decimal, target: Decimal,
) -> _Simulation:
    """Simulate one continuous slot, carrying discretionary depletion forward."""
    if _utc(action_end) <= _utc(action_start):
        return _Simulation(False, _ZERO, None, (), HeadroomIssue("SCHEDULE_INVALID", "empty action interval"))
    points = {_utc(intervals[0].interval.start), _utc(intervals[-1].interval.end), _utc(action_start), _utc(action_end)}
    for item in intervals:
        points.update((_utc(item.interval.start), _utc(item.interval.end)))
    points = sorted(point for point in points if _utc(intervals[0].interval.start) <= point <= _utc(intervals[-1].interval.end))
    energy, withdrawal, stop = start_energy, _ZERO, None
    ledger: list[tuple[datetime, datetime, Decimal, Decimal, Decimal]] = []
    for left, right in zip(points, points[1:]):
        if right <= left:
            continue
        item = _source_at(intervals, left)
        if item is None:
            return _Simulation(False, withdrawal, stop, tuple(ledger), HeadroomIssue("FORECAST_COVERAGE", "simulation forecast has a gap"))
        duration = _hours(left, right)
        source_duration = _hours(item.interval.start, item.interval.end)
        load = _decimal(item.load_kwh, "load_kwh") * duration / source_duration
        solar = _decimal(item.solar_kwh, "solar_kwh") * duration / source_duration
        deficit = load - solar
        if deficit > _ZERO:
            grid = min(deficit, MAXIMUM_GRID_IMPORT_POWER_KW * duration)
            baseline_ac = deficit - grid
            if baseline_ac > maximum_discharge * duration:
                return _Simulation(False, withdrawal, stop, tuple(ledger), HeadroomIssue("DISCHARGE_POWER_EXCEEDED", "baseline household demand exceeds commissioned discharge output"))
            baseline_delta = -baseline_ac / discharge_eff
        else:
            baseline_ac = _ZERO
            baseline_delta = min(-deficit, maximum_charge * duration) * charge_eff
        reserve_left = _reserve_at(reserve_plan, intervals, left)
        reserve_right = _reserve_at(reserve_plan, intervals, right)
        if isinstance(reserve_left, HeadroomIssue) or isinstance(reserve_right, HeadroomIssue):
            return _Simulation(False, withdrawal, stop, tuple(ledger), reserve_left if isinstance(reserve_left, HeadroomIssue) else reserve_right)
        discretionary_ac = _ZERO
        active_left, active_right = max(left, _utc(action_start)), min(right, _utc(action_end))
        if active_left < active_right:
            active_hours = _hours(active_left, active_right)
            baseline_rate = baseline_ac / duration
            remaining_output = max(_ZERO, (maximum_discharge - baseline_rate) * active_hours)
            remaining_target = max(_ZERO, target - withdrawal)
            discretionary_stored = min(remaining_target, remaining_output / discharge_eff)
            discretionary_ac = discretionary_stored * discharge_eff
            if discretionary_stored > _ZERO and stop is None and withdrawal + discretionary_stored >= target:
                stop = active_left + _delta_hours(discretionary_ac / max(_ZERO, maximum_discharge - baseline_rate))
                stop = min(stop, active_right)
            withdrawal += discretionary_stored
        end_energy = energy + baseline_delta - discretionary_ac / discharge_eff
        if energy < reserve_left or energy < minimum or energy > capacity:
            return _Simulation(False, withdrawal, stop, tuple(ledger), HeadroomIssue("RESERVE_BREACH", "trajectory starts below reserve"))
        if end_energy < reserve_right or end_energy < minimum or end_energy > capacity:
            return _Simulation(False, withdrawal, stop, tuple(ledger), HeadroomIssue("RESERVE_BREACH", "trajectory ends below reserve"))
        ledger.append((left, right, energy, end_energy, discretionary_ac))
        energy = end_energy
    return _Simulation(True, withdrawal, stop, tuple(ledger))


def _plan_pre_discharge_headroom(
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
    forecast_observation: ForecastObservation | None = None,
    inverter_timezone: tzinfo | None = None,
    energy_authority: EnergyObservationAuthority | None = None,
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
        if not isinstance(inverter_timezone, tzinfo):
            raise ValueError("inverter_timezone must be explicit")
        local_start = window_start.astimezone(inverter_timezone)
        local_end = window_end.astimezone(inverter_timezone)
        if _utc(window_end) - _utc(window_start) >= _DAY:
            raise ValueError("schedule interval must be shorter than 24 hours")
        wall_issue = _wall_safe(local_start, local_end, inverter_timezone)
        if wall_issue:
            return _issue(PreDischargePlanningStatus.INVALID, wall_issue.code, wall_issue.detail)
    except (TypeError, ValueError) as exc:
        return _issue(PreDischargePlanningStatus.INVALID, "INPUT_INVALID", str(exc))

    energy_value = _validate_energy(energy, now)
    if isinstance(energy_value, HeadroomIssue):
        status = PreDischargePlanningStatus.UNAVAILABLE if energy_value.code in {"ENERGY_STALE", "ENERGY_IN_FUTURE"} else PreDischargePlanningStatus.INVALID
        return _issue(status, energy_value.code, energy_value.detail)
    observed_kwh, fresh_until = energy_value
    energy_authority_issue = _validate_energy_authority(energy, energy_authority)
    if energy_authority_issue:
        status = PreDischargePlanningStatus.UNAVAILABLE if energy_authority_issue.code.endswith("UNAVAILABLE") else PreDischargePlanningStatus.INVALID
        return _issue(status, energy_authority_issue.code, energy_authority_issue.detail)
    forecast_fresh_until, forecast_issue = _validate_forecast_observation(forecast_observation, now=now)
    if forecast_issue:
        status = PreDischargePlanningStatus.UNAVAILABLE if forecast_issue.code.endswith(("STALE", "UNAVAILABLE")) else PreDischargePlanningStatus.INVALID
        return _issue(status, forecast_issue.code, forecast_issue.detail)
    fresh_until = min(fresh_until, forecast_fresh_until)
    if _utc(energy.observed_at) > _utc(window_start):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, "ENERGY_AFTER_WINDOW_START", "energy observation is after the window start")
    try:
        horizon_end = forecast_intervals[-1].interval.end
    except (IndexError, AttributeError, TypeError):
        return _issue(PreDischargePlanningStatus.INVALID, "FORECAST_INVALID", "forecast horizon is empty or malformed")
    forecast_issue = _validate_intervals(forecast_intervals, energy.observed_at, horizon_end)
    if forecast_issue:
        return _issue(PreDischargePlanningStatus.INVALID, forecast_issue.code, forecast_issue.detail)
    if _utc(horizon_end) < _utc(window_end):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, "FORECAST_COVERAGE", "forecast does not cover the complete refill window")
    # Reserve trajectories may include the refill window.  For this strategy,
    # only boundaries through the window start are needed for the backward
    # overlay, but all supplied material inputs remain in the fingerprint.
    # The forecast may also contain refill intervals after the window start.
    # Clip it to the pre-window horizon; the supplied trajectory is not an
    # authority, so it is re-proved through T0013 below.
    pre_window_intervals = _slice_forecast(forecast_intervals, energy.observed_at, window_start)
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
    import_fresh_until, import_fresh_issue = _source_until(import_source, now, OCTOPUS_RATE_SOURCE_MAX_AGE, "import")
    if import_fresh_issue:
        status = PreDischargePlanningStatus.UNAVAILABLE if import_fresh_issue.code.endswith(("UNAVAILABLE", "STALE")) else PreDischargePlanningStatus.INVALID
        return _issue(status, import_fresh_issue.code, import_fresh_issue.detail)
    fresh_until = min(fresh_until, import_fresh_until)
    if dispatch_source is not None:
        try:
            dispatch_retrieved = _aware(dispatch_source.retrieved_at, "dispatch retrieved_at")
            if dispatch_retrieved > now + MAXIMUM_SOURCE_FUTURE_SKEW:
                return _issue(PreDischargePlanningStatus.INVALID, "DISPATCH_INVALID", "dispatch source observation is in the future")
            dispatch_fresh_until = dispatch_retrieved + OCTOPUS_DISPATCH_SOURCE_MAX_AGE
            if now > dispatch_fresh_until:
                return _issue(PreDischargePlanningStatus.UNAVAILABLE, "DISPATCH_STALE", "dispatch source observation is stale")
            fresh_until = min(fresh_until, dispatch_fresh_until)
        except (AttributeError, TypeError, ValueError) as exc:
            return _issue(PreDischargePlanningStatus.INVALID, "DISPATCH_INVALID", str(exc))

    # Re-run T0013 over the complete supplied horizon, including refill and
    # later demand.  A pre-window-only proof can be unsafe when later demand
    # raises the reverse reserve floor.
    if type(reserve_plan) is not ReservePlanResult:
        return _issue(PreDischargePlanningStatus.INVALID, "RESERVE_INVALID", "reserve plan has an unexpected concrete type")
    proved_reserve = plan_commissioned_reserve(
        intervals=forecast_intervals,
        trusted_import=trusted_import,
        import_source=import_source,
        dispatch_source=dispatch_source,
        start=forecast_intervals[0].interval.start,
        end=forecast_intervals[-1].interval.end,
        now=now,
        capacity_kwh=capacity,
        minimum_energy_kwh=minimum,
        reserve_margin_kwh=margin,
        charge_efficiency=charge_eff,
        discharge_efficiency=discharge_eff,
        commissioned=commissioned,
        authority=authority,
    )
    if proved_reserve.status is not ReservePlanningStatus.COMPLETE:
        status = PreDischargePlanningStatus.INFEASIBLE if proved_reserve.status is ReservePlanningStatus.INFEASIBLE else (
            PreDischargePlanningStatus.UNAVAILABLE if proved_reserve.status is ReservePlanningStatus.UNAVAILABLE else PreDischargePlanningStatus.INVALID
        )
        detail = "; ".join(issue.detail for issue in proved_reserve.issues) or "T0013 reserve proof is not complete"
        return _issue(status, "RESERVE_" + proved_reserve.status.value, detail)
    if len(reserve_plan.trajectory) != len(proved_reserve.trajectory) or any(
        left != right for left, right in zip(reserve_plan.trajectory, proved_reserve.trajectory, strict=True)
    ):
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, "RESERVE_FINGERPRINT_MISMATCH", "supplied reserve trajectory does not equal the complete recomputed result")
    reserve_plan = proved_reserve

    # The observation may pre-date ``now``.  Simulate that historical slice
    # only to establish today's starting energy; no proposed intent may start
    # in the past.
    if _utc(now) >= _utc(window_start):
        return _issue(PreDischargePlanningStatus.INFEASIBLE, "NO_ACTION_TIME", "window has already started")
    observed_to_now = _slice_forecast(pre_window_intervals, energy.observed_at, now)
    action_intervals = _slice_forecast(pre_window_intervals, now, window_start)
    past_reserves = _reserve_for_intervals(reserve_plan, forecast_intervals, observed_to_now)
    action_reserves = _reserve_for_intervals(reserve_plan, forecast_intervals, action_intervals)
    if isinstance(past_reserves, HeadroomIssue) or isinstance(action_reserves, HeadroomIssue):
        problem = past_reserves if isinstance(past_reserves, HeadroomIssue) else action_reserves
        return _issue(PreDischargePlanningStatus.UNAVAILABLE, problem.code, problem.detail)
    past_result = _forward_baseline(
        energy=observed_kwh,
        intervals=observed_to_now,
        reserve_starts=past_reserves[0],
        reserve_ends=past_reserves[1],
        capacity=capacity,
        minimum=minimum,
        charge_eff=charge_eff,
        discharge_eff=discharge_eff,
        maximum_charge=maximum_charge,
        maximum_discharge=maximum_discharge,
    )
    if isinstance(past_result, HeadroomIssue):
        return _issue(PreDischargePlanningStatus.INFEASIBLE, past_result.code, past_result.detail)
    energy_at_now = past_result[1][-1] if past_result[1] else observed_kwh
    baseline_result = _forward_baseline(
        energy=energy_at_now,
        intervals=action_intervals,
        reserve_starts=action_reserves[0],
        reserve_ends=action_reserves[1],
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
    reserve_starts, reserve_ends = action_reserves
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
        forecast_observation=forecast_observation,
        energy_authority=energy_authority,
        inverter_timezone=inverter_timezone,
        commissioned=commissioned,
        authority=authority,
        now=now,
        capacity_kwh=capacity,
        minimum_energy_kwh=minimum,
        reserve_margin_kwh=margin,
        charge_efficiency=charge_eff,
        discharge_efficiency=discharge_eff,
        cycle_cost_per_kwh=cycle_cost,
        policy_constants={
            "maximum_grid_import_power_kw": MAXIMUM_GRID_IMPORT_POWER_KW,
            "maximum_telemetry_age": MAXIMUM_TELEMETRY_AGE,
            "maximum_future_clock_skew": MAXIMUM_FUTURE_CLOCK_SKEW,
            "maximum_source_future_skew": MAXIMUM_SOURCE_FUTURE_SKEW,
            "octopus_rate_source_max_age": OCTOPUS_RATE_SOURCE_MAX_AGE,
            "octopus_export_source_max_age": OCTOPUS_EXPORT_SOURCE_MAX_AGE,
            "octopus_dispatch_source_max_age": OCTOPUS_DISPATCH_SOURCE_MAX_AGE,
            "forecast_source_max_age": FORECAST_SOURCE_MAX_AGE,
        },
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

    # Encode both boundaries in the inverter timezone.  Search minute starts
    # backwards using one continuous forward simulator; disjoint forecast
    # pieces are never merged into a Solis schedule.
    local_end = _minute_floor(window_start.astimezone(inverter_timezone))
    quantized_end = local_end
    if _utc(quantized_end) <= _utc(now):
        return _issue(PreDischargePlanningStatus.INFEASIBLE, "NO_PREWINDOW_TIME", "no minute-representable pre-window time remains")
    best: tuple[datetime, _Simulation] | None = None
    latest_safe: tuple[datetime, _Simulation] | None = None
    horizon_minutes = int((_utc(quantized_end) - _utc(now)).total_seconds() // 60)
    for offset in range(horizon_minutes + 1):
        candidate = local_end - offset * _MINUTE
        if _utc(candidate) < _utc(now):
            break
        if _utc(candidate) == _utc(quantized_end):
            continue
        wall_issue = _wall_safe(candidate, quantized_end, inverter_timezone)
        if wall_issue:
            continue
        simulation = _simulate_continuous(
            intervals=forecast_intervals, reserve_plan=reserve_plan,
            start_energy=observed_kwh, action_start=candidate, action_end=quantized_end,
            maximum_charge=maximum_charge, maximum_discharge=maximum_discharge,
            charge_eff=charge_eff, discharge_eff=discharge_eff, capacity=capacity,
            minimum=minimum, target=desired_withdrawal,
        )
        if not simulation.safe:
            continue
        if latest_safe is None or simulation.withdrawal_kwh > latest_safe[1].withdrawal_kwh:
            latest_safe = (candidate, simulation)
        if simulation.withdrawal_kwh >= desired_withdrawal:
            best = (candidate, simulation)
            break
    if best is None:
        best = latest_safe
    if best is None or best[1].withdrawal_kwh <= _ZERO:
        return _issue(PreDischargePlanningStatus.INFEASIBLE, "NO_REACHABLE_HEADROOM", "reserve or commissioned output leaves no safe continuous export interval")
    quantized_start, simulation = best
    proposed_start, proposed_end = quantized_start, quantized_end
    planned = min(simulation.withdrawal_kwh, desired_withdrawal)
    if _utc(proposed_end) - _utc(proposed_start) >= _DAY:
        return _issue(PreDischargePlanningStatus.INVALID, "SCHEDULE_NOT_REPRESENTABLE", "quantized interval is at least 24 hours")
    # Economics covers every encoded discharge instant, even when target-stop
    # would end discretionary withdrawal before the slot boundary.
    exports, issue = _export_coverage(export_rates, proposed_start, proposed_end, export_source, now)
    if issue:
        status = PreDischargePlanningStatus.UNAVAILABLE if issue.code.endswith("UNAVAILABLE") or issue.code in {"EXPORT_COVERAGE", "EXPORT_PROVENANCE"} else PreDischargePlanningStatus.INVALID
        return _issue(status, issue.code, issue.detail)
    export_fresh_until, export_fresh_issue = _source_until(export_source, now, OCTOPUS_EXPORT_SOURCE_MAX_AGE, "export")
    if export_fresh_issue:
        status = PreDischargePlanningStatus.UNAVAILABLE if export_fresh_issue.code.endswith(("UNAVAILABLE", "STALE")) else PreDischargePlanningStatus.INVALID
        return _issue(status, export_fresh_issue.code, export_fresh_issue.detail)
    fresh_until = min(fresh_until, export_fresh_until, proposed_end)
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
        schedule_ledger=tuple(
            ScheduleLedgerEntry(left, right, start_energy, end_energy, discretionary_ac)
            for left, right, start_energy, end_energy, discretionary_ac in simulation.ledger
        ),
        input_fingerprint=fingerprint,
        fresh_until=fresh_until,
    )


def plan_pre_discharge_headroom(**kwargs: Any) -> PreDischargePlanResult:
    """Typed fail-closed boundary for malformed external sequences."""
    try:
        return _plan_pre_discharge_headroom(**kwargs)
    except Exception as exc:  # malformed third-party records are data, not crashes
        return _issue(PreDischargePlanningStatus.INVALID, "INPUT_INVALID", str(exc))


commissioned_pre_discharge_headroom = plan_pre_discharge_headroom

__all__ = [
    "BatteryEnergyObservation",
    "EnergyObservationAuthority",
    "ForecastObservation",
    "EnergyObservation",
    "HeadroomIssue",
    "ScheduleLedgerEntry",
    "PreDischargePlanResult",
    "PreDischargePlanningStatus",
    "PreDischargeResult",
    "PreDischargeStatus",
    "commissioned_pre_discharge_headroom",
    "plan_pre_discharge_headroom",
]
