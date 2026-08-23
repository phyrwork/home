"""Focused tests for the live Solis slot safety boundary."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.contracts import SlotDirection, SlotIntent, SlotOwner
from custom_components.house_battery_control.ha_writer import HomeAssistantWriter
from custom_components.house_battery_control.solis_actuator import SlotActuationStatus, SolisSlotActuator, encode_schedule
from custom_components.house_battery_control.solis_reader import read_solis_state


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class FakeHA:
    def __init__(self, states):
        self.states = states
        self.calls = []
        self.listeners = {}
        self.fail_entity = None

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def async_listen_state_change(self, entity_id, callback):
        self.listeners.setdefault(entity_id, []).append(callback)

        def remove():
            self.listeners.get(entity_id, []).remove(callback)

        return remove

    async def async_call(self, domain, service, data, *, blocking=True):
        self.calls.append((domain, service, data))
        entity_id = data["entity_id"]
        if entity_id == self.fail_entity:
            raise RuntimeError("injected service failure")
        state = self.states[entity_id]
        if domain == "switch":
            value = "on" if service == "turn_on" else "off"
        elif domain == "select":
            value = data["option"]
        else:
            value = str(data.get("value", data.get("datetime")))
        state.update(state=value, last_updated=state["last_updated"] + timedelta(seconds=1), context_id="service")
        for callback in tuple(self.listeners.get(entity_id, ())):
            callback(entity_id, None, state)


def _state(value, *, attributes=None):
    return {"state": value, "attributes": attributes or {}, "last_updated": NOW, "context_id": "initial"}


def _capability(value, unit, maximum="100"):
    return _state(value, attributes={"min": "0", "max": maximum, "step": "1", "unit_of_measurement": unit})


def fixture():
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    source["solis"]["telemetry"].update(
        battery_power_entity_id="sensor.battery_power",
        battery_power_sign="positive_means_charging",
        battery_voltage_entity_id="sensor.battery_voltage",
        device_timestamp_entity_id="sensor.device_time",
    )
    solis = config.from_mapping(source).solis
    states = {
        solis.telemetry.state_of_charge_entity_id: _state("55", attributes={"unit_of_measurement": "%"}),
        solis.telemetry.battery_power_entity_id: _state("0", attributes={"unit_of_measurement": "kW"}),
        solis.telemetry.battery_voltage_entity_id: _state("52", attributes={"unit_of_measurement": "V"}),
        solis.telemetry.device_timestamp_entity_id: _state(NOW.isoformat()),
        solis.persistent.storage_mode_entity_id: _state("Feed-In Priority", attributes={"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]}),
        solis.persistent.inverter_time_entity_id: _state(NOW.isoformat()),
    }
    for entity_id in (
        solis.persistent.allow_grid_charging_entity_id,
    ):
        states[entity_id] = _state("on")
    states[solis.protection.battery_reserve_entity_id] = _state("off")
    for entity_id in (
        solis.protection.battery_reserve_soc_entity_id,
    ):
        states[entity_id] = _capability("10", "%")
    for entity_id in (solis.capability.battery_max_charge_current_entity_id, solis.capability.battery_max_discharge_current_entity_id):
        states[entity_id] = _capability("100", "A", "200")
    for slot in solis.slots:
        for direction in (slot.charge, slot.discharge):
            states[direction.enable_entity_id] = _state("off")
            states[direction.time_entity_id] = _state("00:00-00:00")
            states[direction.current_entity_id] = _capability("1", "A", "10")
            states[direction.target_soc_entity_id] = _capability("50", "%")
    guard = "input_boolean.house_battery_control_disable"
    states[guard] = _state("off")
    observation = read_solis_state(solis, states, NOW)
    assert observation.snapshot is not None
    return solis, states, guard, observation


def intent(**changes):
    values = {
        "owner": SlotOwner.CHEAP_CHARGING,
        "physical_slot": 1,
        "direction": SlotDirection.CHARGE,
        "start": NOW - timedelta(minutes=5),
        "end": NOW + timedelta(minutes=55),
        "current": Decimal("1"),
        "target_soc": Decimal("50"),
        "expiry": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return SlotIntent(**values)


def reserve_intent(**changes):
    values = {
        "owner": SlotOwner.RESERVE_EXPORT,
        "physical_slot": 2,
        "direction": SlotDirection.DISCHARGE,
        "start": NOW - timedelta(minutes=5),
        "end": NOW + timedelta(minutes=55),
        "current": Decimal("1"),
        "target_soc": Decimal("50"),
        "expiry": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return SlotIntent(**values)


def actuator():
    solis, states, guard, observation = fixture()
    ha = FakeHA(states)
    return SolisSlotActuator(solis, HomeAssistantWriter(ha), control_disable_guard_entity_id=guard, inverter_timezone=timezone.utc), ha, observation


def enable_ids(controller):
    return tuple(direction.enable_entity_id for slot in controller.config.slots for direction in (slot.charge, slot.discharge))


@pytest.mark.asyncio
async def test_disable_all_turns_off_every_enabled_direction():
    controller, ha, _observation = actuator()
    for entity_id in enable_ids(controller):
        ha.states[entity_id]["state"] = "on"
    result = await controller.async_disable_all()
    assert result.safe
    assert len([call for call in ha.calls if call[1] == "turn_off"]) == 12
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))


@pytest.mark.asyncio
async def test_success_disables_before_configuring_and_enables_one_slot():
    controller, ha, observation = actuator()
    other = controller.config.slots[4].discharge.enable_entity_id
    ha.states[other]["state"] = "on"
    result = await controller.async_apply_intent(intent(), observation, now=NOW)
    assert result.status is SlotActuationStatus.APPLIED
    target = controller.config.slots[0].charge.enable_entity_id
    assert ha.states[target]["state"] == "on"
    assert all(ha.states[entity_id]["state"] == ("on" if entity_id == target else "off") for entity_id in enable_ids(controller))
    off_index = next(i for i, call in enumerate(ha.calls) if call[1] == "turn_off" and call[2]["entity_id"] == other)
    config_index = next(i for i, call in enumerate(ha.calls) if call[0] in {"text", "number"})
    assert off_index < config_index


@pytest.mark.asyncio
async def test_old_inverter_clock_sample_is_extrapolated_before_actuation():
    controller, ha, _observation = actuator()
    sampled_at = NOW - timedelta(minutes=15)
    ha.states[controller.config.persistent.inverter_time_entity_id].update(
        state=(sampled_at + timedelta(seconds=30)).isoformat(), last_updated=sampled_at
    )
    observation = read_solis_state(controller.config, ha.states, NOW)

    result = await controller.async_apply_intent(intent(), observation, now=NOW)

    assert result.status is SlotActuationStatus.APPLIED


@pytest.mark.asyncio
async def test_old_inverter_clock_sample_with_large_offset_is_rejected():
    controller, ha, _observation = actuator()
    sampled_at = NOW - timedelta(minutes=15)
    ha.states[controller.config.persistent.inverter_time_entity_id].update(
        state=(sampled_at + timedelta(minutes=2)).isoformat(), last_updated=sampled_at
    )
    observation = read_solis_state(controller.config, ha.states, NOW)

    result = await controller.async_apply_intent(intent(), observation, now=NOW)

    assert result.status is SlotActuationStatus.FAILED_SAFE
    assert "clock exceeds allowed skew" in result.message


@pytest.mark.asyncio
async def test_repeated_identical_heartbeat_is_a_verified_noop():
    controller, ha, observation = actuator()
    first = await controller.async_apply_intent(intent(), observation, now=NOW)
    assert first.status is SlotActuationStatus.APPLIED
    ha.calls.clear()

    current_observation = read_solis_state(
        controller.config, ha.states, NOW + timedelta(seconds=30)
    )
    second = await controller.async_apply_intent(
        intent(), current_observation, now=NOW + timedelta(seconds=30)
    )

    assert second.status is SlotActuationStatus.APPLIED
    assert second.mandatory_disable_deadline == intent().end
    assert second.results == ()
    assert ha.calls == []


@pytest.mark.asyncio
async def test_repeated_reserve_discharge_does_not_replay_slot_enable():
    controller, ha, observation = actuator()
    first = await controller.async_apply_intent(reserve_intent(), observation, now=NOW)
    assert first.status is SlotActuationStatus.APPLIED
    ha.calls.clear()

    current_observation = read_solis_state(
        controller.config, ha.states, NOW + timedelta(seconds=30)
    )
    second = await controller.async_apply_intent(
        reserve_intent(), current_observation, now=NOW + timedelta(seconds=30)
    )

    assert second.status is SlotActuationStatus.APPLIED
    assert second.results == ()
    assert ha.calls == []


@pytest.mark.asyncio
async def test_active_reserve_schedule_with_earlier_start_is_a_noop():
    controller, ha, observation = actuator()
    first = await controller.async_apply_intent(reserve_intent(), observation, now=NOW)
    assert first.status is SlotActuationStatus.APPLIED
    ha.calls.clear()
    ha.states[controller.config.persistent.inverter_time_entity_id].update(
        state=(NOW + timedelta(minutes=5)).isoformat(),
        last_updated=NOW + timedelta(minutes=5),
    )
    current_observation = read_solis_state(
        controller.config, ha.states, NOW + timedelta(minutes=5)
    )

    second = await controller.async_apply_intent(
        reserve_intent(
            start=NOW + timedelta(minutes=5),
            end=NOW + timedelta(minutes=55),
        ),
        current_observation,
        now=NOW + timedelta(minutes=5),
    )

    assert second.status is SlotActuationStatus.APPLIED
    assert second.results == ()
    assert ha.calls == []


@pytest.mark.asyncio
async def test_changed_intent_still_replaces_existing_slot_transactionally():
    controller, ha, observation = actuator()
    await controller.async_apply_intent(intent(), observation, now=NOW)
    ha.calls.clear()
    current_observation = read_solis_state(
        controller.config, ha.states, NOW + timedelta(seconds=30)
    )

    result = await controller.async_apply_intent(
        intent(current=Decimal("2")), current_observation, now=NOW + timedelta(seconds=30)
    )

    assert result.status is SlotActuationStatus.APPLIED
    assert not any(call[1] in {"turn_on", "turn_off"} for call in ha.calls)
    assert [call[0:2] for call in ha.calls] == [("number", "set_value")]
    target = controller.config.slots[0].charge
    assert Decimal(ha.states[target.current_entity_id]["state"]) == Decimal("2")
    assert ha.states[target.enable_entity_id]["state"] == "on"


@pytest.mark.asyncio
async def test_failure_cleans_up_all_slot_switches():
    controller, ha, observation = actuator()
    target = controller.config.slots[0].charge
    ha.fail_entity = target.current_entity_id
    ha.states[controller.config.slots[3].charge.enable_entity_id]["state"] = "on"
    result = await controller.async_apply_intent(intent(current=Decimal("2")), observation, now=NOW)
    assert result.status is SlotActuationStatus.FAILED_SAFE
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))


def test_schedule_encoding_supports_cross_midnight_intervals():
    assert encode_schedule(NOW.replace(hour=23), NOW + timedelta(hours=13), timezone.utc) == "23:00-01:00"
