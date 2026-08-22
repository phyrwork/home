"""Deterministic tests for the transactional Solis slot actuator."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.contracts import SlotDirection, SlotIntent, SlotOwner
from custom_components.house_battery_control.ha_writer import HomeAssistantWriter
from custom_components.house_battery_control.solis_actuator import (
    CommissioningRecord,
    SlotActuationStatus,
    SolisSlotActuator,
    canonical_mapping,
    encode_schedule,
    mapping_fingerprint,
)
from custom_components.house_battery_control.solis_reader import read_solis_state


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class FakeHA:
    def __init__(self, states):
        self.states = states
        self.calls = []
        self.listeners = {}
        self.on_call = None

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def async_listen_state_change(self, entity_id, callback):
        self.listeners.setdefault(entity_id, []).append(callback)

        def remove():
            self.listeners.get(entity_id, []).remove(callback)

        return remove

    async def async_call(self, domain, service, service_data, *, blocking=True):
        self.calls.append((domain, service, service_data))
        if self.on_call is not None:
            self.on_call(domain, service, service_data)
        entity_id = service_data["entity_id"]
        state = self.states[entity_id]
        if domain == "switch":
            value = "on" if service == "turn_on" else "off"
        elif domain == "text":
            value = service_data["value"]
        elif domain == "number":
            value = str(service_data["value"])
        else:
            value = str(service_data.get("option", service_data.get("datetime")))
        state["state"] = value
        state["last_updated"] = state["last_updated"] + timedelta(seconds=1)
        state["context_id"] = "service"
        for callback in tuple(self.listeners.get(entity_id, ())):
            callback(entity_id, None, state)


def _state(value, *, attributes=None):
    return {
        "state": value,
        "attributes": attributes or {},
        "last_updated": NOW,
        "context_id": "initial",
    }


def _capability(value, unit, maximum="100"):
    return _state(value, attributes={"min": "0", "max": maximum, "step": "1", "unit_of_measurement": unit})


def fixture():
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    source["solis"]["telemetry"].update(
        battery_power_entity_id="sensor.garage_battery_power",
        battery_power_sign="positive_means_charging",
        device_timestamp_entity_id="sensor.garage_device_time",
    )
    parsed = config.from_mapping(source)
    solis = parsed.solis
    states = {}
    states[solis.telemetry.state_of_charge_entity_id] = _state("55", attributes={"unit_of_measurement": "%"})
    states[solis.telemetry.battery_power_entity_id] = _state("0", attributes={"unit_of_measurement": "kW"})
    states[solis.telemetry.device_timestamp_entity_id] = _state(NOW.isoformat())
    persistent = solis.persistent
    states[persistent.storage_mode_entity_id] = _state("Feed-In Priority", attributes={"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]})
    for entity_id in (persistent.allow_grid_charging_entity_id, persistent.allow_export_entity_id, persistent.grid_peak_shaving_entity_id, persistent.inverter_on_off_entity_id):
        states[entity_id] = _state("on")
    states[persistent.inverter_time_entity_id] = _state(NOW.isoformat())
    for entity_id in (solis.protection.battery_over_discharge_soc_entity_id, solis.protection.battery_force_charge_soc_entity_id, solis.protection.battery_recovery_soc_entity_id, solis.protection.battery_max_charge_soc_entity_id, solis.protection.battery_reserve_soc_entity_id):
        states[entity_id] = _capability("10", "%")
    states[solis.protection.battery_reserve_entity_id] = _state("off")
    states[solis.capability.battery_max_charge_current_entity_id] = _capability("100", "A", "200")
    states[solis.capability.battery_max_discharge_current_entity_id] = _capability("100", "A", "200")
    states[solis.capability.max_output_power_entity_id] = _capability("5000", "W", "6000")
    states[solis.capability.max_export_power_entity_id] = _capability("5000", "W", "6000")
    for slot in solis.slots:
        for direction in (slot.charge, slot.discharge):
            states[direction.enable_entity_id] = _state("off")
            states[direction.time_entity_id] = _state("00:00-00:00")
            states[direction.current_entity_id] = _capability("1", "A", "10")
            states[direction.target_soc_entity_id] = _capability("50", "%")
    guard = "input_boolean.house_battery_control_disable"
    states[guard] = _state("off")
    result = read_solis_state(solis, states, NOW)
    assert result.snapshot is not None
    return solis, states, guard, result.snapshot


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


def actuator(commissioned=True):
    solis, states, guard, snapshot = fixture()
    ha = FakeHA(states)
    fingerprint = mapping_fingerprint(solis)
    record = CommissioningRecord(NOW - timedelta(days=1), fingerprint, True, True) if commissioned else None
    return SolisSlotActuator(solis, HomeAssistantWriter(ha), control_disable_guard_entity_id=guard, commissioning=record), ha, snapshot


def test_mapping_fingerprint_is_canonical_and_changes_with_entity_category():
    solis, *_ = fixture()
    first = canonical_mapping(solis)
    assert first == canonical_mapping(solis)
    assert mapping_fingerprint(solis) == mapping_fingerprint(solis)
    object.__setattr__(solis.telemetry, "battery_power_entity_id", "sensor.changed")
    assert canonical_mapping(solis) != first


def test_schedule_preserves_cross_midnight_and_rejects_dst_ambiguity():
    zone = ZoneInfo("Europe/London")
    assert encode_schedule(datetime(2026, 1, 1, 23, tzinfo=timezone.utc), datetime(2026, 1, 2, 1, tzinfo=timezone.utc), zone) == "23:00-01:00"
    with pytest.raises(ValueError, match="ambiguous"):
        encode_schedule(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), datetime(2026, 10, 25, 2, tzinfo=timezone.utc), zone)


@pytest.mark.asyncio
async def test_uncommissioned_path_only_cleans_and_never_configures_or_enables():
    controller, ha, snapshot = actuator(False)
    result = await controller.async_apply_intent(intent(), snapshot, now=NOW)
    assert result.status == SlotActuationStatus.BLOCKED_UNCOMMISSIONED_SAFE
    assert ha.calls == []


@pytest.mark.asyncio
async def test_success_disables_all_then_configures_and_enables_one_direction():
    controller, ha, snapshot = actuator()
    result = await controller.async_apply_intent(intent(), snapshot, now=NOW)
    assert result.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION
    assert result.mandatory_disable_deadline == NOW + timedelta(minutes=55)
    enable_calls = [call for call in ha.calls if call[0] == "switch" and call[1] == "turn_on"]
    assert len(enable_calls) == 1
    assert enable_calls[0][2]["entity_id"].endswith("_slot1_charge")
    assert ha.calls[0][0:2] == ("text", "set_value")
    assert ha.calls.index(enable_calls[0]) > 0


@pytest.mark.asyncio
async def test_guard_change_after_enable_always_cleans_all_directions():
    controller, ha, snapshot = actuator()

    def change_guard(domain, service, data):
        if domain == "switch" and service == "turn_on" and data["entity_id"].endswith("_slot1_charge"):
            ha.states[controller.control_disable_guard_entity_id]["state"] = "on"

    ha.on_call = change_guard
    result = await controller.async_apply_intent(intent(), snapshot, now=NOW)
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert all(state["state"] == "off" for entity_id, state in ha.states.items() if entity_id.endswith("_enable"))
