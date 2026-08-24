"""Small shared value model for the house-battery controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

FULL_SOC_PERCENT = 100
MINIMUM_SOC_PERCENT = 10


class _TextEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ControllerHealth(_TextEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAIL_SAFE = "fail_safe"


class StrategyAction(_TextEnum):
    IDLE = "IDLE"
    CHEAP_CHARGE = "CHEAP_CHARGE"
    RESERVE_DISCHARGE = "RESERVE_DISCHARGE"
    CYCLE_DISCHARGE = "CYCLE_DISCHARGE"


class CycleState(_TextEnum):
    IDLE = "IDLE"
    RESERVE_DISCHARGING = "RESERVE_DISCHARGING"
    CYCLE_DISCHARGING = "CYCLE_DISCHARGING"
    CHARGING = "CHARGING"
    STOPPING = "STOPPING"


class SlotDirection(_TextEnum):
    CHARGE = "charge"
    DISCHARGE = "discharge"


class SlotOwner(_TextEnum):
    CHEAP_CHARGING = "cheap_charging"
    FULL_SOC_CYCLING = "full_soc_cycling"
    RESERVE_EXPORT = "reserve_export"


class StorageMode(_TextEnum):
    SELF_USE = "Self-Use"
    FEED_IN_PRIORITY = "Feed-In Priority"


@dataclass(frozen=True, slots=True)
class ObservedCapability:
    current_value: Decimal
    minimum: Decimal
    maximum: Decimal
    step: Decimal
    unit: str

    def __post_init__(self) -> None:
        values = (self.current_value, self.minimum, self.maximum, self.step)
        if any(not isinstance(value, Decimal) for value in values):
            raise TypeError("capability values must be Decimal instances")
        if any(not value.is_finite() for value in values):
            raise ValueError("capability values must be finite")
        if self.minimum > self.maximum:
            raise ValueError("capability minimum cannot exceed maximum")
        if not self.minimum <= self.current_value <= self.maximum:
            raise ValueError("capability current value must be within its bounds")
        if self.step <= 0:
            raise ValueError("capability step must be positive")
        if not self.unit:
            raise ValueError("capability unit must not be empty")


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Live inverter current limits used by planning and native slots."""
    maximum_charge_current: ObservedCapability
    maximum_discharge_current: ObservedCapability


def _validate_percent(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal(0) <= value <= Decimal(100):
        raise ValueError(f"{name} must be a percentage from 0 to 100")


@dataclass(frozen=True, slots=True)
class SlotIntent:
    """One bounded logical segment; physical allocation belongs to Solis."""

    owner: SlotOwner
    direction: SlotDirection
    start: datetime
    end: datetime
    current: Decimal
    target_soc: Decimal
    expiry: datetime

    def __post_init__(self) -> None:
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.start, self.end, self.expiry)):
            raise ValueError("slot datetimes must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("slot interval must be ordered")
        if self.expiry <= self.start:
            raise ValueError("slot expiry must be later than slot start")
        if self.end > self.expiry:
            raise ValueError("slot interval must not exceed expiry")
        if not isinstance(self.current, Decimal) or not self.current.is_finite() or self.current < 0:
            raise ValueError("slot current must not be negative")
        _validate_percent(self.target_soc, "target_soc")


@dataclass(frozen=True, slots=True)
class LogicalIntent:
    """One or two adjacent, same-direction segments of one logical action."""

    segments: tuple[SlotIntent, ...]

    def __post_init__(self) -> None:
        if len(self.segments) not in (1, 2):
            raise ValueError("logical intent must contain one or two segments")
        first = self.segments[0]
        for segment in self.segments:
            if (segment.owner, segment.direction, segment.current, segment.target_soc) != (
                first.owner, first.direction, first.current, first.target_soc
            ):
                raise ValueError("logical intent segments must share owner, direction and values")
        for previous, current in zip(self.segments, self.segments[1:]):
            if previous.end != current.start or previous.end > current.start:
                raise ValueError("logical intent segments must be adjacent and ordered")

    @property
    def start(self) -> datetime:
        return self.segments[0].start

    @property
    def end(self) -> datetime:
        return self.segments[-1].end


__all__ = [
    "ControllerHealth", "CycleState", "FULL_SOC_PERCENT", "LogicalIntent",
    "MINIMUM_SOC_PERCENT", "ObservedCapability", "RuntimeCapabilities",
    "SlotDirection", "SlotIntent", "SlotOwner", "StorageMode", "StrategyAction",
]
