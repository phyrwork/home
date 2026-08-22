"""Deterministic tests for the transactional Solis slot actuator."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from custom_components.house_battery_control.ha_writer import HomeAssistantWriter
from custom_components.house_battery_control.solis_actuator import (
    CommissioningRecord,
    MAXIMUM_INVERTER_CLOCK_SKEW,
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
    return solis, states, guard, result


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
    solis, states, guard, observation = fixture()
    ha = FakeHA(states)
    fingerprint = mapping_fingerprint(solis)
    record = CommissioningRecord(NOW - timedelta(days=1), fingerprint, True, True) if commissioned else None
    return SolisSlotActuator(
        solis,
        HomeAssistantWriter(ha),
        control_disable_guard_entity_id=guard,
        inverter_timezone=timezone.utc,
        commissioning=record,
    ), ha, observation


def enable_ids(controller):
    return tuple(
        direction.enable_entity_id
        for slot in controller.config.slots
        for direction in (slot.charge, slot.discharge)
    )


def test_mapping_fingerprint_is_canonical_and_changes_with_entity_category():
    solis, *_ = fixture()
    first = canonical_mapping(solis)
    assert first == canonical_mapping(solis)
    assert mapping_fingerprint(solis) == mapping_fingerprint(solis)
    object.__setattr__(solis.telemetry, "battery_power_entity_id", "sensor.changed")
    assert canonical_mapping(solis) != first


def test_canonical_mapping_retains_explicit_null_telemetry_fields():
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    solis = config.from_mapping(source).solis
    canonical = canonical_mapping(solis)
    assert b'"battery_power_entity_id":null' in canonical
    assert b'"battery_power_sign":null' in canonical
    assert b'"device_timestamp_entity_id":null' in canonical


@pytest.mark.parametrize("category", ("persistent", "protection", "capability", "policy", "slot_id", "owner"))
def test_mapping_fingerprint_covers_every_managed_category(category):
    solis, *_ = fixture()
    before = mapping_fingerprint(solis)
    if category == "persistent":
        object.__setattr__(solis.persistent, "storage_mode_entity_id", "select.changed")
    elif category == "protection":
        object.__setattr__(solis.protection, "battery_reserve_entity_id", "switch.changed")
    elif category == "capability":
        object.__setattr__(solis.capability, "max_output_power_entity_id", "number.changed")
    elif category == "policy":
        object.__setattr__(solis, "maximum_grid_import_policy", "changed")
    elif category == "slot_id":
        object.__setattr__(solis.slots[0].charge, "time_entity_id", "text.changed")
    else:
        object.__setattr__(solis.slots[0].charge, "owner", "changed")
    assert mapping_fingerprint(solis) != before


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
@pytest.mark.parametrize("post_state", (None, "on", "unknown", "unavailable", "invalid_context"))
async def test_guard_change_after_enable_always_cleans_all_directions(post_state):
    controller, ha, snapshot = actuator()

    def change_guard(domain, service, data):
        if domain == "switch" and service == "turn_on" and data["entity_id"].endswith("_slot1_charge"):
            guard_id = controller.control_disable_guard_entity_id
            if post_state is None:
                del ha.states[guard_id]
            elif post_state == "invalid_context":
                ha.states[guard_id]["context_id"] = None
            else:
                ha.states[guard_id]["state"] = post_state

    ha.on_call = change_guard
    result = await controller.async_apply_intent(intent(), snapshot, now=NOW)
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "direction", "physical_slot"),
    (
        (SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE, 1),
        (SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, 1),
        (SlotOwner.PRE_DISCHARGE, SlotDirection.DISCHARGE, 2),
    ),
)
async def test_exact_three_directional_owners_are_targetable(owner, direction, physical_slot):
    controller, ha, observation = actuator()
    result = await controller.async_apply_intent(
        intent(owner=owner, direction=direction, physical_slot=physical_slot),
        observation,
        now=NOW,
    )
    assert result.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION
    target = controller.config.slots[physical_slot - 1]
    expected = target.charge.enable_entity_id if direction is SlotDirection.CHARGE else target.discharge.enable_entity_id
    assert ha.states[expected]["state"] == "on"
    assert all(ha.states[entity_id]["state"] == ("on" if entity_id == expected else "off") for entity_id in enable_ids(controller))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "direction", "physical_slot"),
    (
        (SlotOwner.CHEAP_CHARGING, SlotDirection.DISCHARGE, 1),
        (SlotOwner.FULL_SOC_CYCLING, SlotDirection.CHARGE, 1),
        (SlotOwner.PRE_DISCHARGE, SlotDirection.CHARGE, 2),
        (SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE, 2),
        (SlotOwner.PRE_DISCHARGE, SlotDirection.DISCHARGE, 3),
    ),
)
async def test_all_other_owner_direction_allocations_fail_safe(owner, direction, physical_slot):
    controller, ha, observation = actuator()
    result = await controller.async_apply_intent(
        intent(owner=owner, direction=direction, physical_slot=physical_slot),
        observation,
        now=NOW,
    )
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert not [call for call in ha.calls if call[1] == "turn_on"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ("missing", "malformed", "future", "schema", "ha", "device", "fingerprint"))
async def test_every_invalid_commissioning_gate_only_permits_cleanup(gate):
    controller, ha, observation = actuator()
    fingerprint = controller.mapping_fingerprint
    records = {
        "missing": None,
        "malformed": object(),
        "future": CommissioningRecord(NOW + timedelta(seconds=1), fingerprint, True, True),
        "schema": CommissioningRecord(NOW, fingerprint, True, True, schema_version=999),
        "ha": CommissioningRecord(NOW, fingerprint, False, True),
        "device": CommissioningRecord(NOW, fingerprint, True, False),
        "fingerprint": CommissioningRecord(NOW, "0" * 64, True, True),
    }
    controller.commissioning = records[gate]
    result = await controller.async_apply_intent(intent(), observation, now=NOW)
    assert result.status == SlotActuationStatus.BLOCKED_UNCOMMISSIONED_SAFE
    assert not [call for call in ha.calls if call[0] in {"text", "number"} or call[1] == "turn_on"]


@pytest.mark.asyncio
async def test_degraded_result_never_authorizes_even_with_complete_snapshot():
    controller, ha, observation = actuator()
    degraded = replace(observation, health=ControllerHealth.DEGRADED)
    result = await controller.async_apply_intent(intent(), degraded, now=NOW)
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert not [call for call in ha.calls if call[1] == "turn_on"]


@pytest.mark.asyncio
async def test_inverter_clock_is_authoritative_not_telemetry_device_timestamp():
    controller, _ha, observation = actuator()
    snapshot = observation.snapshot
    old_telemetry = replace(snapshot.telemetry, device_timestamp=NOW - timedelta(hours=1))
    telemetry_old = replace(observation, snapshot=replace(snapshot, telemetry=old_telemetry))
    accepted = await controller.async_apply_intent(intent(), telemetry_old, now=NOW)
    assert accepted.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION

    controller, _ha, observation = actuator()
    snapshot = observation.snapshot
    old_inverter = replace(snapshot.persistent, inverter_time=NOW - MAXIMUM_INVERTER_CLOCK_SKEW - timedelta(seconds=1))
    inverter_old = replace(observation, snapshot=replace(snapshot, persistent=old_inverter))
    rejected = await controller.async_apply_intent(intent(), inverter_old, now=NOW)
    assert rejected.status == SlotActuationStatus.FAILED_SAFE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end", "expiry", "accepted"),
    (
        (NOW - timedelta(minutes=1), NOW + timedelta(hours=1), NOW + timedelta(minutes=10), True),
        (NOW + timedelta(minutes=1), NOW + timedelta(hours=1), NOW + timedelta(hours=1), False),
        (NOW - timedelta(hours=1), NOW, NOW + timedelta(hours=1), False),
        (NOW - timedelta(hours=1), NOW + timedelta(hours=1), NOW, False),
    ),
)
async def test_active_interval_is_strictly_bounded_by_expiry(start, end, expiry, accepted):
    controller, _ha, observation = actuator()
    result = await controller.async_apply_intent(intent(start=start, end=end, expiry=expiry), observation, now=NOW)
    if accepted:
        assert result.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION
        assert result.mandatory_disable_deadline == min(end, expiry)
    else:
        assert result.status == SlotActuationStatus.FAILED_SAFE


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "accepted"), ((Decimal("0"), True), (Decimal("10"), True), (Decimal("10.5"), False), (Decimal("11"), False)))
async def test_exact_current_capability_bounds_and_step(current, accepted):
    controller, _ha, observation = actuator()
    result = await controller.async_apply_intent(intent(current=current), observation, now=NOW)
    assert (result.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize(("target_soc", "accepted"), ((Decimal("0"), True), (Decimal("100"), True), (Decimal("50.5"), False)))
async def test_exact_target_soc_capability_bounds_and_step(target_soc, accepted):
    controller, _ha, observation = actuator()
    result = await controller.async_apply_intent(intent(target_soc=target_soc), observation, now=NOW)
    assert (result.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION) is accepted


def test_schedule_rejects_nonexistent_transition_and_multiday_representation():
    zone = ZoneInfo("Europe/London")
    with pytest.raises(ValueError, match="ambiguous|nonexistent"):
        encode_schedule(
            datetime(2026, 3, 29, 1, 30, tzinfo=zone),
            datetime(2026, 3, 29, 3, 30, tzinfo=zone),
            zone,
        )
    with pytest.raises(ValueError, match="24 hours"):
        encode_schedule(NOW, NOW + timedelta(days=1), timezone.utc)


def test_inverter_timezone_is_required_explicitly():
    solis, states, guard, _observation = fixture()
    with pytest.raises(TypeError):
        SolisSlotActuator(
            solis,
            HomeAssistantWriter(FakeHA(states)),
            control_disable_guard_entity_id=guard,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_state", (None, "on", "unknown", "unavailable", "invalid_context"))
async def test_guard_requires_exact_revision_verified_off(guard_state):
    controller, ha, observation = actuator()
    guard_id = controller.control_disable_guard_entity_id
    if guard_state is None:
        del ha.states[guard_id]
    elif guard_state == "invalid_context":
        ha.states[guard_id]["context_id"] = None
    else:
        ha.states[guard_id]["state"] = guard_state
    result = await controller.async_apply_intent(intent(), observation, now=NOW)
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert not [call for call in ha.calls if call[1] == "turn_on"]


@pytest.mark.asyncio
async def test_live_enabled_switch_is_disabled_before_configuration():
    controller, ha, observation = actuator()
    other = controller.config.slots[4].discharge.enable_entity_id
    ha.states[other]["state"] = "on"
    result = await controller.async_apply_intent(intent(current=Decimal("2")), observation, now=NOW)
    assert result.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION
    off_index = next(index for index, call in enumerate(ha.calls) if call[1] == "turn_off" and call[2]["entity_id"] == other)
    config_index = next(index for index, call in enumerate(ha.calls) if call[0] in {"text", "number"})
    assert off_index < config_index


@pytest.mark.asyncio
async def test_external_second_enable_after_configuration_is_caught_and_cleaned():
    controller, ha, observation = actuator()
    intruder = controller.config.slots[5].discharge.enable_entity_id

    def race(domain, service, data):
        if domain == "number" and data["entity_id"] == controller.config.slots[0].charge.target_soc_entity_id:
            ha.states[intruder].update(state="on", last_updated=NOW + timedelta(seconds=5), context_id="external")

    ha.on_call = race
    result = await controller.async_apply_intent(intent(current=Decimal("2"), target_soc=Decimal("51")), observation, now=NOW)
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))
    assert not [call for call in ha.calls if call[1] == "turn_on" and call[2]["entity_id"] == controller.config.slots[0].charge.enable_entity_id]


@pytest.mark.asyncio
async def test_disable_all_continues_all_twelve_and_invalid_revision_is_unsafe():
    controller, ha, _observation = actuator()
    ids = enable_ids(controller)
    for entity_id in ids:
        ha.states[entity_id]["state"] = "on"
    failed = ids[3]
    original = ha.async_call

    async def fail_one(domain, service, data, *, blocking=True):
        if data["entity_id"] == failed:
            ha.calls.append((domain, service, data))
            raise RuntimeError("injected")
        await original(domain, service, data, blocking=blocking)

    ha.async_call = fail_one
    result = await controller.async_disable_all()
    assert not result.safe
    assert len([call for call in ha.calls if call[1] == "turn_off"]) == 12
    assert ha.states[failed]["state"] == "on"

    controller, ha, _observation = actuator()
    entity_id = enable_ids(controller)[0]
    for invalid_state in ("unknown", "unavailable", "missing_revision", "missing_context"):
        controller, ha, _observation = actuator()
        entity_id = enable_ids(controller)[0]
        if invalid_state == "missing_revision":
            del ha.states[entity_id]["last_updated"]
        elif invalid_state == "missing_context":
            ha.states[entity_id]["context_id"] = None
        else:
            ha.states[entity_id]["state"] = invalid_state
        invalid = await controller.async_disable_all()
        assert not invalid.safe


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("disable", "time", "current", "soc", "enable"))
async def test_service_failure_at_each_transition_always_runs_separate_cleanup(stage):
    controller, ha, observation = actuator()
    slot = controller.config.slots[0].charge
    other = controller.config.slots[4].charge.enable_entity_id
    if stage == "disable":
        ha.states[other]["state"] = "on"
    expected = {
        "disable": other,
        "time": slot.time_entity_id,
        "current": slot.current_entity_id,
        "soc": slot.target_soc_entity_id,
        "enable": slot.enable_entity_id,
    }[stage]
    original = ha.async_call
    failed = False

    async def fail_once(domain, service, data, *, blocking=True):
        nonlocal failed
        if data["entity_id"] == expected and not failed:
            failed = True
            ha.calls.append((domain, service, data))
            raise RuntimeError("injected transition failure")
        await original(domain, service, data, blocking=blocking)

    ha.async_call = fail_once
    result = await controller.async_apply_intent(
        intent(current=Decimal("2"), target_soc=Decimal("51")), observation, now=NOW
    )
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("time", "current", "soc", "enable"))
async def test_uncertain_readback_at_each_application_stage_fails_safe(stage, monkeypatch):
    from custom_components.house_battery_control import ha_writer

    monkeypatch.setattr(ha_writer, "HA_READBACK_TIMEOUT", timedelta(0))
    controller, ha, observation = actuator()
    slot = controller.config.slots[0].charge
    expected = {
        "time": slot.time_entity_id,
        "current": slot.current_entity_id,
        "soc": slot.target_soc_entity_id,
        "enable": slot.enable_entity_id,
    }[stage]
    original = ha.async_call
    suppressed = False

    async def suppress_once(domain, service, data, *, blocking=True):
        nonlocal suppressed
        if data["entity_id"] == expected and not suppressed:
            suppressed = True
            ha.calls.append((domain, service, data))
            return
        await original(domain, service, data, blocking=blocking)

    ha.async_call = suppress_once
    result = await controller.async_apply_intent(
        intent(current=Decimal("2"), target_soc=Decimal("51")), observation, now=NOW
    )
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert any(write.outcome.value == "readback_timeout" for write in result.results)
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("time", "enable"))
async def test_revision_conflict_during_configuration_or_enable_fails_safe(stage):
    controller, ha, observation = actuator()
    slot = controller.config.slots[0].charge
    expected = slot.time_entity_id if stage == "time" else slot.enable_entity_id
    original_get = ha.get_state
    reads = 0

    def conflicting_get(entity_id):
        nonlocal reads
        if entity_id == expected:
            reads += 1
            conflict_read = 2 if stage == "time" else 5
            if reads == conflict_read:
                ha.states[entity_id]["last_updated"] += timedelta(seconds=1)
                ha.states[entity_id]["context_id"] = "external"
        return original_get(entity_id)

    ha.get_state = conflicting_get
    result = await controller.async_apply_intent(
        intent(current=Decimal("2"), target_soc=Decimal("51")), observation, now=NOW
    )
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert any(write.outcome.value == "conflict" for write in result.results)


@pytest.mark.asyncio
async def test_guard_assertion_after_configuration_but_before_enable_fails_safe():
    controller, ha, observation = actuator()
    guard_id = controller.control_disable_guard_entity_id
    soc_id = controller.config.slots[0].charge.target_soc_entity_id

    def assert_guard(domain, service, data):
        if data["entity_id"] == soc_id:
            ha.states[guard_id].update(state="on", last_updated=NOW + timedelta(seconds=3), context_id="external")

    ha.on_call = assert_guard
    result = await controller.async_apply_intent(
        intent(current=Decimal("2"), target_soc=Decimal("51")), observation, now=NOW
    )
    assert result.status == SlotActuationStatus.FAILED_SAFE
    assert not [call for call in ha.calls if call[1] == "turn_on"]


async def _cancel_during_service(controller, ha, observation, expected_entity, *, repeated=False, invalid_intent=False):
    original = ha.async_call
    call_count = 0
    gate = asyncio.Event()

    async def block(domain, service, data, *, blocking=True):
        nonlocal call_count
        if data["entity_id"] == expected_entity and call_count < (2 if repeated else 1):
            call_count += 1
            await gate.wait()
        await original(domain, service, data, blocking=blocking)

    ha.async_call = block
    candidate = intent(owner=SlotOwner.CHEAP_CHARGING, direction=SlotDirection.DISCHARGE) if invalid_intent else intent(current=Decimal("2"), target_soc=Decimal("51"))
    task = asyncio.create_task(controller.async_apply_intent(candidate, observation, now=NOW))
    while call_count < 1:
        await asyncio.sleep(0)
    task.cancel("original")
    if repeated:
        while call_count < 2:
            await asyncio.sleep(0)
        task.cancel("repeated")
    gate.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    diagnostic = controller.last_cancellation_result
    assert diagnostic is not None
    assert diagnostic.original_exception is raised.value
    assert diagnostic.all_directions_proven_off
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("disable", "configuration", "enable", "failure_cleanup"))
async def test_cancellation_at_each_stage_records_diagnostics_and_re_raises_original(stage):
    controller, ha, observation = actuator()
    slot = controller.config.slots[0].charge
    invalid_intent = False
    if stage in {"disable", "failure_cleanup"}:
        expected = controller.config.slots[4].discharge.enable_entity_id
        ha.states[expected]["state"] = "on"
        invalid_intent = stage == "failure_cleanup"
    elif stage == "configuration":
        expected = slot.time_entity_id
    else:
        expected = slot.enable_entity_id
    await _cancel_during_service(
        controller,
        ha,
        observation,
        expected,
        invalid_intent=invalid_intent,
    )


@pytest.mark.asyncio
async def test_cancellation_before_transaction_still_waits_for_safe_cleanup():
    controller, ha, observation = actuator()
    lock_held = asyncio.Event()
    release = asyncio.Event()

    async def hold_writer():
        async with controller.writer.transaction():
            lock_held.set()
            await release.wait()

    holder = asyncio.create_task(hold_writer())
    await lock_held.wait()
    task = asyncio.create_task(controller.async_apply_intent(intent(), observation, now=NOW))
    await asyncio.sleep(0)
    task.cancel("before")
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    await holder
    diagnostic = controller.last_cancellation_result
    assert diagnostic is not None
    assert diagnostic.original_exception is raised.value
    assert diagnostic.all_directions_proven_off
    assert all(ha.states[entity_id]["state"] == "off" for entity_id in enable_ids(controller))


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_replace_original_or_cancel_cleanup():
    controller, ha, observation = actuator()
    expected = controller.config.slots[3].charge.enable_entity_id
    ha.states[expected]["state"] = "on"
    await _cancel_during_service(controller, ha, observation, expected, repeated=True)


@pytest.mark.asyncio
async def test_direct_cleanup_cancellation_starts_one_fresh_shielded_cleanup():
    controller, ha, _observation = actuator()
    expected = controller.config.slots[2].discharge.enable_entity_id
    ha.states[expected]["state"] = "on"
    original = ha.async_call
    started = asyncio.Event()
    first = True

    async def block_first(domain, service, data, *, blocking=True):
        nonlocal first
        if data["entity_id"] == expected and first:
            first = False
            started.set()
            await asyncio.Event().wait()
        await original(domain, service, data, blocking=blocking)

    ha.async_call = block_first
    task = asyncio.create_task(controller.async_disable_all())
    await started.wait()
    task.cancel("direct")
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    diagnostic = controller.last_cancellation_result
    assert diagnostic is not None
    assert diagnostic.original_exception is raised.value
    assert diagnostic.all_directions_proven_off
