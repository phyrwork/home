"""Focused tests for the pure house-battery domain contracts."""

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.house_battery_control import contracts
from custom_components.house_battery_control.domain_constants import (
    BATTERY_CYCLE_COST_PER_KWH,
    FORCE_CHARGE_SOC_PERCENT,
    FULL_SOC_PERCENT,
    MAXIMUM_GRID_IMPORT_POWER_KW,
    MINIMUM_SOC_PERCENT,
)


def capability() -> contracts.ObservedCapability:
    return contracts.ObservedCapability(
        current_value=Decimal("2"),
        minimum=Decimal("0"),
        maximum=Decimal("6"),
        step=Decimal("0.1"),
        unit="kW",
    )


def intent(**changes: object) -> contracts.SlotIntent:
    values: dict[str, object] = {
        "owner": contracts.SlotOwner.CHEAP_CHARGING,
        "physical_slot": 1,
        "direction": contracts.SlotDirection.CHARGE,
        "start": datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "end": datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        "current": Decimal("2"),
        "target_soc": Decimal("100"),
        "expiry": datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
    }
    values.update(changes)
    return contracts.SlotIntent(**values)  # type: ignore[arg-type]


def test_policy_constants_are_exact() -> None:
    assert (FULL_SOC_PERCENT, MINIMUM_SOC_PERCENT, FORCE_CHARGE_SOC_PERCENT) == (100, 10, 7)
    assert MAXIMUM_GRID_IMPORT_POWER_KW == Decimal("0.1")
    assert isinstance(MAXIMUM_GRID_IMPORT_POWER_KW, Decimal)
    assert BATTERY_CYCLE_COST_PER_KWH == Decimal("0.0165")
    assert str(BATTERY_CYCLE_COST_PER_KWH) == "0.0165"


def test_capability_validation_and_value_semantics() -> None:
    assert capability() == capability()
    with pytest.raises(ValueError):
        contracts.ObservedCapability(Decimal("7"), Decimal("0"), Decimal("6"), Decimal("1"), "kW")
    with pytest.raises(ValueError):
        contracts.ObservedCapability(Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1"), "kW")


def test_slot_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        intent(physical_slot=0)
    with pytest.raises(ValueError):
        intent(current=Decimal("-1"))
    with pytest.raises(ValueError):
        intent(target_soc=Decimal("101"))
    with pytest.raises(ValueError):
        intent(start=datetime(2026, 1, 1, 1), end=datetime(2026, 1, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        intent(end=datetime(2026, 1, 1, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        intent(expiry=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
    assert intent(expiry=datetime(2026, 1, 1, 1, 59, tzinfo=timezone.utc)).expiry < intent().end


def test_enums_and_structural_exclusivity() -> None:
    assert set(contracts.ControllerHealth) == {
        contracts.ControllerHealth.HEALTHY,
        contracts.ControllerHealth.DEGRADED,
        contracts.ControllerHealth.FAIL_SAFE,
    }
    assert set(contracts.StrategyPhase) == {
        contracts.StrategyPhase.OBSERVING,
        contracts.StrategyPhase.IDLE,
        contracts.StrategyPhase.RESERVE_DISCHARGE,
        contracts.StrategyPhase.OFF_PEAK_CHARGE,
        contracts.StrategyPhase.OFF_PEAK_CYCLE_DISCHARGE,
        contracts.StrategyPhase.FINAL_CHARGE,
        contracts.StrategyPhase.FAIL_SAFE,
    }
    assert set(contracts.SlotOwner) == {
        contracts.SlotOwner.CHEAP_CHARGING,
        contracts.SlotOwner.FULL_SOC_CYCLING,
        contracts.SlotOwner.RESERVE_EXPORT,
    }
    assert set(contracts.SlotDirection) == {
        contracts.SlotDirection.CHARGE,
        contracts.SlotDirection.DISCHARGE,
    }
    assert [mode.value for mode in contracts.StorageMode] == [
        "Self-Use", "Feed-In Priority", "Off-Grid"
    ]
    policy = contracts.InverterPolicy(
        contracts.StorageMode.SELF_USE,
        True,
        True,
        Decimal("10"),
        Decimal("7"),
        Decimal("8"),
        Decimal("100"),
        True,
        Decimal("10"),
        contracts.MaximumVerifiedValue(),
        contracts.PreserveCurrentValue(),
    )
    state = contracts.DesiredInverterState(
        policy, intent(direction=contracts.SlotDirection.DISCHARGE), contracts.StrategyPhase.IDLE,
        "test", datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert state.slot.direction is contracts.SlotDirection.DISCHARGE
    assert isinstance(policy.output_power_target, contracts.CapabilityTarget)
    assert isinstance(policy.feed_in_power_target, contracts.PreserveCurrentValue)
    assert isinstance(contracts.MaximumVerifiedValue(), contracts.CapabilityTarget)
    assert isinstance(contracts.DocumentedUnlimitedValue(), contracts.CapabilityTarget)
    assert isinstance(contracts.PreserveCurrentValue(), contracts.CapabilityTarget)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.reason = "changed"  # type: ignore[misc]
