"""Focused tests for the pure house-battery domain contracts."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.house_battery_control import contracts
from custom_components.house_battery_control import model
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
    with pytest.raises(ValueError):
        intent(expiry=datetime(2026, 1, 1, 1, 59, tzinfo=timezone.utc))


def test_enums_and_structural_exclusivity() -> None:
    assert set(contracts.ControllerHealth) == {
        contracts.ControllerHealth.HEALTHY,
        contracts.ControllerHealth.DEGRADED,
        contracts.ControllerHealth.FAIL_SAFE,
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
        "Self-Use", "Feed-In Priority"
    ]


def test_logical_intent_accepts_adjacent_same_direction_segments() -> None:
    first = intent(end=datetime(2026, 1, 1, 2, tzinfo=timezone.utc), expiry=datetime(2026, 1, 1, 2, tzinfo=timezone.utc))
    second = intent(start=datetime(2026, 1, 1, 2, tzinfo=timezone.utc), end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc), expiry=datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
    logical = contracts.LogicalIntent((first, second))
    assert logical.start == first.start
    assert logical.end == second.end


def test_logical_intent_rejects_overlap_or_mixed_direction() -> None:
    first = intent(end=datetime(2026, 1, 1, 2, tzinfo=timezone.utc), expiry=datetime(2026, 1, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        contracts.LogicalIntent((first, intent(start=datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc), end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc), expiry=datetime(2026, 1, 1, 3, tzinfo=timezone.utc))))
    with pytest.raises(ValueError):
        contracts.LogicalIntent((first, intent(start=datetime(2026, 1, 1, 2, tzinfo=timezone.utc), end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc), expiry=datetime(2026, 1, 1, 3, tzinfo=timezone.utc), direction=contracts.SlotDirection.DISCHARGE)))


def test_new_shared_slot_intent_has_no_physical_slot_ownership() -> None:
    shared = model.SlotIntent(
        owner=model.SlotOwner.CHEAP_CHARGING,
        direction=model.SlotDirection.CHARGE,
        start=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        current=Decimal("2"),
        target_soc=Decimal("100"),
        expiry=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
    )
    assert not hasattr(shared, "physical_slot")
