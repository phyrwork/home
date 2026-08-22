from datetime import datetime, timedelta, timezone
from decimal import Decimal

from custom_components.house_battery_control.octopus_windows import CheapWindow
from custom_components.house_battery_control.pre_discharge import (
    PreDischargePlanningStatus,
    plan_pre_discharge,
)


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
WINDOW = CheapWindow(NOW + timedelta(hours=2), NOW + timedelta(hours=6), ())


def args(**changes):
    values = dict(
        now=NOW,
        current_energy_kwh=Decimal("30"),
        expected_window_start_energy_kwh=Decimal("30"),
        reserve_energy_kwh=Decimal("3.2"),
        capacity_kwh=Decimal("32"),
        maximum_charge_power_kw=Decimal("5"),
        maximum_discharge_power_kw=Decimal("5"),
        charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"),
        window=WINDOW,
        minimum_target_soc=Decimal("10"),
        target_soc_step=Decimal("5"),
    )
    values.update(changes)
    return values


def test_plans_headroom_only_for_energy_the_window_can_replace() -> None:
    result = plan_pre_discharge(**args())

    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.proposed_start is not None
    assert result.proposed_end == WINDOW.start.replace(second=0, microsecond=0)
    assert result.target_soc_percent == Decimal("45")
    assert result.headroom_kwh == Decimal("17")


def test_no_headroom_when_expected_energy_is_already_below_target() -> None:
    result = plan_pre_discharge(**args(expected_window_start_energy_kwh=Decimal("10")))
    assert result.status is PreDischargePlanningStatus.NO_HEADROOM_NEEDED


def test_never_rounds_target_below_absolute_safety_floor() -> None:
    result = plan_pre_discharge(**args(expected_window_start_energy_kwh=Decimal("32"), reserve_energy_kwh=Decimal("0")))
    assert result.target_soc_percent is not None
    assert result.target_soc_percent >= Decimal("10")


def test_invalid_window_is_safe_result() -> None:
    result = plan_pre_discharge(**args(window=CheapWindow(NOW, NOW + timedelta(hours=1), ())))
    assert result.status is PreDischargePlanningStatus.INFEASIBLE
