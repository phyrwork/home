"""Recorded reserve stall: stop and refuse rearming at the two-percent boundary.

This is a focused replay of production stop discovery, not a hardware simulator.
Device sample timestamps are recorded, never refreshed synthetically. Capabilities
use fixture defaults where recorder omits metadata. The replay verifies controller decisions, not physical load following after a stop.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.house_battery_control.controller import Controller
from custom_components.house_battery_control.model import CycleState, SlotDirection, StrategyAction
from custom_components.house_battery_control.planner import _reserve_export_allowed
from custom_components.house_battery_control.solis import SlotKey, read_state, telemetry_is_fresh
from custom_components.house_battery_control.tests import test_planner as planning
from custom_components.house_battery_control.tests.test_solis import fixture

DATA = json.loads((Path(__file__).parent / 'scenarios/incidents/reserve_discharge_stalled_2026_09_25_states.json').read_text())['states']
KEY = SlotKey(2, SlotDirection.DISCHARGE)


def replay(at):
    now = datetime.fromisoformat('2026-09-25T' + at + '+01:00')
    config, states = fixture()
    latest = {}
    for row in DATA:
        if row['last_updated_ts'] <= now.timestamp():
            latest[row['entity_id']] = row
    for entity, baseline in states.items():
        row = latest[entity]  # Missing raw state must fail, not silently synthesize.
        baseline.update(state=row['state'], last_updated=datetime.fromtimestamp(row['last_updated_ts'], timezone.utc))
        baseline['attributes'].update(json.loads(row['shared_attrs'] or '{}'))
    observed = read_state(states, config, now=now)
    assert telemetry_is_fresh(observed, config)
    return now, observed, latest


def test_recorded_reserve_boundary_requests_stop(hass):
    # Immediately before HA records the off command, SOC19 reaches old target18+1.
    _, observed, _ = replay('22:36:16.910000')
    controller = Controller(hass, planning._config())
    assert observed.telemetry.state_of_charge_percent == 19
    assert observed.direction(KEY).target_soc.current_value == 18
    assert observed.direction(KEY).enabled is True
    controller._discover_unconditional_stops(observed, observed.observed_at, 0)
    assert KEY in controller._stop_debts


def test_recorded_stall_is_not_eligible_for_rearming():
    _, observed, latest = replay('22:53:01')
    assert observed.direction(KEY).enabled is True
    assert observed.direction(KEY).target_soc.current_value == 17
    assert observed.telemetry.state_of_charge_percent == 19
    assert not _reserve_export_allowed(Decimal(19), Decimal(17))
    assert Decimal(latest['sensor.house_battery_reserve_usable']['state']) > 2


def test_sustained_recorded_stall_releases_export_slot(hass):
    """Every fresh stalled observation requests a stop without waiting for power history."""
    controller = Controller(hass, planning._config())
    samples = []
    for at in ('22:41:20', '22:46:30', '22:51:20'):
        now, observed, latest = replay(at)
        assert observed.direction(KEY).enabled is True
        assert abs(observed.telemetry.battery_power_kw) < Decimal('0.05')
        grid = Decimal(latest['sensor.garage_inverter_telemetry_garage_inverter_power_grid_total_power']['state'])
        assert grid < -250
        samples.append(observed.telemetry.device_timestamp)
        controller._stop_debts.clear()
        controller._discover_unconditional_stops(observed, now, (now - datetime.fromisoformat('2026-09-25T22:36:26+01:00')).total_seconds())
        assert KEY in controller._stop_debts
    assert len(set(samples)) == 3
    assert KEY in controller._stop_debts, 'Sustained near-zero discharge leaves the reserve export slot armed while importing'


@pytest.mark.asyncio
@pytest.mark.parametrize('cycle_state', [CycleState.RESERVE_DISCHARGING, CycleState.STOPPING, CycleState.IDLE])
async def test_recorded_reserve_plan_does_not_rearm_after_stop(hass, monkeypatch, cycle_state):
    now, observed, latest = replay('22:53:01')
    monkeypatch.setattr(planning, 'NOW', now)
    # Forecast/tariff arrays are not recorded: inject the recorded reserve result
    # and fixture peak tariff. The SOC and inverter controls remain recorded.
    with patch.object(planning, '_solis', return_value=observed):
        plan = await planning._build(
            hass, cheap=False, now=now, state=cycle_state,
            reserve_energy=Decimal(latest['sensor.house_battery_reserve_target']['state']),
        )
    assert plan.issue is None
    assert plan.control_reserve_soc_percent == 17
    assert plan.action is StrategyAction.IDLE
    assert plan.intent is None
