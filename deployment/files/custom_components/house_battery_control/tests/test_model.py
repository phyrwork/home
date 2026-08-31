"""Behavior tests for the shared house-battery value model."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.house_battery_control.model import (
    ControllerHealth,
    FULL_SOC_PERCENT,
    LogicalIntent,
    MINIMUM_SOC_PERCENT,
    ObservedCapability,
    SlotDirection,
    SlotIntent,
    SlotOwner,
    StorageMode,
)


def capability() -> ObservedCapability:
    return ObservedCapability(
        current_value=Decimal("2"),
        minimum=Decimal("0"),
        maximum=Decimal("6"),
        step=Decimal("0.1"),
        unit="kW",
    )


def intent(**changes: object) -> SlotIntent:
    values: dict[str, object] = {
        "owner": SlotOwner.CHEAP_CHARGING,
        "direction": SlotDirection.CHARGE,
        "start": datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "end": datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        "current": Decimal("2"),
        "target_soc": Decimal("100"),
        "expiry": datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
    }
    values.update(changes)
    return SlotIntent(**values)  # type: ignore[arg-type]


def test_shared_safety_bounds_are_exact() -> None:
    assert (FULL_SOC_PERCENT, MINIMUM_SOC_PERCENT) == (100, 10)


def test_capability_validation_and_value_semantics() -> None:
    assert capability() == capability()
    with pytest.raises(ValueError):
        ObservedCapability(Decimal("7"), Decimal("0"), Decimal("6"), Decimal("1"), "kW")
    with pytest.raises(ValueError):
        ObservedCapability(Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1"), "kW")


def test_slot_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        intent(current=Decimal("-1"))
    with pytest.raises(ValueError):
        intent(target_soc=Decimal("101"))
    with pytest.raises(ValueError):
        intent(
            start=datetime(2026, 1, 1, 1),
            end=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError):
        intent(end=datetime(2026, 1, 1, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        intent(expiry=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))


def test_enums_retain_the_runtime_surface() -> None:
    assert set(ControllerHealth) == {
        ControllerHealth.HEALTHY,
        ControllerHealth.DEGRADED,
        ControllerHealth.FAIL_SAFE,
    }
    assert set(SlotOwner) == {
        SlotOwner.CHEAP_CHARGING,
        SlotOwner.FULL_SOC_CYCLING,
        SlotOwner.RESERVE_EXPORT,
    }
    assert set(SlotDirection) == {SlotDirection.CHARGE, SlotDirection.DISCHARGE}
    assert [mode.value for mode in StorageMode] == ["Self-Use", "Feed-In Priority"]


def test_logical_intent_accepts_adjacent_same_direction_segments() -> None:
    first = intent()
    second = intent(
        start=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
        expiry=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
    )
    logical = LogicalIntent((first, second))
    assert (logical.start, logical.end) == (first.start, second.end)


def test_logical_intent_accepts_only_the_narrow_adjacent_cycle_pair() -> None:
    discharge = intent(
        owner=SlotOwner.FULL_SOC_CYCLING,
        direction=SlotDirection.DISCHARGE,
        target_soc=Decimal("20"),
    )
    charge = intent(
        start=discharge.end,
        end=discharge.end + timedelta(hours=1),
        expiry=discharge.end + timedelta(hours=1),
    )
    pair = LogicalIntent((discharge, charge))
    assert pair.segments == (discharge, charge)


def test_logical_intent_rejects_overlap_or_mixed_direction() -> None:
    first = intent()
    with pytest.raises(ValueError):
        LogicalIntent(
            (
                first,
                intent(
                    start=datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc),
                    end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
                    expiry=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
                ),
            )
        )
    with pytest.raises(ValueError):
        LogicalIntent(
            (
                first,
                intent(
                    start=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
                    end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
                    expiry=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
                    direction=SlotDirection.DISCHARGE,
                ),
            )
        )
