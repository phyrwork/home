"""Replay the idle-with-surplus incident and exercise changes in charge priority."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest
import yaml

from custom_components.house_battery_control.charge_authorization import ChargeAuthorization, ChargeGuard
from custom_components.house_battery_control.controller import Controller, _plan_reason
from custom_components.house_battery_control.model import CycleState, SlotDirection, StrategyAction
from custom_components.house_battery_control.solis import SolisAdapter, read_state
from custom_components.house_battery_control.tests import test_planner as fixture
from custom_components.house_battery_control.tests.behavioral_replay import _reconcile, _update_telemetry
from custom_components.house_battery_control.tests.test_solis import fixture as solis_fixture

INCIDENT = yaml.safe_load((Path(__file__).parent / 'scenarios/incidents/idle_surplus_2026_09_23.yaml').read_text())


@pytest.mark.asyncio
async def test_recorded_idle_surplus(hass, monkeypatch, freezer):
    data = INCIDENT
    at = datetime.fromisoformat(data['at'])
    monkeypatch.setattr(fixture, 'NOW', at)
    config = fixture._config()
    recorded = json.loads((Path(__file__).parent / 'scenarios/incidents/idle_surplus_2026_09_23_states.json').read_text())['states']
    freezer.move_to(datetime.fromisoformat(data['ev_power_reported_at']))
    for entity, value in (
        (config.charge_guard.ev_power_entity_id, recorded[config.charge_guard.ev_power_entity_id]['state']),
        (config.charge_guard.intelligent_state_entity_id, recorded[config.charge_guard.intelligent_state_entity_id]['state']),
        (config.charge_guard.dispatches_retrieved_entity_id, recorded[config.charge_guard.dispatches_retrieved_entity_id]['state']),
    ):
        hass.states.async_set(entity, str(value), recorded[entity]['attributes'])
    freezer.move_to(at)
    guard = ChargeGuard(at - timedelta(minutes=2))
    authorization = guard.observe(hass, config.charge_guard, at)
    assert guard.reason == data['charge_authorization_reason']
    assert authorization.authorized_until(at) is None

    parsed, states = solis_fixture()
    # Recorder omits capability metadata; retain fixture min/max/step/options.
    for entity, baseline in states.items():
        actual = recorded[entity]
        baseline.update(state=actual['state'], last_updated=datetime.fromisoformat(actual['last_updated']))
        baseline['attributes'].update(actual['attributes'])
    observed = read_state(states, parsed, now=at)
    assert observed.telemetry.state_of_charge_percent == Decimal(str(data['soc_percent']))
    assert observed.telemetry.device_timestamp == datetime.fromisoformat(data['telemetry_timestamp'])
    # The current prices were recorded; future intervals/provenance are fixture
    # inputs because recorder excludes the fused tariff arrays.
    imports = fixture._import_rates(
        (recorded[config.tariff.import_rates_entity_id]['state'], fixture.CheapClassification.BONUS_CHEAP, True),
        ('0.30', fixture.CheapClassification.NOT_CHEAP, False),
    )
    exports = fixture._export((recorded[config.tariff.export_rates_entity_id]['state'],) * 2)
    with patch.object(fixture, '_solis', return_value=observed):
        plan = await fixture._build(
            hass, cheap=True, bonus=True, soc=str(data['soc_percent']), now=at,
            window_minutes=(datetime.fromisoformat(data['cheap_window_end']) - at).total_seconds() / 60,
            device_timestamp=datetime.fromisoformat(data['telemetry_timestamp']),
            reserve_energy=Decimal(str(data['reserve_energy_kwh'])), authorization=authorization,
            import_rates_override=imports, export_rates_override=exports,
        )
    assert plan.issue is None
    assert plan.current_cheap_window.end == datetime.fromisoformat(data['cheap_window_end'])
    assert plan.action.value == data['expected_action']
    assert plan.battery_energy_kwh == Decimal(str(data['battery_energy_kwh']))
    assert plan.control_reserve_soc_percent == Decimal(str(data['control_reserve_soc_percent']))
    assert plan.control_reserve_balance_kwh == Decimal(str(data['control_reserve_balance_kwh']))
    assert 'charging not authorized: not_smart_control' in _plan_reason(plan, guard.reason)
    writer = SolisAdapter(states, parsed, timezone=ZoneInfo('Europe/London'))
    operations = _reconcile(writer, states, plan, at)
    assert any(op.target is True and op.key.direction is SlotDirection.DISCHARGE for op in operations)
    assert not any(states[slot.charge.enable_entity_id]['state'] == 'on' for slot in parsed.slots)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', list(CycleState))
@pytest.mark.parametrize('soc,action', [('54', StrategyAction.RESERVE_DISCHARGE), ('100', StrategyAction.RESERVE_DISCHARGE), ('17', StrategyAction.IDLE)])
async def test_unqualified_cheap_period_uses_surplus_from_every_state(hass, state, soc, action):
    result = await fixture._build(
        hass, cheap=True, bonus=True, soc=soc, state=state,
        deadline=fixture.NOW + timedelta(minutes=10),
        authorization=ChargeAuthorization(fixture.NOW),
    )
    assert result.issue is None
    assert result.action is action
    assert all(s.direction is SlotDirection.DISCHARGE for s in (() if result.intent is None else result.intent.segments))


@pytest.mark.asyncio
async def test_permission_preempts_export_and_expiry_returns_to_export(hass):
    export = await fixture._build(hass, cheap=True, bonus=True, authorization=ChargeAuthorization(fixture.NOW))
    charge = await fixture._build(hass, cheap=True, bonus=True, state=export.next_cycle_state)
    expired = await fixture._build(hass, cheap=True, bonus=True, state=charge.next_cycle_state, authorization=ChargeAuthorization(fixture.NOW))
    assert [p.action for p in (export, charge, expired)] == [
        StrategyAction.RESERVE_DISCHARGE, StrategyAction.CHEAP_CHARGE, StrategyAction.RESERVE_DISCHARGE,
    ]
    parsed, states = solis_fixture()
    writer = SolisAdapter(states, parsed, timezone=ZoneInfo('Europe/London'))
    _update_telemetry(states, parsed, {'soc_percent': 55}, fixture.NOW)
    _reconcile(writer, states, export, fixture.NOW)
    for result, stopped, started in ((charge, SlotDirection.DISCHARGE, SlotDirection.CHARGE), (expired, SlotDirection.CHARGE, SlotDirection.DISCHARGE)):
        ops = _reconcile(writer, states, result, fixture.NOW)
        stop = next(i for i, op in enumerate(ops) if op.target is False and op.key.direction is stopped)
        start = next(i for i, op in enumerate(ops) if op.target is True and op.key.direction is started)
        assert stop < start


@pytest.mark.asyncio
async def test_stale_telemetry_retains_export_in_cheap_period_but_charge_preempts(hass):
    export = await fixture._build(hass, cheap=True, bonus=True, authorization=ChargeAuthorization(fixture.NOW))
    controller = Controller(hass, fixture._config())
    controller._confirmed_plan = export
    retained = controller._retain_armed_schedule(replace(export, intent=None), fixture.NOW)
    assert retained.intent == export.intent
    assert retained.action is StrategyAction.RESERVE_DISCHARGE
    charge = await fixture._build(hass, cheap=True, bonus=True)
    retained = controller._retain_armed_schedule(charge, fixture.NOW)
    assert retained.action is StrategyAction.CHEAP_CHARGE
    assert retained.intent == charge.intent
