"""Calculate one useful pre-discharge before a cheap charging window."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from enum import Enum

from .octopus_windows import CheapWindow


class PreDischargePlanningStatus(str, Enum):
    PLANNED = "PLANNED"
    NO_HEADROOM_NEEDED = "NO_HEADROOM_NEEDED"
    INFEASIBLE = "INFEASIBLE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class PreDischargePlanResult:
    status: PreDischargePlanningStatus
    proposed_start: datetime | None = None
    proposed_end: datetime | None = None
    target_soc_percent: Decimal | None = None
    target_energy_kwh: Decimal | None = None
    headroom_kwh: Decimal = Decimal(0)
    reason: str = ""


def plan_pre_discharge(
    *,
    now: datetime,
    current_energy_kwh: Decimal,
    expected_window_start_energy_kwh: Decimal,
    reserve_energy_kwh: Decimal,
    capacity_kwh: Decimal,
    maximum_charge_power_kw: Decimal,
    maximum_discharge_power_kw: Decimal,
    charge_efficiency: Decimal,
    discharge_efficiency: Decimal,
    window: CheapWindow,
    minimum_target_soc: Decimal,
    target_soc_step: Decimal,
) -> PreDischargePlanResult:
    """Plan the latest continuous discharge that creates refill headroom."""

    try:
        if now.tzinfo is None or window.start.tzinfo is None or window.end.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        if _utc(window.start) <= _utc(now) or _utc(window.end) <= _utc(window.start):
            return PreDischargePlanResult(PreDischargePlanningStatus.INFEASIBLE, reason="cheap window has started or is invalid")
        values = (
            current_energy_kwh,
            expected_window_start_energy_kwh,
            reserve_energy_kwh,
            capacity_kwh,
            maximum_charge_power_kw,
            maximum_discharge_power_kw,
            charge_efficiency,
            discharge_efficiency,
            minimum_target_soc,
            target_soc_step,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise ValueError("planner values must be finite Decimals")
        if capacity_kwh <= 0 or maximum_charge_power_kw <= 0 or maximum_discharge_power_kw <= 0:
            raise ValueError("capacity and runtime powers must be positive")
        if target_soc_step <= 0 or not Decimal(0) < charge_efficiency <= 1 or not Decimal(0) < discharge_efficiency <= 1:
            raise ValueError("step or efficiency is invalid")
    except (AttributeError, TypeError, ValueError) as exc:
        return PreDischargePlanResult(PreDischargePlanningStatus.INVALID, reason=str(exc))

    window_hours = _hours(window.start, window.end)
    refill_kwh = maximum_charge_power_kw * window_hours * charge_efficiency
    target_energy = max(reserve_energy_kwh, capacity_kwh - refill_kwh)
    target_energy = min(capacity_kwh, max(Decimal(0), target_energy))
    headroom = max(Decimal(0), expected_window_start_energy_kwh - target_energy)
    if headroom == 0:
        return PreDischargePlanResult(
            PreDischargePlanningStatus.NO_HEADROOM_NEEDED,
            target_energy_kwh=target_energy,
            reason="the cheap window can already use all available capacity",
        )

    # AC discharge power removes stored energy at P / discharge_efficiency.
    duration_hours = headroom * discharge_efficiency / maximum_discharge_power_kw
    duration = timedelta(seconds=float(duration_hours * Decimal(3600)))
    end = window.start.replace(second=0, microsecond=0)
    raw_start = end - duration
    start = raw_start.replace(second=0, microsecond=0)
    if start > raw_start:
        start -= timedelta(minutes=1)
    if start < now:
        start = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    if start >= end:
        return PreDischargePlanResult(PreDischargePlanningStatus.INFEASIBLE, reason="not enough time remains to create useful headroom")

    raw_soc = target_energy * Decimal(100) / capacity_kwh
    stepped_soc = (raw_soc / target_soc_step).to_integral_value(rounding=ROUND_CEILING) * target_soc_step
    target_soc = max(minimum_target_soc, stepped_soc)
    if target_soc >= Decimal(100):
        return PreDischargePlanResult(PreDischargePlanningStatus.NO_HEADROOM_NEEDED, reason="rounded target SOC leaves no headroom")
    return PreDischargePlanResult(
        PreDischargePlanningStatus.PLANNED,
        proposed_start=start,
        proposed_end=end,
        target_soc_percent=target_soc,
        target_energy_kwh=target_energy,
        headroom_kwh=headroom,
        reason="create only the headroom that the next cheap window can refill",
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _hours(start: datetime, end: datetime) -> Decimal:
    delta = _utc(end) - _utc(start)
    return Decimal(str(delta.total_seconds())) / Decimal(3600)


__all__ = ["PreDischargePlanResult", "PreDischargePlanningStatus", "plan_pre_discharge"]
