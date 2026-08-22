from datetime import datetime, timedelta, timezone
from decimal import Decimal

from custom_components.house_battery_control.contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from custom_components.house_battery_control.octopus_windows import CheapWindow
from custom_components.house_battery_control.pre_discharge import PreDischargePlanResult, PreDischargePlanningStatus
from custom_components.house_battery_control.strategy import CycleState, StrategyAction, StrategyInputs, select_strategy


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)


def _intent(owner, direction, start=NOW, *, minutes=30, target="80"):
    return SlotIntent(owner, 1 if owner is not SlotOwner.PRE_DISCHARGE else 2, direction, start, start + timedelta(minutes=minutes), Decimal("5"), Decimal(target), start + timedelta(minutes=minutes))


def _window(*, start=NOW, minutes=60, margin="0.10"):
    return CheapWindow(start, start + timedelta(minutes=minutes), ()) if margin == "0" else CheapWindow(start, start + timedelta(minutes=minutes), (type("Component", (), {"margin_per_stored_kwh": Decimal(margin)})(),))


def _inputs(**changes):
    values = dict(
        now=NOW,
        health=ControllerHealth.HEALTHY,
        control_enabled=True,
        guard_off=True,
        soc_percent=Decimal("50"),
        reserve_soc_percent=Decimal("10"),
        cheap_window=_window(),
        cheap_charge_intent=_intent(SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE),
        cycle_window=_window(),
        cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, minutes=10, target="20"),
        recharge_duration=timedelta(minutes=30),
    )
    values.update(changes)
    return StrategyInputs(**values)


def test_fail_safe_precedes_economic_actions():
    result = select_strategy(_inputs(control_enabled=False, soc_percent=Decimal("100")))
    assert result.action is StrategyAction.FAIL_SAFE
    assert result.slot is None

    result = select_strategy(_inputs(health=ControllerHealth.DEGRADED, guard_off=False))
    assert result.action is StrategyAction.FAIL_SAFE


def test_malformed_input_and_floor_breach_fail_safe():
    assert select_strategy(object()).action is StrategyAction.FAIL_SAFE
    result = select_strategy(_inputs(soc_percent=Decimal("9")))
    assert result.action is StrategyAction.FAIL_SAFE


def test_pre_discharge_precedes_cheap_charge_when_its_window_is_active():
    plan = PreDischargePlanResult(PreDischargePlanningStatus.PLANNED, proposed_start=NOW, proposed_end=NOW + timedelta(minutes=20))
    values = _inputs(pre_discharge_plan=plan, pre_discharge_intent=_intent(SlotOwner.PRE_DISCHARGE, SlotDirection.DISCHARGE))
    result = select_strategy(values)
    assert result.action is StrategyAction.PRE_DISCHARGE
    assert result.slot is not None and result.slot.owner is SlotOwner.PRE_DISCHARGE
    assert result.next_cycle_state is CycleState.IDLE

    # The same plan remains active on the next evaluation; it must not enter
    # the full-SOC cycle continuation branch and stop prematurely.
    again = select_strategy(values)
    assert again.action is StrategyAction.PRE_DISCHARGE
    assert again.slot is not None and again.slot.owner is SlotOwner.PRE_DISCHARGE
    assert again.next_cycle_state is CycleState.IDLE


def test_cheap_charge_is_exclusive_and_requires_not_full():
    result = select_strategy(_inputs())
    assert result.action is StrategyAction.CHEAP_CHARGE
    assert result.slot is not None and result.slot.direction is SlotDirection.CHARGE
    assert result.next_cycle_state is CycleState.CHARGING

    result = select_strategy(_inputs(soc_percent=Decimal("100")))
    assert result.action is StrategyAction.CYCLE_DISCHARGE
    assert result.slot is not None and result.slot.direction is SlotDirection.DISCHARGE


def test_cycle_requires_profit_and_time_to_recharge():
    result = select_strategy(_inputs(soc_percent=Decimal("100"), recharge_duration=timedelta(minutes=55)))
    assert result.action is StrategyAction.IDLE

    result = select_strategy(_inputs(soc_percent=Decimal("100"), cycle_window=_window(margin="0")))
    assert result.action is StrategyAction.IDLE


def test_direction_change_always_stops_before_new_direction():
    result = select_strategy(_inputs(cycle_state=CycleState.CHARGING, soc_percent=Decimal("100")))
    assert result.action is StrategyAction.STOP
    assert result.slot is None
    assert result.next_cycle_state is CycleState.STOPPING

    result = select_strategy(_inputs(cycle_state=CycleState.STOPPING, soc_percent=Decimal("100")))
    assert result.action is StrategyAction.STOP
    assert result.next_cycle_state is CycleState.IDLE


def test_active_cycle_stops_at_target_or_when_window_cannot_refill():
    result = select_strategy(_inputs(cycle_state=CycleState.DISCHARGING, soc_percent=Decimal("20")))
    assert result.action is StrategyAction.STOP
    assert result.next_cycle_state is CycleState.STOPPING

    result = select_strategy(_inputs(cycle_state=CycleState.DISCHARGING, soc_percent=Decimal("80"), recharge_duration=timedelta(minutes=61)))
    assert result.action is StrategyAction.STOP


def test_discharge_target_is_raised_to_reserve_floor():
    result = select_strategy(_inputs(soc_percent=Decimal("100"), reserve_soc_percent=Decimal("30"), cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, target="5")))
    assert result.action is StrategyAction.CYCLE_DISCHARGE
    assert result.slot is not None and result.slot.target_soc == Decimal("30")


def test_non_planned_pre_discharge_is_ignored():
    plan = PreDischargePlanResult(PreDischargePlanningStatus.NO_HEADROOM_NEEDED, proposed_start=NOW, proposed_end=NOW + timedelta(minutes=20))
    result = select_strategy(_inputs(pre_discharge_plan=plan, pre_discharge_intent=_intent(SlotOwner.PRE_DISCHARGE, SlotDirection.DISCHARGE)))
    assert result.action is StrategyAction.CHEAP_CHARGE
