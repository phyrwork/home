"""Pure reverse planner for the dynamic household reserve."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Sequence

from .domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from .interval import TimeInterval
from .octopus_windows import CheapClassification


class ReservePlanningStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INFEASIBLE = "INFEASIBLE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ReserveIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReserveInputInterval:
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

    @property
    def reserve_energy_kwh(self) -> Decimal | None:
        return self.trajectory[0].start_energy_kwh if self.trajectory else None


def plan_reserve(
    *,
    intervals: Sequence[ReserveInputInterval],
    capacity_kwh: Decimal,
    minimum_energy_kwh: Decimal,
    reserve_margin_kwh: Decimal,
    charge_efficiency: Decimal,
    discharge_efficiency: Decimal,
    maximum_charge_power_kw: Decimal,
    maximum_discharge_power_kw: Decimal,
) -> ReservePlanResult:
    """Reverse-plan energy needed until the next trusted charging opportunity."""

    try:
        values = (
            capacity_kwh,
            minimum_energy_kwh,
            reserve_margin_kwh,
            charge_efficiency,
            discharge_efficiency,
            maximum_charge_power_kw,
            maximum_discharge_power_kw,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise ValueError("planner values must be finite Decimals")
        if capacity_kwh <= 0 or not Decimal(0) <= minimum_energy_kwh <= capacity_kwh:
            raise ValueError("battery energy bounds are invalid")
        if reserve_margin_kwh < 0:
            raise ValueError("reserve margin must not be negative")
        if not Decimal(0) < charge_efficiency <= 1 or not Decimal(0) < discharge_efficiency <= 1:
            raise ValueError("efficiencies must be in (0, 1]")
        if maximum_charge_power_kw <= 0 or maximum_discharge_power_kw <= 0:
            raise ValueError("runtime power limits must be positive")
        if not intervals:
            raise ValueError("at least one forecast interval is required")
        previous_end: datetime | None = None
        for item in intervals:
            if type(item) is not ReserveInputInterval:
                raise ValueError("forecast interval has an unexpected type")
            if item.interval.start.tzinfo is None or item.interval.end.tzinfo is None:
                raise ValueError("forecast timestamps must be timezone-aware")
            if _utc(item.interval.end) <= _utc(item.interval.start):
                raise ValueError("forecast interval must be ordered")
            if previous_end is not None and _utc(item.interval.start) != _utc(previous_end):
                raise ValueError("forecast intervals must be contiguous")
            if any(not value.is_finite() for value in (item.load_kwh, item.solar_kwh)):
                raise ValueError("forecast energy must be finite")
            if item.solar_kwh < 0:
                raise ValueError("solar forecast must not be negative")
            previous_end = item.interval.end
    except (AttributeError, TypeError, ValueError) as exc:
        return ReservePlanResult(ReservePlanningStatus.INVALID, issues=(ReserveIssue("INVALID_INPUT", str(exc)),))

    floor = minimum_energy_kwh + reserve_margin_kwh
    if floor > capacity_kwh:
        return ReservePlanResult(
            ReservePlanningStatus.INFEASIBLE,
            issues=(ReserveIssue("RESERVE_EXCEEDS_CAPACITY", "minimum energy plus reserve margin exceeds capacity"),),
        )

    required = floor
    reverse: list[ReserveTrajectoryInterval] = []
    for item in reversed(intervals):
        end_required = required
        hours = _hours(item.interval.start, item.interval.end)
        deficit = item.load_kwh - item.solar_kwh
        if item.classification is not CheapClassification.NOT_CHEAP:
            required = max(floor, required - maximum_charge_power_kw * hours * charge_efficiency)
        elif deficit > 0:
            battery_output = max(Decimal(0), deficit - MAXIMUM_GRID_IMPORT_POWER_KW * hours)
            if battery_output > maximum_discharge_power_kw * hours:
                return ReservePlanResult(
                    ReservePlanningStatus.INFEASIBLE,
                    issues=(ReserveIssue("DISCHARGE_POWER_EXCEEDED", "forecast demand exceeds battery and grid power"),),
                )
            required += battery_output / discharge_efficiency
        else:
            # External-PV surplus is visible as negative load and can charge
            # the battery in Feed-In Priority.
            stored = min(-deficit, maximum_charge_power_kw * hours) * charge_efficiency
            required = max(floor, required - stored)
        if required > capacity_kwh:
            return ReservePlanResult(
                ReservePlanningStatus.INFEASIBLE,
                issues=(ReserveIssue("CAPACITY_EXCEEDED", "required household reserve exceeds battery capacity"),),
            )
        reverse.append(ReserveTrajectoryInterval(item.interval, required, end_required))
    return ReservePlanResult(ReservePlanningStatus.COMPLETE, trajectory=tuple(reversed(reverse)))


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _hours(start: datetime, end: datetime) -> Decimal:
    delta = _utc(end) - _utc(start)
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(micros) / Decimal(3_600_000_000)


__all__ = [
    "ReserveInputInterval",
    "ReserveIssue",
    "ReservePlanResult",
    "ReservePlanningStatus",
    "ReserveTrajectoryInterval",
    "plan_reserve",
]
