"""Pure result types for the read-only Solis state boundary.

Nothing in this module knows about Home Assistant.  The adapter turns external
state into these immutable values and records unsafe observations as issues.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import Enum

from .contracts import (
    ControllerHealth,
    ObservedCapability,
    RuntimeCapabilities,
    SlotDirection,
)
from .solis_config import SolisSlotOwner


MAXIMUM_TELEMETRY_AGE = timedelta(minutes=5)
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

    @property
    def related_entity_id(self) -> str | None:
        return self.entity_id

    @property
    def explanation(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class SolisTelemetry:
    """Normalized telemetry; battery power is kW, positive while charging."""

    state_of_charge_percent: Decimal
    battery_power_kw: Decimal
    device_timestamp: datetime | None
    home_assistant_last_updated: datetime | None
    soc_last_updated: datetime | None = None
    power_last_updated: datetime | None = None

    @property
    def soc_percent(self) -> Decimal:
        return self.state_of_charge_percent

    @property
    def power_kw(self) -> Decimal:
        return self.battery_power_kw


@dataclass(frozen=True, slots=True)
class SolisPersistentState:
    """Observed persistent and protection controls."""

    storage_mode: str
    storage_mode_options: tuple[str, ...]
    allow_grid_charging: bool
    allow_export: bool
    grid_peak_shaving: bool
    inverter_on_off: bool
    inverter_time: datetime
    over_discharge_soc: ObservedCapability
    force_charge_soc: ObservedCapability
    recovery_soc: ObservedCapability
    maximum_charge_soc: ObservedCapability
    battery_reserve: bool
    battery_reserve_soc: ObservedCapability


@dataclass(frozen=True, slots=True)
class SolisSlotCapability:
    """Capabilities for both directions of one physical Solis slot."""

    physical_slot: int
    charge_current: ObservedCapability
    charge_target_soc: ObservedCapability
    discharge_current: ObservedCapability
    discharge_target_soc: ObservedCapability


@dataclass(frozen=True, slots=True)
class SolisSlotDirectionState:
    """Observed state of one charge or discharge direction."""

    physical_slot: int
    direction: SlotDirection
    owner: SolisSlotOwner
    enabled: bool
    time_text: str
    start: time | None
    end: time | None
    crosses_midnight: bool
    current: ObservedCapability
    target_soc: ObservedCapability


@dataclass(frozen=True, slots=True)
class SolisSlotState:
    """Observed state and capabilities for one physical slot."""

    physical_slot: int
    charge: SolisSlotDirectionState
    discharge: SolisSlotDirectionState
    capability: SolisSlotCapability


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

    @property
    def is_healthy(self) -> bool:
        return self.health is ControllerHealth.HEALTHY


__all__ = [
    "MAXIMUM_FUTURE_CLOCK_SKEW",
    "MAXIMUM_TELEMETRY_AGE",
    "IssueSeverity",
    "SolisIssue",
    "SolisPersistentState",
    "SolisSlotCapability",
    "SolisSlotDirectionState",
    "SolisSlotState",
    "SolisStateReadResult",
    "SolisStateSnapshot",
    "SolisTelemetry",
]
