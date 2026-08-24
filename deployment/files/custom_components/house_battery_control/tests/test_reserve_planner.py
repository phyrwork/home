from datetime import datetime, timedelta, timezone
from decimal import Decimal

from custom_components.house_battery_control.planner import (
    CheapClassification,
    ReserveInputInterval,
    TimeInterval,
    plan_reserve,
)


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


def interval(hours: int, *, load: str, solar: str = "0", classification=CheapClassification.NOT_CHEAP):
    start = NOW + timedelta(hours=hours)
    return ReserveInputInterval(
        TimeInterval(start, start + timedelta(hours=1)),
        Decimal(load),
        Decimal(solar),
        classification,
    )


def plan(items):
    return plan_reserve(
        intervals=items,
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal("0"),
        charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"),
        maximum_charge_power_kw=Decimal("5"),
        maximum_discharge_power_kw=Decimal("5"),
    )


def test_reverse_plan_reserves_for_load_above_grid_shaving_limit() -> None:
    result = plan((interval(0, load="1"),))

    assert result.issue is None
    assert result.reserve_energy_kwh == Decimal("1.947368421052631578947368421")


def test_cheap_window_can_refill_reserve_at_owned_power() -> None:
    result = plan((interval(0, load="0", classification=CheapClassification.STANDARD_CHEAP),))

    assert result.issue is None
    assert result.reserve_energy_kwh == Decimal("1")


def test_surplus_external_pv_reduces_required_reserve() -> None:
    result = plan((interval(0, load="0", solar="2"),))

    assert result.issue is None
    assert result.reserve_energy_kwh == Decimal("1")


def test_invalid_or_infeasible_forecast_is_reported_without_raising() -> None:
    invalid = plan_reserve(
        intervals=(),
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal("0"),
        charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"),
        maximum_charge_power_kw=Decimal("5"),
        maximum_discharge_power_kw=Decimal("5"),
    )
    assert invalid.reserve_energy_kwh is None
    assert invalid.issue == "at least one forecast interval is required"

    impossible = plan((interval(0, load="6"),))
    assert impossible.reserve_energy_kwh is None
    assert impossible.issue == "forecast demand exceeds battery and grid power"
