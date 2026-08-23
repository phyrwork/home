"""Pure, immutable contracts for the refactored house-battery controller.

This module deliberately has no Home Assistant imports.  It describes observed
capabilities and desired policy; adapters are responsible for reading or
writing Home Assistant entities.
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


class StrategyPhase(_TextEnum):
    OBSERVING = "observing"
    IDLE = "idle"
    PRE_DISCHARGE = "pre_discharge"
    OFF_PEAK_CHARGE = "off_peak_charge"
    OFF_PEAK_CYCLE_DISCHARGE = "off_peak_cycle_discharge"
    FINAL_CHARGE = "final_charge"
    FAIL_SAFE = "fail_safe"


class SlotDirection(_TextEnum):
    CHARGE = "charge"
    DISCHARGE = "discharge"


class SlotOwner(_TextEnum):
    CHEAP_CHARGING = "cheap_charging"
    FULL_SOC_CYCLING = "full_soc_cycling"
    PRE_DISCHARGE = "pre_discharge"


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
    charge_slot_current: ObservedCapability
    discharge_slot_current: ObservedCapability
    charge_slot_target_soc: ObservedCapability
    discharge_slot_target_soc: ObservedCapability
    maximum_output_power: ObservedCapability
    maximum_feed_in_power: ObservedCapability
    # These are optional for compatibility with T0002 callers.  A live Solis
    # observation is not healthy until both global current capabilities have
    # been read and validated.
    maximum_charge_current: ObservedCapability | None = None
    maximum_discharge_current: ObservedCapability | None = None

    @property
    def global_maximum_charge_current(self) -> ObservedCapability | None:
        return self.maximum_charge_current

    @property
    def global_maximum_discharge_current(self) -> ObservedCapability | None:
        return self.maximum_discharge_current


class CapabilityTarget:
    """Marker base for an explicit capability-derived target variant."""


@dataclass(frozen=True, slots=True)
class MaximumVerifiedValue(CapabilityTarget):
    pass


@dataclass(frozen=True, slots=True)
class DocumentedUnlimitedValue(CapabilityTarget):
    pass


@dataclass(frozen=True, slots=True)
class PreserveCurrentValue(CapabilityTarget):
    pass


@dataclass(frozen=True, slots=True)
class PreserveCurrentPolicyValue:
    """Explicitly preserve a persistent value during fail-safe application."""

    pass


MaximumVerified = MaximumVerifiedValue
DocumentedUnlimited = DocumentedUnlimitedValue
PreserveCurrent = PreserveCurrentValue
PreservePolicyValue = PreserveCurrentPolicyValue


@dataclass(frozen=True, slots=True)
class InverterPolicy:
    storage_mode: StorageMode
    grid_charge_allowed: bool | PreserveCurrentPolicyValue
    export_allowed: bool | PreserveCurrentPolicyValue
    over_discharge_soc: Decimal | PreserveCurrentPolicyValue
    force_charge_soc: Decimal | PreserveCurrentPolicyValue
    recovery_soc: Decimal | PreserveCurrentPolicyValue
    maximum_charge_soc: Decimal | PreserveCurrentPolicyValue
    battery_reserve_enabled: bool
    battery_reserve_soc: Decimal | PreserveCurrentPolicyValue
    output_power_target: CapabilityTarget
    feed_in_power_target: CapabilityTarget

    def __post_init__(self) -> None:
        for name in (
            "over_discharge_soc",
            "force_charge_soc",
            "recovery_soc",
            "maximum_charge_soc",
            "battery_reserve_soc",
        ):
            _validate_percent(getattr(self, name), name)


def _validate_percent(value: Decimal, name: str) -> None:
    if isinstance(value, PreserveCurrentPolicyValue):
        return
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


@dataclass(frozen=True, slots=True)
class DesiredInverterState:
    policy: InverterPolicy
    slot: SlotIntent | None
    phase: StrategyPhase
    reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if not self.reason:
            raise ValueError("reason must not be empty")


# Short aliases keep the vocabulary pleasant for adapters and callers.
Capability = ObservedCapability
CapabilityValue = ObservedCapability
RuntimeCapability = RuntimeCapabilities
PersistentInverterPolicy = InverterPolicy
TimedSlotIntent = SlotIntent
Health = ControllerHealth
Direction = SlotDirection
DesiredState = DesiredInverterState
