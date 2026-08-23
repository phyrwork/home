"""Focused tests for the Solis policy fail-safe and healthy baseline."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.contracts import StorageMode
from custom_components.house_battery_control.ha_writer import HomeAssistantWriter
from custom_components.house_battery_control.solis_policy import (
    PolicyActuationResult,
    SolisPolicyActuator,
)
from custom_components.house_battery_control.solis_reader import read_solis_state
from custom_components.house_battery_control.write_contracts import (
    WriteOutcome,
    WriteResult,
)


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class FakeHA:
    def __init__(self, states):
        self.states = states
        self.calls = []
        self.listeners = {}

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
        solis.persistent.storage_mode_entity_id: _state("Self-Use", attributes={"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]}),
        solis.persistent.inverter_time_entity_id: _state(NOW.isoformat()),
    }
    for entity_id in (solis.persistent.allow_grid_charging_entity_id,):
        states[entity_id] = _state("off")
    states[solis.protection.battery_reserve_entity_id] = _state("on")
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


def policy(*, guard_state="off"):
    solis, states, guard, observation = fixture()
    states[guard]["state"] = guard_state
    ha = FakeHA(states)
    return SolisPolicyActuator(solis, HomeAssistantWriter(ha), control_disable_guard_entity_id=guard, inverter_timezone=timezone.utc), ha, observation


@pytest.mark.asyncio
async def test_safe_baseline_selects_self_use_and_disables_reserve_with_guard_off():
    actuator, ha, _observation = policy(guard_state="off")
    result = await actuator.async_apply_safe_baseline()

    assert result.success and result.safe
    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == StorageMode.SELF_USE.value
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"


@pytest.mark.asyncio
async def test_safe_baseline_writes_mode_before_reserve():
    actuator, ha, _observation = policy(guard_state="on")
    ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] = "Feed-In Priority"

    result = await actuator.async_apply_safe_baseline()

    assert result.success
    written_entities = [call[2]["entity_id"] for call in ha.calls]
    assert written_entities[-2:] == [
        actuator.config.persistent.storage_mode_entity_id,
        actuator.config.protection.battery_reserve_entity_id,
    ]


@pytest.mark.asyncio
async def test_healthy_writes_mode_before_reserve():
    actuator, ha, observation = policy()
    ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] = "Self-Use"
    ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] = "off"

    result = await actuator.async_apply_healthy(
        observation=observation,
        reserve_soc_percent=10,
        intent=None,
        now=NOW,
    )

    assert result.success
    written_entities = [call[2]["entity_id"] for call in ha.calls]
    assert written_entities[-3:] == [
        actuator.config.persistent.storage_mode_entity_id,
        actuator.config.persistent.allow_grid_charging_entity_id,
        actuator.config.protection.battery_reserve_entity_id,
    ]


@pytest.mark.asyncio
async def test_healthy_baseline_selects_feed_in_priority_and_proves_no_slots():
    actuator, ha, observation = policy()
    result = await actuator.async_apply_healthy(
        observation=observation,
        reserve_soc_percent=10,
        intent=None,
        now=NOW,
    )

    assert result.success
    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == StorageMode.FEED_IN_PRIORITY.value
    assert ha.states[actuator.config.persistent.allow_grid_charging_entity_id]["state"] == "on"
    assert result.slot_result is None


@pytest.mark.asyncio
async def test_guard_assertion_during_healthy_write_falls_back_to_safe_baseline():
    actuator, ha, observation = policy()
    original_call = ha.async_call

    async def assert_guard_after_first_write(domain, service, data, *, blocking=True):
        await original_call(domain, service, data, blocking=blocking)
        if data["entity_id"] == actuator.config.persistent.storage_mode_entity_id:
            ha.states[actuator.control_disable_guard_entity_id]["state"] = "on"

    ha.async_call = assert_guard_after_first_write
    result = await actuator.async_apply_healthy(
        observation=observation,
        reserve_soc_percent=10,
        intent=None,
        now=NOW,
    )

    assert not result.success
    assert result.safe
    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == StorageMode.SELF_USE.value
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"
    assert all(
        ha.states[direction.enable_entity_id]["state"] == "off"
        for slot in actuator.config.slots
        for direction in (slot.charge, slot.discharge)
    )


async def _cancel_repeatedly_during_write(actuator, ha, operation):
    gate = asyncio.Event()
    started = asyncio.Event()
    original_call = ha.async_call

    async def blocked_call(domain, service, data, *, blocking=True):
        started.set()
        await gate.wait()
        return await original_call(domain, service, data, blocking=blocking)

    ha.async_call = blocked_call
    task = asyncio.create_task(operation())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_safe_baseline_completes_slot_and_persistent_cleanup_before_cancellation():
    actuator, ha, _observation = policy(guard_state="off")

    await _cancel_repeatedly_during_write(actuator, ha, actuator.async_apply_safe_baseline)

    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == StorageMode.SELF_USE.value
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"
    assert all(
        ha.states[direction.enable_entity_id]["state"] == "off"
        for slot in actuator.config.slots
        for direction in (slot.charge, slot.discharge)
    )


@pytest.mark.asyncio
async def test_safe_baseline_leaves_guard_off_and_proves_safe_outputs():
    actuator, ha, _observation = policy(guard_state="off")

    result = await actuator.async_apply_safe_baseline()

    assert result.success
    assert result.safe
    assert ha.states[actuator.control_disable_guard_entity_id]["state"] == "off"
    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == StorageMode.SELF_USE.value
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"


@pytest.mark.asyncio
async def test_safe_baseline_does_not_depend_on_guard_availability():
    actuator, ha, _observation = policy(guard_state="unavailable")

    result = await actuator.async_apply_safe_baseline()

    assert result.success
    assert result.safe


@pytest.mark.asyncio
async def test_healthy_cancellation_falls_back_without_lock_deadlock():
    actuator, ha, observation = policy()

    await _cancel_repeatedly_during_write(
        actuator,
        ha,
        lambda: actuator.async_apply_healthy(
            observation=observation,
            reserve_soc_percent=10,
            intent=None,
            now=NOW,
        ),
    )

    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == StorageMode.SELF_USE.value
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"


@pytest.mark.asyncio
async def test_healthy_rejects_reserve_below_required_capability():
    actuator, ha, observation = policy()
    entity_id = actuator.config.protection.battery_reserve_soc_entity_id
    ha.states[entity_id]["state"] = "5"
    ha.states[entity_id]["attributes"]["max"] = "5"
    observation = read_solis_state(actuator.config, ha.states, NOW)

    result = await actuator.async_apply_healthy(
        observation=observation,
        reserve_soc_percent=10,
        intent=None,
        now=NOW,
    )

    assert not result.success
    assert result.safe
    assert "cannot represent" in result.message
    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == StorageMode.SELF_USE.value
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"


def _policy_result(outcome: WriteOutcome, label: str) -> PolicyActuationResult:
    result = WriteResult(f"switch.{label}", outcome, label)
    return PolicyActuationResult(
        result.success,
        result.success,
        label,
        (result,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeout_outcome",
    (WriteOutcome.SERVICE_TIMEOUT, WriteOutcome.READBACK_TIMEOUT),
)
async def test_safe_baseline_retries_one_timeout_and_preserves_ordered_evidence(
    timeout_outcome,
) -> None:
    actuator, _ha, _observation = policy()
    first = _policy_result(timeout_outcome, "first")
    second = _policy_result(WriteOutcome.NO_CHANGE, "second")
    attempt = AsyncMock(side_effect=(first, second))
    actuator._apply_safe_baseline_once_locked = attempt

    with patch(
        "custom_components.house_battery_control.solis_policy.asyncio.sleep",
        AsyncMock(),
    ) as sleep:
        result = await actuator.async_apply_safe_baseline()

    assert result.success and result.safe
    assert [item.entity_id for item in result.results] == [
        "switch.first",
        "switch.second",
    ]
    assert attempt.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    (
        WriteOutcome.CONFLICT,
        WriteOutcome.REJECTED,
        WriteOutcome.SERVICE_ERROR,
    ),
)
async def test_safe_baseline_does_not_retry_non_timeout_failures(
    outcome,
) -> None:
    actuator, _ha, _observation = policy()
    attempt = AsyncMock(return_value=_policy_result(outcome, "hard"))
    actuator._apply_safe_baseline_once_locked = attempt

    with patch(
        "custom_components.house_battery_control.solis_policy.asyncio.sleep",
        AsyncMock(),
    ) as sleep:
        result = await actuator.async_apply_safe_baseline()

    assert not result.safe
    attempt.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_baseline_does_not_retry_mixed_timeout_and_rejection() -> None:
    actuator, _ha, _observation = policy()
    first = PolicyActuationResult(
        False,
        False,
        "mixed",
        (
            WriteResult(
                "switch.timeout", WriteOutcome.SERVICE_TIMEOUT, "timeout"
            ),
            WriteResult(
                "switch.rejected", WriteOutcome.REJECTED, "rejected"
            ),
        ),
    )
    attempt = AsyncMock(return_value=first)
    actuator._apply_safe_baseline_once_locked = attempt

    result = await actuator.async_apply_safe_baseline()

    assert not result.safe
    attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_baseline_deadline_expiry_prevents_timeout_retry(
    monkeypatch,
) -> None:
    actuator, _ha, _observation = policy()

    async def slow_first(*, deadline):
        await asyncio.sleep(0.02)
        return _policy_result(WriteOutcome.SERVICE_TIMEOUT, "timeout")

    attempt = AsyncMock(side_effect=slow_first)
    actuator._apply_safe_baseline_once_locked = attempt
    monkeypatch.setattr(
        "custom_components.house_battery_control.solis_policy.SAFE_BASELINE_TIMEOUT",
        timedelta(seconds=0.01),
    )

    result = await actuator.async_apply_safe_baseline()

    assert not result.safe
    attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_baseline_deadline_bounds_orchestration_lock() -> None:
    actuator, _ha, _observation = policy()
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock() -> None:
        async with actuator._lock:
            acquired.set()
            await release.wait()

    holder = asyncio.create_task(hold_lock())
    await acquired.wait()
    with patch(
        "custom_components.house_battery_control.solis_policy.SAFE_BASELINE_TIMEOUT",
        timedelta(seconds=0.01),
    ):
        result = await actuator.async_apply_safe_baseline()
    release.set()
    await holder

    assert not result.safe
    assert "deadline" in result.message
    assert not actuator._lock._lock.locked()


@pytest.mark.asyncio
async def test_healthy_service_timeout_is_not_replayed_before_safe_cleanup(
    monkeypatch,
) -> None:
    actuator, ha, observation = policy()
    storage_mode_id = actuator.config.persistent.storage_mode_entity_id
    original_call = ha.async_call
    never = asyncio.Event()

    async def timeout_healthy_mode(domain, service, data, *, blocking=True):
        if (
            data["entity_id"] == storage_mode_id
            and data.get("option") == StorageMode.FEED_IN_PRIORITY.value
        ):
            ha.calls.append((domain, service, data))
            await never.wait()
            return
        await original_call(domain, service, data, blocking=blocking)

    ha.async_call = timeout_healthy_mode
    monkeypatch.setattr(
        "custom_components.house_battery_control.ha_writer.HA_SERVICE_CALL_TIMEOUT",
        timedelta(seconds=0.01),
    )
    monkeypatch.setattr(
        "custom_components.house_battery_control.ha_writer.HA_SERVICE_TIMEOUT_CONFIRMATION",
        timedelta(seconds=0.01),
    )

    result = await actuator.async_apply_healthy(
        observation=observation,
        reserve_soc_percent=10,
        intent=None,
        now=NOW,
    )

    feed_in_calls = [
        call
        for call in ha.calls
        if call[2]["entity_id"] == storage_mode_id
        and call[2].get("option") == StorageMode.FEED_IN_PRIORITY.value
    ]
    assert len(feed_in_calls) == 1
    assert not result.success
    assert result.safe
    assert ha.states[storage_mode_id]["state"] == StorageMode.SELF_USE.value


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_reset_safe_cleanup_deadline(
    monkeypatch,
) -> None:
    actuator, ha, _observation = policy()
    never = asyncio.Event()

    async def blocked_call(domain, service, data, *, blocking=True):
        ha.calls.append((domain, service, data))
        await never.wait()

    ha.async_call = blocked_call
    monkeypatch.setattr(
        "custom_components.house_battery_control.solis_policy.SAFE_BASELINE_TIMEOUT",
        timedelta(seconds=0.05),
    )
    task = asyncio.create_task(actuator.async_apply_safe_baseline())
    for _ in range(20):
        await asyncio.sleep(0)
        if ha.calls:
            break
    assert ha.calls
    task.cancel("original")
    await asyncio.sleep(0)
    task.cancel("repeat")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), 0.2)

    assert not actuator._lock._lock.locked()
    assert not actuator.writer._lock.locked()
    assert all(not callbacks for callbacks in ha.listeners.values())


@pytest.mark.asyncio
async def test_safe_baseline_reraises_original_cancelled_error_instance() -> None:
    actuator, _ha, _observation = policy()
    original = asyncio.CancelledError("original")
    actuator._apply_safe_baseline_with_lock = AsyncMock(side_effect=original)
    actuator._finish_safe_baseline_after_cancellation = AsyncMock(
        return_value=PolicyActuationResult(False, False, "cleanup")
    )

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await actuator.async_apply_safe_baseline()

    assert cancelled.value is original
