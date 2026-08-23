"""Pure, immutable contracts for the refactored house-battery controller.

This module deliberately has no Home Assistant imports. It describes the
observations and intents exchanged by the live runtime adapters.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class _TextEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ControllerHealth(_TextEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAIL_SAFE = "fail_safe"


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
    OFF_GRID = "Off-Grid"


@dataclass(frozen=True, slots=True)
class ObservedCapability:
    """A numeric capability observed from an external entity."""

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
    maximum_charge_current: ObservedCapability
    maximum_discharge_current: ObservedCapability


def _validate_percent(value: Decimal, name: str) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal(0) <= value <= Decimal(100)
    ):
        raise ValueError(f"{name} must be a percentage from 0 to 100")


@dataclass(frozen=True, slots=True)
class SlotIntent:
    owner: SlotOwner
    physical_slot: int
    direction: SlotDirection
    start: datetime
    end: datetime
    current: Decimal
    target_soc: Decimal
    expiry: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.physical_slot, int) or isinstance(self.physical_slot, bool) or not 1 <= self.physical_slot <= 6:
            raise ValueError("physical_slot must be in the Solis range 1 through 6")
        if self.start.tzinfo is None or self.start.utcoffset() is None or self.end.tzinfo is None or self.end.utcoffset() is None or self.expiry.tzinfo is None or self.expiry.utcoffset() is None:
            raise ValueError("slot datetimes must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("slot interval must be ordered")
        if self.expiry <= self.start:
            raise ValueError("slot expiry must be later than slot start")
        if not isinstance(self.current, Decimal) or not self.current.is_finite() or self.current < 0:
            raise ValueError("slot current must not be negative")
        _validate_percent(self.target_soc, "target_soc")
