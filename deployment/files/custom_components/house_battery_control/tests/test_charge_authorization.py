"""Settlement evidence, independent interval permission, and actuator backstops."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.house_battery_control.charge_authorization import (
    ChargeAuthorization, ChargeGuard, QualifiedHalfHour, settlement_start,
)
from custom_components.house_battery_control.controller import Controller
from custom_components.house_battery_control.model import SlotDirection, StrategyAction
from custom_components.house_battery_control.solis import NativeSchedule, SlotKey
from custom_components.house_battery_control.tests.test_controller import (
    adapter, config, observation, plan,
)
from custom_components.house_battery_control.tests.test_planner import _build
from custom_components.house_battery_control.tests.test_solis import intent

UTC = timezone.utc


def dt(value):
    return datetime.fromisoformat(value).astimezone(UTC)


NOW = dt("2026-09-22T21:09:00+01:00")


def inputs(at=NOW, *, watts="7464", smart="SMART_CONTROL_IN_PROGRESS", power_at=None, retrieved_at=None,
           error=None, restored=False):
    cfg = config().charge_guard
    states = {
        cfg.ev_power_entity_id: SimpleNamespace(state=watts, last_reported=power_at or at, attributes={"restored": restored}),
        cfg.intelligent_state_entity_id: SimpleNamespace(state=smart, attributes={}),
        cfg.dispatches_retrieved_entity_id: SimpleNamespace(state=(retrieved_at or at).isoformat(), attributes={"last_error": error}),
    }
    return SimpleNamespace(states=SimpleNamespace(get=states.get))


@pytest.mark.parametrize("watts,smart", [
    ("2.8", "SMART_CONTROL_IN_PROGRESS"), ("50", "SMART_CONTROL_IN_PROGRESS"),
    ("NaN", "SMART_CONTROL_IN_PROGRESS"), ("Infinity", "SMART_CONTROL_IN_PROGRESS"),
    ("unavailable", "SMART_CONTROL_IN_PROGRESS"), ("7464", "BOOSTING"),
    ("7464", "unknown"), ("7464", "SMART_CONTROL_CAPABLE"),
])
def test_nonqualifying_values(watts, smart):
    guard = ChargeGuard(NOW - timedelta(minutes=10))
    auth = guard.observe(inputs(watts=watts, smart=smart), config().charge_guard, NOW)
    assert auth.authorized_until(NOW) is None


@pytest.mark.parametrize("kwargs", [
    {"power_at": NOW - timedelta(minutes=3)},
    {"retrieved_at": NOW - timedelta(minutes=6)},
    {"power_at": NOW + timedelta(seconds=1)},
    {"retrieved_at": NOW + timedelta(seconds=1)},
    {"error": "API failed"}, {"restored": True},
])
def test_invalid_evidence_does_not_qualify(kwargs):
    guard = ChargeGuard(NOW - timedelta(minutes=10))
    assert guard.observe(inputs(**kwargs), config().charge_guard, NOW).qualified is None


def test_previous_period_report_cannot_qualify_even_when_only_one_second_old():
    now = dt("2026-09-22T21:30:00+01:00")
    for name in ("power_at", "retrieved_at"):
        guard = ChargeGuard(NOW)
        assert guard.observe(inputs(now, **{name: now - timedelta(seconds=1)}), config().charge_guard, now).qualified is None


def test_restart_requires_post_start_evidence_from_both_inputs():
    guard = ChargeGuard(NOW)
    for name in ("power_at", "retrieved_at"):
        assert guard.observe(inputs(NOW, **{name: NOW - timedelta(seconds=1)}), config().charge_guard, NOW).qualified is None
    assert guard.observe(inputs(NOW + timedelta(seconds=1)), config().charge_guard, NOW + timedelta(seconds=1)).qualified


def test_nonoverlapping_conditions_do_not_qualify():
    guard = ChargeGuard(NOW)
    assert guard.observe(inputs(watts="3"), config().charge_guard, NOW).qualified is None
    assert guard.observe(inputs(NOW + timedelta(seconds=1), smart="BOOSTING"), config().charge_guard, NOW + timedelta(seconds=1)).qualified is None


@pytest.mark.parametrize("later_smart", ["BOOSTING", "unavailable", "SMART_CONTROL_IN_PROGRESS"])
def test_real_incident_latches_then_expires_despite_scheduled_smart_state(later_smart):
    guard = ChargeGuard(NOW - timedelta(minutes=1))
    first = guard.observe(inputs(), config().charge_guard, NOW)
    assert first.charge_is_authorized(NOW, dt("2026-09-22T21:30:00+01:00"))
    assert not first.charge_is_authorized(NOW, dt("2026-09-22T21:30:01+01:00"))
    stopped = NOW + timedelta(minutes=2)
    assert guard.observe(inputs(stopped, watts="3.1", smart=later_smart), config().charge_guard, stopped).qualified == first.qualified
    for hour, minute in [(21, 30), (22, 0), (22, 30), (23, 0)]:
        at = dt(f"2026-09-22T{hour:02}:{minute:02}:01+01:00")
        assert not guard.observe(inputs(at, watts="3.1"), config().charge_guard, at).charge_is_authorized(at, at + timedelta(minutes=10))
    overnight = dt("2026-09-22T23:30:00+01:00")
    assert guard.observe(inputs(overnight, watts="3.1"), config().charge_guard, overnight).charge_is_authorized(overnight, overnight + timedelta(hours=6))


def test_same_values_can_qualify_following_period_with_new_report_times():
    guard = ChargeGuard(NOW)
    assert guard.observe(inputs(), config().charge_guard, NOW).qualified
    boundary = dt("2026-09-22T21:30:00+01:00")
    assert guard.observe(inputs(), config().charge_guard, boundary).qualified is None
    assert guard.observe(inputs(boundary), config().charge_guard, boundary).qualified.start == boundary


@pytest.mark.parametrize("start,end,hours", [
    ("2026-09-22T23:30:00+01:00", "2026-09-23T05:30:00+01:00", 6),
    ("2026-03-28T23:30:00+00:00", "2026-03-29T05:30:00+01:00", 5),
    ("2026-10-24T23:30:00+01:00", "2026-10-25T05:30:00+00:00", 7),
])
def test_overnight_half_open_coverage_and_dst(start, end, hours):
    start, end = dt(start), dt(end)
    auth = ChargeAuthorization(start)
    assert auth.charge_is_authorized(start, end)
    assert not auth.charge_is_authorized(start - timedelta(seconds=1), end)
    assert not auth.charge_is_authorized(start, end + timedelta(seconds=1))
    assert not auth.charge_is_authorized(end, end + timedelta(seconds=1))
    assert (end - start).total_seconds() == hours * 3600


def test_dst_fold_has_distinct_settlement_keys():
    assert settlement_start(dt("2026-10-25T01:10:00+01:00")) != settlement_start(dt("2026-10-25T01:10:00+00:00"))


def qualified(now):
    start = settlement_start(now)
    return ChargeAuthorization(now, QualifiedHalfHour(start, start + timedelta(minutes=30), now, now, now))


@pytest.mark.asyncio
async def test_charge_lease_clipped_to_half_hour_and_cycle_recharge_cannot_cross(hass, monkeypatch):
    from custom_components.house_battery_control.tests import test_planner as fixture
    now = dt("2026-09-22T21:20:00+01:00")
    monkeypatch.setattr(fixture, "NOW", now)
    ordinary = await _build(hass, cheap=True, bonus=True, now=now, authorization=qualified(now))
    assert ordinary.issue is None
    assert ordinary.intent.end == dt("2026-09-22T21:30:00+01:00")
    assert ordinary.charge_lease_deadline == ordinary.intent.end
    cycle = await _build(hass, cheap=True, bonus=True, soc="100", now=now, authorization=qualified(now))
    assert cycle.issue is None
    assert cycle.action is StrategyAction.RESERVE_DISCHARGE
    assert all(s.direction is SlotDirection.DISCHARGE for s in cycle.intent.segments)


@pytest.mark.asyncio
async def test_discharge_before_static_window_can_pair_with_authorized_recharge(hass, monkeypatch):
    from custom_components.house_battery_control.tests import test_planner as fixture
    now = dt("2026-09-22T23:20:00+01:00")
    monkeypatch.setattr(fixture, "NOW", now)
    result = await _build(hass, cheap=True, bonus=True, soc="100", now=now, authorization=ChargeAuthorization(now))
    assert result.issue is None
    assert result.action is StrategyAction.CYCLE_DISCHARGE
    discharge, recharge = result.intent.segments
    assert discharge.start == now
    assert recharge.start == dt("2026-09-22T23:30:00+01:00")


@pytest.mark.asyncio
async def test_unqualified_native_charge_stops_before_forecast_or_stale_soc_handling(hass):
    now = dt("2026-09-22T21:30:00+01:00")
    controller = Controller(hass, config())
    writer = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    observed = replace(observation(enabled=key, target_state="100", time_state="20:30-20:45"), telemetry=None)
    with patch.object(Controller, "_now", return_value=now), patch(
        "custom_components.house_battery_control.controller.read_state", return_value=observed
    ), patch("custom_components.house_battery_control.controller.build_plan", AsyncMock()) as build:
        await controller._reconcile()
    writer.stop.assert_awaited_once()
    build.assert_not_awaited()


def test_native_schedule_cannot_cross_permission_boundary(hass):
    now = dt("2026-09-22T21:20:00+01:00")
    controller = Controller(hass, config())
    controller._zone = UTC
    controller._charge_authorization = qualified(now)
    assert controller._native_charge_authorized(NativeSchedule(20 * 60 + 10, 20 * 60 + 30), now)
    assert not controller._native_charge_authorized(NativeSchedule(20 * 60 + 10, 20 * 60 + 31), now)


@pytest.mark.asyncio
async def test_delayed_enable_rechecks_actual_clock(hass):
    controller = Controller(hass, config())
    writer = adapter(controller)
    start = dt("2026-09-22T21:20:00+01:00")
    end = dt("2026-09-22T21:30:00+01:00")
    candidate = replace(plan(), action=StrategyAction.CHEAP_CHARGE, intent=intent(start=start, end=end))
    change = SimpleNamespace(entity_id="switch.charge", target=True)
    with patch.object(Controller, "_now", return_value=end):
        await controller._attempt_start(change, "old", candidate, observation(), start, 0)
    writer.apply.assert_not_awaited()
    assert controller._dirty


@pytest.mark.asyncio
async def test_callback_retains_brief_overlap_while_writer_is_busy(hass, freezer):
    freezer.move_to(NOW)
    with patch.object(Controller, "_now", return_value=NOW):
        controller = Controller(hass, config())
        cfg = controller.config.charge_guard
        hass.states.async_set(cfg.ev_power_entity_id, "7000")
        hass.states.async_set(cfg.intelligent_state_entity_id, "SMART_CONTROL_IN_PROGRESS")
        hass.states.async_set(cfg.dispatches_retrieved_entity_id, NOW.isoformat())
        with patch.object(controller, "trigger") as trigger:
            controller._source_changed(None)
            recorded = controller._charge_guard.qualified
            assert recorded is not None
            hass.states.async_set(cfg.ev_power_entity_id, "3.1")
            controller._source_changed(None)
            assert controller._charge_guard.qualified == recorded
            assert trigger.call_count == 2


@pytest.mark.asyncio
async def test_retained_cycle_recharge_cannot_bypass_guard(hass):
    now = dt("2026-09-22T21:20:00+01:00")
    controller = Controller(hass, config())
    writer = adapter(controller)
    bad = replace(
        plan(), action=StrategyAction.CYCLE_RECHARGE,
        intent=intent(start=now, end=now + timedelta(hours=1)),
        current_cheap_window=SimpleNamespace(start=now, end=now + timedelta(hours=2), components=()),
    )
    controller._confirmed_plan = bad
    original = observation()
    controller._planning_state = original
    with patch.object(Controller, "_now", return_value=now), patch(
        "custom_components.house_battery_control.controller.read_state", return_value=replace(original, telemetry=None)
    ), patch("custom_components.house_battery_control.controller.build_plan", AsyncMock(return_value=bad)):
        await controller._reconcile()
    assert controller._last_plan.intent is None
    assert writer.next_start_change.call_args.args[1] is None
