"""Pure reserve planning against commissioned AC power evidence.

The legacy :mod:`planner` module remains the compatibility model.  This
module is deliberately separate: it cannot use generic Home Assistant entity
limits, and it does not produce SOC values or perform writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Sequence

from .domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from .interval import TimeInterval
from .octopus_windows import (
    CheapClassification,
    CoverageStatus,
    DispatchSourceObservation,
    RateSourceObservation,
    TrustedImportResult,
    evaluate_trusted_import_rates,
)


class ReservePlanningStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INFEASIBLE = "INFEASIBLE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ReserveIssue:
    """A stable, machine-readable reason for a non-complete result."""

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class CommissionedPowerEnvelope:
    """Device-verified AC boundary and authority evidence."""

    maximum_charge_power_kw: Decimal
    maximum_discharge_power_kw: Decimal
    maximum_grid_import_power_kw: Decimal
    schema_version: str
    inverter_identity: str
    mapping_fingerprint: str
    candidate_policy_fingerprint: str
    manual_grid_fingerprint: str
    capability_fingerprint: str
    evidence_source: str
    validated_at: datetime


@dataclass(frozen=True, slots=True)
class ReserveAuthority:
    """Current authority values which must match commissioning evidence."""

    schema_version: str
    inverter_identity: str
    mapping_fingerprint: str
    candidate_policy_fingerprint: str
    manual_grid_fingerprint: str
    capability_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReserveInputInterval:
    """Forecast energy and trusted import class for one exact interval."""

    interval: TimeInterval
    load_kwh: Decimal
    solar_kwh: Decimal
    classification: CheapClassification


@dataclass(frozen=True, slots=True)
class ReserveTrajectoryInterval:
    interval: TimeInterval
    start_energy_kwh: Decimal
    end_energy_kwh: Decimal


@dataclass(frozen=True, slots=True)
class ReservePlanResult:
    status: ReservePlanningStatus
    trajectory: tuple[ReserveTrajectoryInterval, ...] = ()
    issues: tuple[ReserveIssue, ...] = ()


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a Decimal-compatible value") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _aware(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _duration_hours(start: datetime, end: datetime) -> Decimal:
    """Return exact elapsed hours without a float ``total_seconds`` step."""

    delta = _instant(end) - _instant(start)
    microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return Decimal(microseconds) / Decimal(3_600_000_000)


def _issue(code: str, detail: str) -> ReservePlanResult:
    return ReservePlanResult(ReservePlanningStatus.INVALID, issues=(ReserveIssue(code, detail),))


def _unavailable(code: str, detail: str) -> ReservePlanResult:
    return ReservePlanResult(ReservePlanningStatus.UNAVAILABLE, issues=(ReserveIssue(code, detail),))


def _infeasible(code: str, detail: str) -> ReservePlanResult:
    return ReservePlanResult(ReservePlanningStatus.INFEASIBLE, issues=(ReserveIssue(code, detail),))


def _validate_authority(
    envelope: CommissionedPowerEnvelope | None,
    authority: ReserveAuthority | None,
    now: datetime,
) -> tuple[Decimal, Decimal] | ReservePlanResult:
    if envelope is None:
        return _unavailable("COMMISSIONING_MISSING", "commissioned power evidence is absent")
    if authority is None:
        return _unavailable("AUTHORITY_MISSING", "current authority evidence is absent")
    if type(envelope) is not CommissionedPowerEnvelope or type(authority) is not ReserveAuthority:
        return _issue("COMMISSIONING_INVALID", "commissioning authority has an unexpected concrete type")
    if (
        envelope.maximum_charge_power_kw is None
        or envelope.maximum_discharge_power_kw is None
        or envelope.maximum_grid_import_power_kw is None
    ):
        return _unavailable(
            "POWER_EVIDENCE_MISSING",
            "separate verified AC charge and discharge power evidence is required",
        )
    try:
        charge = _decimal(envelope.maximum_charge_power_kw, "maximum_charge_power_kw")
        discharge = _decimal(envelope.maximum_discharge_power_kw, "maximum_discharge_power_kw")
        grid = _decimal(envelope.maximum_grid_import_power_kw, "maximum_grid_import_power_kw")
        if charge < 0 or discharge < 0 or grid < 0:
            raise ValueError("commissioned AC powers must not be negative")
        if grid != MAXIMUM_GRID_IMPORT_POWER_KW:
            return _unavailable(
                "GRID_IMPORT_MISMATCH",
                "commissioned grid contribution does not equal MAXIMUM_GRID_IMPORT_POWER_KW",
            )
        validated_at = _aware(envelope.validated_at, "validated_at")
        if _instant(validated_at) > _instant(now):
            raise ValueError("validated_at must not be in the future")
        fields = (
            ("schema_version", envelope.schema_version, authority.schema_version),
            ("inverter_identity", envelope.inverter_identity, authority.inverter_identity),
            ("mapping_fingerprint", envelope.mapping_fingerprint, authority.mapping_fingerprint),
            (
                "candidate_policy_fingerprint",
                envelope.candidate_policy_fingerprint,
                authority.candidate_policy_fingerprint,
            ),
            ("manual_grid_fingerprint", envelope.manual_grid_fingerprint, authority.manual_grid_fingerprint),
            ("capability_fingerprint", envelope.capability_fingerprint, authority.capability_fingerprint),
        )
        for label, commissioned, current in fields:
            _text(commissioned, f"commissioned {label}")
            _text(current, f"current {label}")
            if commissioned != current:
                return _unavailable("AUTHORITY_MISMATCH", f"{label} does not match commissioning evidence")
        _text(envelope.evidence_source, "evidence_source")
    except (AttributeError, TypeError, ValueError) as exc:
        return _issue("COMMISSIONING_INVALID", str(exc))
    return charge, discharge


def _validate_inputs(
    *,
    intervals: Sequence[ReserveInputInterval],
    trusted_import: TrustedImportResult | None,
    import_source: RateSourceObservation | None,
    dispatch_source: DispatchSourceObservation | None,
    start: datetime,
    end: datetime,
    now: datetime,
    capacity_kwh: Any,
    minimum_energy_kwh: Any,
    reserve_margin_kwh: Any,
    charge_efficiency: Any,
    discharge_efficiency: Any,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | ReservePlanResult:
    try:
        start = _aware(start, "requested start")
        end = _aware(end, "requested end")
        if _instant(end) <= _instant(start):
            raise ValueError("requested horizon must be non-empty and ordered")
        capacity = _decimal(capacity_kwh, "capacity_kwh")
        minimum = _decimal(minimum_energy_kwh, "minimum_energy_kwh")
        margin = _decimal(reserve_margin_kwh, "reserve_margin_kwh")
        charge_eff = _decimal(charge_efficiency, "charge_efficiency")
        discharge_eff = _decimal(discharge_efficiency, "discharge_efficiency")
        if capacity < 0 or minimum < 0 or minimum > capacity:
            raise ValueError("capacity and minimum energy must be non-negative and ordered")
        if margin < 0:
            raise ValueError("reserve margin must not be negative")
        if not (Decimal(0) < charge_eff <= Decimal(1)):
            raise ValueError("charge efficiency must be greater than zero and at most one")
        if not (Decimal(0) < discharge_eff <= Decimal(1)):
            raise ValueError("discharge efficiency must be greater than zero and at most one")
        if not isinstance(intervals, Sequence) or isinstance(intervals, (str, bytes)) or not intervals:
            raise ValueError("reserve intervals must be a non-empty sequence")
        previous: ReserveInputInterval | None = None
        for source in intervals:
            if type(source) is not ReserveInputInterval:
                raise ValueError("reserve interval has an unexpected concrete type")
            _aware(source.interval.start, "interval start")
            _aware(source.interval.end, "interval end")
            if _instant(source.interval.end) <= _instant(source.interval.start):
                raise ValueError("interval end must be after start")
            if previous is not None and _instant(source.interval.start) != _instant(previous.interval.end):
                raise ValueError("reserve intervals must be contiguous and ordered")
            load = _decimal(source.load_kwh, "load_kwh")
            solar = _decimal(source.solar_kwh, "solar_kwh")
            # A negative load is the explicit representation used by the
            # external-PV integration for unmonitored surplus.  Solar is
            # non-negative.  The validated net deficit below preserves the
            # load sign exactly.
            if solar < 0:
                raise ValueError("solar energy must not be negative")
            if type(source.classification) is not CheapClassification:
                raise ValueError("reserve interval classification is not allowlisted")
            previous = source
        if _instant(intervals[0].interval.start) != _instant(start) or _instant(intervals[-1].interval.end) != _instant(end):
            raise ValueError("reserve intervals must cover the exact requested horizon")
        if trusted_import is None:
            raise LookupError("trusted import classification is absent")
        if trusted_import.coverage_status is CoverageStatus.INVALID:
            return _issue("IMPORT_INVALID", "trusted import classification is invalid")
        if trusted_import.coverage_status is not CoverageStatus.COMPLETE:
            return _unavailable("IMPORT_UNAVAILABLE", "trusted import classification is not complete")
        if not trusted_import.intervals:
            return _unavailable("IMPORT_UNAVAILABLE", "trusted import classification is empty")
        # Do not trust the status object as a capability.  Bind the exact rate
        # objects to this call's horizon, clock and authoritative source
        # observations by running the T0012 evaluator again.  This proves
        # ordinary freshness and independently proves bonus freshness/source.
        evaluated = evaluate_trusted_import_rates(
            import_rates=trusted_import.intervals,
            start=start,
            end=end,
            now=now,
            import_source=import_source,
            dispatch_source=dispatch_source,
        )
        if evaluated.coverage_status is CoverageStatus.INVALID:
            return _issue("IMPORT_INVALID", "; ".join(evaluated.issues))
        if evaluated.coverage_status is not CoverageStatus.COMPLETE:
            return _unavailable("IMPORT_UNAVAILABLE", "; ".join(evaluated.issues))
        # T0012 retains complete source events, including records outside the
        # requested horizon.  Bind each planner interval to exactly one source
        # record with identical boundaries so classification cannot change
        # part-way through a forecast interval.
        for source in intervals:
            aligned = tuple(
                rate
                for rate in trusted_import.intervals
                if _instant(source.interval.start) == _instant(rate.start)
                and _instant(source.interval.end) == _instant(rate.end)
            )
            if len(aligned) != 1:
                return _issue("IMPORT_ALIGNMENT", "trusted import intervals do not align with forecast intervals")
            if source.classification is not aligned[0].classification:
                return _issue("IMPORT_CLASSIFICATION", "forecast classification contradicts trusted import data")
    except LookupError as exc:
        return _unavailable("IMPORT_UNAVAILABLE", str(exc))
    except (AttributeError, TypeError, ValueError) as exc:
        return _issue("INPUT_INVALID", str(exc))
    return capacity, minimum, margin, charge_eff, discharge_eff


def plan_commissioned_reserve(
    *,
    intervals: Sequence[ReserveInputInterval],
    trusted_import: TrustedImportResult | None,
    import_source: RateSourceObservation | None = None,
    dispatch_source: DispatchSourceObservation | None = None,
    start: datetime,
    end: datetime,
    now: datetime,
    capacity_kwh: Any,
    minimum_energy_kwh: Any,
    reserve_margin_kwh: Any,
    charge_efficiency: Any,
    discharge_efficiency: Any,
    commissioned: CommissionedPowerEnvelope | None,
    authority: ReserveAuthority | None,
) -> ReservePlanResult:
    """Calculate a complete exact reverse reserve trajectory.

    ``now`` is intentionally only used to validate its timezone here.  Source
    freshness is established by ``evaluate_trusted_import_rates`` and remains
    bound to that result; this function never substitutes stale direct data.
    """
    try:
        _aware(now, "now")
    except ValueError as exc:
        return _issue("INPUT_INVALID", str(exc))
    inputs = _validate_inputs(
        intervals=intervals,
        trusted_import=trusted_import,
        import_source=import_source,
        dispatch_source=dispatch_source,
        start=start,
        end=end,
        now=now,
        capacity_kwh=capacity_kwh,
        minimum_energy_kwh=minimum_energy_kwh,
        reserve_margin_kwh=reserve_margin_kwh,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
    )
    if isinstance(inputs, ReservePlanResult):
        return inputs
    capacity, minimum, margin, charge_eff, discharge_eff = inputs
    powers = _validate_authority(commissioned, authority, now)
    if isinstance(powers, ReservePlanResult):
        return powers
    maximum_charge, maximum_discharge = powers
    floor = minimum + margin
    if floor > capacity:
        return _infeasible("PROTECTED_FLOOR_EXCEEDS_CAPACITY", "minimum energy plus reserve margin exceeds capacity")
    required = floor
    starts: list[Decimal] = []
    ends: list[Decimal] = []
    for source in reversed(intervals):
        end_required = required
        duration = _duration_hours(source.interval.start, source.interval.end)
        deficit = _decimal(source.load_kwh, "load_kwh") - _decimal(source.solar_kwh, "solar_kwh")
        if source.classification in (CheapClassification.STANDARD_CHEAP, CheapClassification.BONUS_DISPATCH):
            stored = maximum_charge * duration * charge_eff
            required = max(floor, required - stored)
        elif deficit > 0:
            grid = min(deficit, MAXIMUM_GRID_IMPORT_POWER_KW * duration)
            remaining = deficit - grid
            if remaining > maximum_discharge * duration:
                return _infeasible("DISCHARGE_POWER_EXCEEDED", "forecast battery output exceeds commissioned discharge power")
            required = required + remaining / discharge_eff
        else:
            stored = min(-deficit, maximum_charge * duration) * charge_eff
            required = max(floor, required - stored)
        if required > capacity:
            return _infeasible("CAPACITY_EXCEEDED", "required reserve boundary exceeds commissioned battery capacity")
        starts.append(required)
        ends.append(end_required)
    starts.reverse()
    ends.reverse()
    trajectory = tuple(
        ReserveTrajectoryInterval(source.interval, starts[index], ends[index])
        for index, source in enumerate(intervals)
    )
    return ReservePlanResult(ReservePlanningStatus.COMPLETE, trajectory=trajectory)


# Short alias for callers which already use the noun from the legacy planner.
commissioned_reserve = plan_commissioned_reserve
