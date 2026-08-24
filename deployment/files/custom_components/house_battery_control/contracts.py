"""Compatibility boundary for the pre-T0029 runtime.

The controller and Solis actuator still require a physical slot on their old
intent type.  Keep that transitional shape here only; the new shared model in
``model.py`` deliberately has no physical-slot ownership.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .model import (
    ControllerHealth,
    CycleState,
    LogicalIntent,
    ObservedCapability,
    RuntimeCapabilities,
    SlotDirection,
    SlotOwner,
    StorageMode,
    StrategyAction,
)


def _validate_percent(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal(0) <= value <= Decimal(100):
        raise ValueError(f"{name} must be a percentage from 0 to 100")


@dataclass(frozen=True, slots=True)
class SlotIntent:
    """Legacy physical-slot intent, deleted with the T0029 actuator."""

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
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.start, self.end, self.expiry)):
            raise ValueError("slot datetimes must be timezone-aware")
        if self.start >= self.end or self.end > self.expiry or self.expiry <= self.start:
            raise ValueError("slot interval must be ordered and bounded by expiry")
        if not isinstance(self.current, Decimal) or not self.current.is_finite() or self.current < 0:
            raise ValueError("slot current must not be negative")
        _validate_percent(self.target_soc, "target_soc")

__all__ = [
    "ControllerHealth", "CycleState", "LogicalIntent", "ObservedCapability",
    "RuntimeCapabilities", "SlotDirection", "SlotIntent", "SlotOwner",
    "StorageMode", "StrategyAction",
]
