"""Real incident plus explicit forward projection through the T0051 guard.

Exercises the production guard, planner and native slot reconciliation with
observed EV power followed by explicitly synthetic future idle reports.
"""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control.model import SlotDirection
from custom_components.house_battery_control.charge_authorization import ChargeGuard
from custom_components.house_battery_control.tests import test_planner as fixture
from custom_components.house_battery_control.tests.behavioral_replay import _reconcile, _update_telemetry
from custom_components.house_battery_control.tests.test_solis import fixture as solis_fixture
from custom_components.house_battery_control.solis import SolisAdapter
from custom_components.house_battery_control.model import CycleState
from zoneinfo import ZoneInfo


SCENARIO = yaml.safe_load(
    (Path(__file__).parent / "scenarios/projections/iog_ev_finished_2026_09_22.yaml").read_text()
)
DEVICE = "octopus_energy_00000000_0009_4000_8020_00000007d6b4"


CASES = [
    pytest.param(
        step,
        id=step["at"][11:19],
    )
    for step in SCENARIO["steps"]
]


@pytest.mark.parametrize("step", CASES)
@pytest.mark.parametrize("soc", ["49", "100"], ids=["ordinary_charge", "cycle_recharge"])
@pytest.mark.asyncio
async def test_idle_ev_projection(hass, monkeypatch, freezer, step, soc):
    at = datetime.fromisoformat(step["at"]).astimezone(timezone.utc)
    freezer.move_to(at)
    monkeypatch.setattr(fixture, "NOW", at)
    projection = SCENARIO["projection"]
    for entity_id, state in (
        ("sensor.ev_charger_energy_meter_power", str(projection["ev_power_w"])),
        (f"sensor.{DEVICE}_intelligent_state", projection["intelligent_state"]),
        (f"sensor.{DEVICE}_intelligent_dispatches_data_last_retrieved", at.isoformat()),
        (f"binary_sensor.{DEVICE}_intelligent_dispatching", projection["dispatching"]),
    ):
        hass.states.async_set(entity_id, state)

    # Representative profitable price intervals; future EV reports are
    # deliberately fresh and idle, so freshness cannot explain the refusal.
    plan = await fixture._build(
        hass, cheap=True, bonus=not step["charge_allowed"],
        soc=soc, dispatch_state=projection["dispatching"], now=at,
        authorization=ChargeGuard(at - timedelta(seconds=1)).observe(hass, fixture._config().charge_guard, at),
    )
    assert plan.issue is None, plan.issue
    charges = tuple(
        segment for segment in (() if plan.intent is None else plan.intent.segments)
        if segment.direction is SlotDirection.CHARGE
    )
    if step["charge_allowed"]:
        assert charges, "Static off-peak must remain usable with an idle EV"
    elif charges:
        raise AssertionError(
            f"{step['at']}: idle EV still permits {plan.action}: "
            + ", ".join(f"{s.start.isoformat()}–{s.end.isoformat()}" for s in charges)
        )


@pytest.mark.parametrize("soc", ["49", "100"], ids=["ordinary_charge", "cycle_recharge"])
@pytest.mark.asyncio
async def test_sequential_stop_and_idle_projection(hass, monkeypatch, freezer, soc):
    # The earlier source retrieval is synthetic because its sensor was enabled
    # after the observed EV stop. These acceptance inputs establish a prior
    # valid latch and test its survival, expiry and armed-slot cleanup.
    times = [
        ("2026-09-22T21:09:14+01:00", "7464", True),
        ("2026-09-22T21:11:19+01:00", "3.1", True),
        *[(s["at"], "3.1", s["charge_allowed"]) for s in SCENARIO["steps"]],
    ]
    guard = ChargeGuard(datetime.fromisoformat(times[0][0]) - timedelta(seconds=1))
    parsed, states = solis_fixture()
    writer = SolisAdapter(states, parsed, timezone=ZoneInfo("Europe/London"))
    previous = None
    for timestamp, power, allowed in times:
        at = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
        freezer.move_to(at)
        monkeypatch.setattr(fixture, "NOW", at)
        for entity, value in (
            ("sensor.ev_charger_energy_meter_power", power),
            (f"sensor.{DEVICE}_intelligent_state", "SMART_CONTROL_IN_PROGRESS"),
            (f"sensor.{DEVICE}_intelligent_dispatches_data_last_retrieved", at.isoformat()),
        ):
            hass.states.async_set(entity, value)
        auth = guard.observe(hass, fixture._config().charge_guard, at)
        result = await fixture._build(
            hass, cheap=True, bonus=timestamp[11:16] != "23:30", soc=soc, now=at,
            authorization=auth,
            state=CycleState.IDLE if previous is None else previous.next_cycle_state,
            deadline=None if previous is None else previous.cycle_deadline,
            charge_lease_deadline=None if previous is None else previous.charge_lease_deadline,
        )
        assert result.issue is None
        charges = [s for s in (() if result.intent is None else result.intent.segments)
                   if s.direction is SlotDirection.CHARGE]
        assert bool(charges) == allowed, timestamp
        for segment in charges:
            assert auth.charge_is_authorized(max(at, segment.start), segment.end)
        _update_telemetry(states, parsed, {"soc_percent": soc}, at)
        operations = _reconcile(writer, states, result, at)
        if timestamp[11:16] == "21:30":
            assert any(op.target is False and op.key.direction is SlotDirection.CHARGE for op in operations)
        if not allowed:
            assert not any(states[slot.charge.enable_entity_id]["state"] == "on" for slot in parsed.slots)
        # _reconcile has synchronously confirmed off in the fake device. Match
        # Controller._retire_proven_stops clearing the expired bonus lease.
        previous = replace(result, charge_lease_deadline=None) if not charges else result
