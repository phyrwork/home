"""Slot-only control survives stale reads without authorizing new discharge."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.house_battery_control.controller import Controller
from custom_components.house_battery_control.model import ControllerHealth, CycleState, SlotDirection, StrategyAction
from custom_components.house_battery_control.solis import (
    SolisAdapter, WriteOutcome, planning_observation, read_state, telemetry_is_fresh,
)
from custom_components.house_battery_control.tests.test_controller import NOW, adapter, config, observation, plan
from custom_components.house_battery_control.tests.test_solis import (
    LONDON, FakeHA, advance, cycle_pair, first_slot_change, fixture, intent,
)


def stale(value):
    return replace(value, telemetry=None, health=ControllerHealth.DEGRADED)


def test_cached_values_keep_original_timestamp_and_never_become_fresh():
    parsed, states = fixture()
    original = read_state(states, parsed, now=NOW)
    for entity in (parsed.telemetry.state_of_charge_entity_id, parsed.slots[0].charge.current_entity_id):
        states[entity]['state'] = 'unavailable'
    failed = read_state(states, parsed, now=NOW + timedelta(minutes=1))
    cached = planning_observation(failed, original, parsed)
    assert cached.telemetry == original.telemetry
    assert cached.slots[0].charge.current == original.slots[0].charge.current
    assert cached.telemetry.device_timestamp == original.telemetry.device_timestamp
    assert not telemetry_is_fresh(cached, parsed)
    assert not telemetry_is_fresh(replace(original, observed_at=NOW + timedelta(minutes=31)), parsed)


@pytest.mark.parametrize('field,value', [('storage_mode_entity_id','Self-Use'), ('allow_grid_charging_entity_id','off')])
def test_commissioned_policy_is_checked_not_rewritten(field, value):
    parsed, states = fixture()
    states[getattr(parsed.persistent, field)]['state'] = value
    writer = SolisAdapter(states, parsed, timezone=LONDON)
    observed = read_state(states, parsed, now=NOW)
    assert writer.next_start_change(observed, intent(), battery_reserve_soc_percent=Decimal(10)) is None
    assert not writer.intent_matches(observed, intent(), battery_reserve_soc_percent=Decimal(10))


def test_slot_writer_needs_no_telemetry_for_tariff_charge_and_never_writes_policy():
    parsed, states = fixture()
    states[parsed.telemetry.state_of_charge_entity_id]['state'] = 'unavailable'
    writer = SolisAdapter(states, parsed, timezone=LONDON)
    changes = advance(writer, states, intent())
    assert changes
    assert all('_slot' in c.entity_id for c in changes)
    assert changes[-1].target is True
    assert advance(writer, states, intent()) == []


async def test_read_failure_replans_tariffs_using_cache_and_keeps_values(hass):
    controller = Controller(hass, config())
    writer = adapter(controller)
    original = observation()
    controller._planning_state = original
    controller._last_plan = plan()
    with (
        patch.object(Controller, '_now', return_value=NOW),
        patch('custom_components.house_battery_control.controller.read_state', return_value=stale(original)),
        patch('custom_components.house_battery_control.controller.build_plan', AsyncMock(return_value=plan())) as build,
    ):
        await controller._reconcile()
    assert build.await_args.args[2].telemetry == original.telemetry
    assert controller.data.state_of_charge_percent == Decimal(55)
    assert controller.data.reserve_soc_percent == Decimal(20)
    assert controller.data.telemetry_stale
    assert controller.data.health is ControllerHealth.DEGRADED
    writer.set_mode.assert_not_awaited()
    writer.set_peak_shaving.assert_not_awaited()


def test_no_new_discharge_from_cached_full_soc(hass):
    controller = Controller(hass, config())
    desired = replace(plan(), action=StrategyAction.CYCLE_DISCHARGE, intent=cycle_pair(NOW),
                      next_cycle_state=CycleState.CYCLE_DISCHARGING,
                      current_cheap_window=SimpleNamespace(end=NOW + timedelta(hours=1)))
    limited = controller._retain_armed_schedule(desired, NOW)
    assert limited.intent is not None
    assert all(s.direction is SlotDirection.CHARGE for s in limited.intent.segments)
    assert limited.action is StrategyAction.CHEAP_CHARGE


def test_stale_cycle_handover_retains_armed_recharge_but_does_not_roll_new_discharge(hass):
    controller = Controller(hass, config())
    first = replace(plan(), action=StrategyAction.CYCLE_DISCHARGE, intent=cycle_pair(NOW),
                    next_cycle_state=CycleState.CYCLE_DISCHARGING,
                    current_cheap_window=SimpleNamespace(end=NOW + timedelta(hours=1)))
    controller._confirmed_plan = first
    boundary = NOW + timedelta(minutes=10)
    desired = replace(first, intent=cycle_pair(boundary, recharge_first=True), action=StrategyAction.CYCLE_RECHARGE)
    limited = controller._retain_armed_schedule(desired, boundary)
    assert limited.intent.segments == (first.intent.segments[1],)
    assert limited.action is StrategyAction.CYCLE_RECHARGE
    assert limited.cycle_deadline == NOW + timedelta(minutes=20)
    # Withdrawing cheap authority must still clear the pair despite stale SOC.
    revoked = controller._retain_armed_schedule(replace(plan(), current_cheap_window=None), boundary)
    assert revoked.intent is None


async def test_known_expiry_creates_stop_debt_even_when_enable_is_unavailable(hass):
    from custom_components.house_battery_control.solis import SlotKey
    controller = Controller(hass, config())
    key = SlotKey(2, SlotDirection.DISCHARGE)
    controller._owned_expiry[key] = NOW
    unknown = observation(enabled=key, enabled_state='unavailable')
    controller._discover_unconditional_stops(stale(unknown), NOW, 0)
    assert key in controller._stop_debts


async def test_optimistic_error_requires_later_read_before_dependent_write():
    parsed, states = fixture()
    fake = FakeHA(states, 'error')
    writer = SolisAdapter(fake, parsed, timezone=LONDON)
    change = first_slot_change(writer, states)
    result = await writer.apply(change, deadline=asyncio.get_running_loop().time() + 1)
    assert result.outcome is WriteOutcome.SERVICE_ERROR
    observed = read_state(states, parsed, now=NOW)
    assert writer.next_start_change(observed, intent(), battery_reserve_soc_percent=Decimal(10)) is None
    assert any(i.code == 'write_unconfirmed' for i in writer.control_issues(observed))
    # A real later report can have an unchanged value and last_updated.
    states[change.entity_id]['last_reported'] = NOW + timedelta(seconds=1)
    assert not writer.control_issues(read_state(states, parsed, now=NOW))
    fake.behavior = 'success'
    assert first_slot_change(writer, states).entity_id != change.entity_id


async def test_shutdown_is_bounded_with_unavailable_controls(hass):
    from custom_components.house_battery_control.solis import SlotKey
    controller = Controller(hass, config())
    writer = adapter(controller)
    with (
        patch('custom_components.house_battery_control.controller.SHUTDOWN_TIMEOUT', timedelta(milliseconds=20)),
        patch('custom_components.house_battery_control.controller.read_state', return_value=observation(enabled=SlotKey(1, SlotDirection.CHARGE), enabled_state='unavailable')),
    ):
        await asyncio.wait_for(controller.async_stop(), .5)
    assert 'cleanup incomplete' in controller.data.reason
    writer.set_mode.assert_not_awaited()
    writer.stop.assert_not_awaited()


async def test_shutdown_does_not_wait_forever_for_cancellation_ignoring_worker(hass):
    controller = Controller(hass, config())
    adapter(controller)
    ready, release = asyncio.Event(), asyncio.Event()
    async def ignores_cancel():
        ready.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
    worker = asyncio.create_task(ignores_cancel())
    controller._worker_task = worker
    await ready.wait()
    try:
        with patch('custom_components.house_battery_control.controller.SHUTDOWN_TIMEOUT', timedelta(milliseconds=20)):
            await asyncio.wait_for(controller.async_stop(), .5)
        assert not worker.done()
        assert 'cleanup incomplete' in controller.data.reason
    finally:
        release.set()
        await worker


async def test_ambiguous_enable_keeps_expiry_obligation(hass):
    from custom_components.house_battery_control.solis import SlotKey, WriteResult
    controller = Controller(hass, config())
    writer = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    planned = replace(plan(), action=StrategyAction.CHEAP_CHARGE,
                      intent=intent(start=NOW, end=NOW + timedelta(minutes=10)))
    change = SimpleNamespace(entity_id=controller.config.solis.direction(key).enable_entity_id, target=True)
    writer.apply.return_value = WriteResult(change.entity_id, WriteOutcome.SERVICE_TIMEOUT, 'timeout')
    with patch.object(Controller, '_now', return_value=NOW):
        await controller._attempt_start(change, 'generation', planned, observation(), NOW, 0)
    assert controller._owned_expiry[key] == NOW + timedelta(minutes=10)
    controller._discover_unconditional_stops(stale(observation()), NOW + timedelta(minutes=10), 600)
    assert key in controller._stop_debts


async def test_low_soc_can_charge_but_cannot_start_discharge(hass):
    from custom_components.house_battery_control.tests.test_planner import _build
    cheap = await _build(hass, cheap=True, soc='2')
    expensive = await _build(hass, cheap=False, soc='2')
    assert cheap.issue is None
    assert cheap.action is StrategyAction.CHEAP_CHARGE
    assert all(s.direction is SlotDirection.CHARGE for s in cheap.intent.segments)
    assert expensive.issue is None
    assert expensive.intent is None
