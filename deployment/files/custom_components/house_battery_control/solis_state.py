"""Pure result types for the read-only Solis state boundary.

Nothing in this module knows about Home Assistant.  The adapter turns external
state into these immutable values and records unsafe observations as issues.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from .contracts import (
    ControllerHealth,
    ObservedCapability,
    RuntimeCapabilities,
    SlotDirection,
)
from .solis_config import SolisSlotOwner


# Solis Inverter polls at five-minute intervals, but SolisCloud can return a
# successful response whose device timestamp is already about 15 minutes old.
# Allow six intervals for that observed delivery lag and additional scheduling
# jitter; older device data still fails closed.
MAXIMUM_TELEMETRY_AGE = timedelta(minutes=30)
MAXIMUM_FUTURE_CLOCK_SKEW = timedelta(minutes=1)


class IssueSeverity(str, Enum):
    """Severity of an observation issue."""

    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class SolisIssue:
    """A deterministic, user-facing problem found while reading Solis state."""

    code: str
    severity: IssueSeverity
    entity_id: str | None
    message: str

    @property
    def critical(self) -> bool:
        return self.severity is IssueSeverity.CRITICAL

@dataclass(frozen=True, slots=True)
class SolisTelemetry:
    """Normalized telemetry; battery power is kW, positive while charging."""

    state_of_charge_percent: Decimal
    battery_power_kw: Decimal
    battery_voltage_v: Decimal
    device_timestamp: datetime | None

@dataclass(frozen=True, slots=True)
class SolisPersistentState:
    """Observed persistent and protection controls."""

    storage_mode: str
    inverter_time: datetime
    grid_peak_shaving: bool
    battery_reserve: bool
    battery_reserve_soc: ObservedCapability


@dataclass(frozen=True, slots=True)
class SolisSlotDirectionState:
    """Observed state of one charge or discharge direction."""

    physical_slot: int
    direction: SlotDirection
    owner: SolisSlotOwner
    enabled: bool
    time_text: str
    current: ObservedCapability
    target_soc: ObservedCapability


@dataclass(frozen=True, slots=True)
class SolisSlotState:
    """Observed state and capabilities for one physical slot."""

    physical_slot: int
    charge: SolisSlotDirectionState
    discharge: SolisSlotDirectionState


@dataclass(frozen=True, slots=True)
class SolisStateSnapshot:
    """A complete, internally consistent Solis observation."""

    telemetry: SolisTelemetry
    persistent: SolisPersistentState
    capabilities: RuntimeCapabilities
    slots: tuple[SolisSlotState, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SolisStateReadResult:
    """Reader output, including safe partial observations and all issues."""

    health: ControllerHealth
    snapshot: SolisStateSnapshot | None
    telemetry: SolisTelemetry | None
    persistent: SolisPersistentState | None
    slots: tuple[SolisSlotState, ...]
    issues: tuple[SolisIssue, ...]

__all__ = [
    "MAXIMUM_FUTURE_CLOCK_SKEW",
    "MAXIMUM_TELEMETRY_AGE",
    "IssueSeverity",
    "SolisIssue",
    "SolisPersistentState",
    "SolisSlotDirectionState",
    "SolisSlotState",
    "SolisStateReadResult",
    "SolisStateSnapshot",
    "SolisTelemetry",
]
