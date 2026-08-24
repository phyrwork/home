from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import replace

from custom_components.house_battery_control.contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from custom_components.house_battery_control.planner import CheapWindow
from custom_components.house_battery_control.strategy import CycleState, StrategyAction, StrategyInputs, select_strategy


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)


def _intent(owner, direction, start=NOW, *, minutes=30, target="80"):
    return SlotIntent(owner, 2 if owner is SlotOwner.RESERVE_EXPORT else 1, direction, start, start + timedelta(minutes=minutes), Decimal("5"), Decimal(target), start + timedelta(minutes=minutes))


def _window(*, start=NOW, minutes=60, margin="0.10"):
    return CheapWindow(start, start + timedelta(minutes=minutes), ()) if margin == "0" else CheapWindow(start, start + timedelta(minutes=minutes), (type("Component", (), {"margin_per_stored_kwh": Decimal(margin)})(),))


def _inputs(**changes):
    values = dict(
        now=NOW,
        health=ControllerHealth.HEALTHY,
        soc_percent=Decimal("50"),
        reserve_soc_percent=Decimal("10"),
        cheap_window=_window(),
        cheap_charge_intent=_intent(SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE),
        cycle_window=_window(),
        cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, minutes=10, target="20"),
        reserve_discharge_intent=_intent(SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE, minutes=30, target="10"),
        recharge_duration=timedelta(minutes=30),
        cycle_discharge_duration=timedelta(minutes=10),
    )
    values.update(changes)
    return StrategyInputs(**values)


def test_unhealthy_input_precedes_economic_actions():
    result = select_strategy(_inputs(health=ControllerHealth.DEGRADED))
    assert result.action is StrategyAction.FAIL_SAFE


def test_malformed_input_and_floor_breach_fail_safe():
    assert select_strategy(object()).action is StrategyAction.FAIL_SAFE
    result = select_strategy(_inputs(soc_percent=Decimal("9")))
    assert result.action is StrategyAction.FAIL_SAFE


def test_reserve_export_is_used_outside_cheap_window_and_has_stop_barrier():
    values = _inputs(
        cheap_window=None,
        cheap_charge_intent=None,
        cycle_window=None,
        cycle_discharge_intent=None,
        reserve_discharge_intent=_intent(SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE, target="10"),
        soc_percent=Decimal("50"),
    )
    result = select_strategy(values)
    assert result.action is StrategyAction.RESERVE_DISCHARGE
    assert result.slot is not None and result.slot.owner is SlotOwner.RESERVE_EXPORT
    assert result.next_cycle_state is CycleState.RESERVE_DISCHARGING

    again = select_strategy(replace(values, cycle_state=CycleState.RESERVE_DISCHARGING))
    assert again.action is StrategyAction.RESERVE_DISCHARGE

    stopped = select_strategy(replace(values, cycle_state=CycleState.RESERVE_DISCHARGING, cheap_window=_window(), cheap_charge_intent=_intent(SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE)))
    assert stopped.action is StrategyAction.STOP
    assert stopped.next_cycle_state is CycleState.STOPPING


def test_reserve_discharge_stops_at_reserve_soc():
    result = select_strategy(
        _inputs(
            cycle_state=CycleState.RESERVE_DISCHARGING,
            soc_percent=Decimal("10"),
            reserve_soc_percent=Decimal("10"),
            cheap_window=None,
        )
    )
    assert result.action is StrategyAction.STOP
    assert result.next_cycle_state is CycleState.STOPPING


def test_reserve_intent_target_is_an_effective_floor():
    values = _inputs(
        cheap_window=None,
        cheap_charge_intent=None,
        cycle_window=None,
        cycle_discharge_intent=None,
        reserve_soc_percent=Decimal("35"),
        reserve_discharge_intent=_intent(SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE, target="40"),
        soc_percent=Decimal("37"),
    )
    assert select_strategy(values).action is StrategyAction.IDLE
    active = select_strategy(replace(values, cycle_state=CycleState.RESERVE_DISCHARGING))
    assert active.action is StrategyAction.STOP
    assert active.next_cycle_state is CycleState.STOPPING


def test_charging_continues_below_full_then_stops_at_full_before_new_cycle():
    continuing = select_strategy(
        _inputs(cycle_state=CycleState.CHARGING, soc_percent=Decimal("99"))
    )
    assert continuing.action is StrategyAction.CHEAP_CHARGE
    assert continuing.next_cycle_state is CycleState.CHARGING

    complete = select_strategy(
        _inputs(cycle_state=CycleState.CHARGING, soc_percent=Decimal("100"))
    )
    assert complete.action is StrategyAction.STOP
    assert complete.next_cycle_state is CycleState.STOPPING


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


def test_cycle_deadline_is_fixed_and_not_slid_by_heartbeat():
    deadline = NOW + timedelta(minutes=10)
    result = select_strategy(
        _inputs(
            soc_percent=Decimal("100"),
            cycle_state=CycleState.DISCHARGING,
            cycle_deadline=deadline,
            cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, minutes=10),
            now=NOW + timedelta(minutes=5),
        )
    )
    assert result.action is StrategyAction.CYCLE_DISCHARGE
    assert result.slot is not None and result.slot.end == NOW + timedelta(minutes=10)

    stopped = select_strategy(
        _inputs(
            soc_percent=Decimal("100"),
            cycle_state=CycleState.DISCHARGING,
            cycle_deadline=deadline,
            cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, minutes=10),
            now=deadline,
        )
    )
    assert stopped.action is StrategyAction.STOP
    assert stopped.next_cycle_state is CycleState.STOPPING


def test_shortened_cycle_window_stops_and_never_returns_intent_past_window():
    short_window = _window(minutes=12)
    result = select_strategy(
        _inputs(
            now=NOW + timedelta(minutes=5),
            soc_percent=Decimal("100"),
            cycle_state=CycleState.DISCHARGING,
            cheap_window=short_window,
            cycle_window=short_window,
            cycle_deadline=NOW + timedelta(minutes=10),
            cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, minutes=30),
        )
    )
    assert result.action is StrategyAction.STOP

    enough_window = _window(minutes=50)
    continued = select_strategy(
        _inputs(
            now=NOW + timedelta(minutes=5),
            soc_percent=Decimal("100"),
            cycle_state=CycleState.DISCHARGING,
            cheap_window=enough_window,
            cycle_window=enough_window,
            cycle_deadline=NOW + timedelta(minutes=20),
            cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, minutes=30),
            recharge_duration=timedelta(0),
        )
    )
    assert continued.action is StrategyAction.CYCLE_DISCHARGE
    assert continued.slot is not None and continued.slot.end <= enough_window.end

    result = select_strategy(_inputs(cycle_state=CycleState.DISCHARGING, soc_percent=Decimal("80"), recharge_duration=timedelta(minutes=61)))
    assert result.action is StrategyAction.STOP


def test_discharge_target_is_raised_to_reserve_floor():
    result = select_strategy(_inputs(soc_percent=Decimal("100"), reserve_soc_percent=Decimal("30"), cycle_discharge_intent=_intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, target="5")))
    assert result.action is StrategyAction.CYCLE_DISCHARGE
    assert result.slot is not None and result.slot.target_soc == Decimal("30")
