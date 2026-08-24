"""Focused tests for the transitional Plan-consuming coordinator."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.coordinator import HEARTBEAT_INTERVAL, Coordinator, _legacy_intent
from custom_components.house_battery_control.model import (
    ControllerHealth, CycleState, LogicalIntent, SlotDirection, SlotIntent,
    SlotOwner, StrategyAction,
)
from custom_components.house_battery_control.planner import Plan
from custom_components.house_battery_control.solis_policy import PolicyActuationResult

NOW = datetime(2026, 8, 22, 10, tzinfo=UTC)


def config() -> integration_config.Config:
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    return integration_config.from_mapping(source)


def observation(health: ControllerHealth = ControllerHealth.HEALTHY) -> SimpleNamespace:
    return SimpleNamespace(
        health=health,
        snapshot=SimpleNamespace(telemetry=SimpleNamespace(
            state_of_charge_percent=Decimal("55"), battery_power_kw=Decimal("0")
        )),
        issues=(),
    )


def plan(*, action=StrategyAction.IDLE, intent=None, state=CycleState.IDLE,
         deadline=None, issue=None) -> Plan:
    return Plan(
        action=action, intent=intent, next_cycle_state=state, cycle_deadline=deadline,
        reserve_soc_percent=None if issue else Decimal("20"),
        reserve_energy_kwh=Decimal("6.4"), battery_energy_kwh=Decimal("17.6"),
        reserve_balance_kwh=Decimal("11.2"), maximum_charge_power_kw=Decimal("5.12"),
        maximum_discharge_power_kw=Decimal("5.12"), issue=issue,
    )


def policy(coordinator: Coordinator) -> MagicMock:
    instance = MagicMock()
    instance.async_apply_safe_baseline = AsyncMock(return_value=PolicyActuationResult(True, True, "safe"))
    instance.async_apply_healthy = AsyncMock(return_value=PolicyActuationResult(True, False, "healthy"))
    coordinator.policy_actuator = instance
    return instance


async def test_coordinator_reads_solis_once_and_consumes_plan(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    actuator = policy(coordinator)
    observed = observation()
    planned = plan(state=CycleState.CHARGING)
    with (
        patch.object(Coordinator, "_now", return_value=NOW),
        patch("custom_components.house_battery_control.coordinator.read_solis_state", return_value=observed) as read,
        patch("custom_components.house_battery_control.coordinator.build_plan", AsyncMock(return_value=planned)) as build,
    ):
        result = await coordinator._async_update_data()

    read.assert_called_once_with(coordinator.config.solis, hass.states, NOW)
    build.assert_awaited_once_with(
        hass, coordinator.config, observed.snapshot, now=NOW,
        cycle_state=CycleState.IDLE, cycle_deadline=None,
    )
    actuator.async_apply_healthy.assert_awaited_once()
    assert result.health is ControllerHealth.HEALTHY
    assert coordinator._cycle_state is CycleState.CHARGING


async def test_plan_issue_is_degraded_without_writes_and_preserves_cycle(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    actuator = policy(coordinator)
    deadline = NOW + timedelta(minutes=8)
    coordinator._cycle_state = CycleState.CYCLE_DISCHARGING
    coordinator._cycle_deadline = deadline
    unavailable = plan(state=CycleState.CYCLE_DISCHARGING, deadline=deadline, issue="tariff unavailable")
    with (
        patch.object(Coordinator, "_now", return_value=NOW),
        patch("custom_components.house_battery_control.coordinator.read_solis_state", return_value=observation()),
        patch("custom_components.house_battery_control.coordinator.build_plan", AsyncMock(return_value=unavailable)),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.DEGRADED
    assert result.last_error == "tariff unavailable"
    assert coordinator._cycle_state is CycleState.CYCLE_DISCHARGING
    assert coordinator._cycle_deadline == deadline
    actuator.async_apply_healthy.assert_not_awaited()
    actuator.async_apply_safe_baseline.assert_not_awaited()


def test_physical_slot_allocation_is_confined_to_temporary_coordinator_bridge() -> None:
    segment = SlotIntent(
        SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE, NOW, NOW + timedelta(minutes=30),
        Decimal("100"), Decimal("100"), NOW + timedelta(minutes=30),
    )
    logical = LogicalIntent((segment,))

    assert not hasattr(segment, "physical_slot")
    assert not hasattr(logical, "physical_slot")
    assert not hasattr(plan(intent=logical), "physical_slot")
    assert _legacy_intent(logical).physical_slot == 1  # type: ignore[union-attr]


def test_source_listener_covers_every_configured_runtime_entity(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    solis = coordinator.config.solis
    expected = {
        solis.telemetry.state_of_charge_entity_id, solis.telemetry.battery_power_entity_id,
        solis.telemetry.battery_voltage_entity_id, solis.telemetry.device_timestamp_entity_id,
        solis.persistent.storage_mode_entity_id, solis.persistent.allow_grid_charging_entity_id,
        solis.persistent.inverter_time_entity_id, solis.protection.battery_reserve_entity_id,
        solis.protection.battery_reserve_soc_entity_id,
        solis.capability.battery_max_charge_current_entity_id,
        solis.capability.battery_max_discharge_current_entity_id,
    }
    for slot in solis.slots:
        for direction in (slot.charge, slot.discharge):
            expected.update((direction.enable_entity_id, direction.time_entity_id,
                             direction.current_entity_id, direction.target_soc_entity_id))
    assert expected.issubset(set(coordinator._source_entity_ids()))


async def test_stop_remains_idempotent_during_transitional_lifecycle(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    actuator = policy(coordinator)
    unsubscribe = MagicMock()
    coordinator._unsub_sources = unsubscribe
    await coordinator.async_stop()
    await coordinator.async_stop()
    unsubscribe.assert_called_once_with()
    actuator.async_apply_safe_baseline.assert_awaited_once_with()


def test_heartbeat_interval_is_one_minute() -> None:
    assert HEARTBEAT_INTERVAL == timedelta(minutes=1)
