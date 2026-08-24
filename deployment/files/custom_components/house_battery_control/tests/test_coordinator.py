"""Focused tests for the transitional SolisAdapter-consuming coordinator."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.coordinator import HEARTBEAT_INTERVAL, Coordinator
from custom_components.house_battery_control.model import ControllerHealth, CycleState, StrategyAction
from custom_components.house_battery_control.planner import Plan
from custom_components.house_battery_control.solis import SolisChange, WriteOutcome, WriteResult

NOW = datetime(2026, 8, 22, 10, tzinfo=UTC)


def config() -> integration_config.Config:
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    return integration_config.from_mapping(source)


def observation(health: ControllerHealth = ControllerHealth.HEALTHY) -> SimpleNamespace:
    return SimpleNamespace(
        health=health,
        telemetry=SimpleNamespace(state_of_charge_percent=Decimal("55"), battery_power_kw=Decimal("0")),
        issues=(),
    )


def plan(*, state=CycleState.IDLE, deadline=None, issue=None) -> Plan:
    return Plan(
        action=StrategyAction.IDLE, intent=None, next_cycle_state=state,
        cycle_deadline=deadline, reserve_soc_percent=None if issue else Decimal("20"),
        reserve_energy_kwh=Decimal("6.4"), battery_energy_kwh=Decimal("17.6"),
        reserve_balance_kwh=Decimal("11.2"), maximum_charge_power_kw=Decimal("5.12"),
        maximum_discharge_power_kw=Decimal("5.12"), issue=issue,
    )


def adapter(coordinator: Coordinator, *, reconciled: bool = True) -> MagicMock:
    result = MagicMock()
    result.next_start_change.return_value = None
    result.intent_matches.return_value = reconciled
    result.apply = AsyncMock(return_value=WriteResult("text.slot", WriteOutcome.APPLIED, "applied"))
    result.set_mode = AsyncMock(return_value=WriteResult("select.mode", WriteOutcome.APPLIED, "applied"))
    coordinator.solis_adapter = result
    return result


async def test_coordinator_reads_solis_once_builds_plan_and_confirms_complete_intent(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    solis = adapter(coordinator)
    observed = observation()
    planned = plan(state=CycleState.CHARGING)
    with (
        patch.object(Coordinator, "_now", return_value=NOW),
        patch("custom_components.house_battery_control.coordinator.read_state", return_value=observed) as read,
        patch("custom_components.house_battery_control.coordinator.build_plan", AsyncMock(return_value=planned)) as build,
    ):
        result = await coordinator._async_update_data()

    read.assert_called_once_with(hass, coordinator.config.solis, now=NOW)
    build.assert_awaited_once_with(
        hass, coordinator.config, observed, now=NOW,
        cycle_state=CycleState.IDLE, cycle_deadline=None,
    )
    solis.next_start_change.assert_called_once_with(observed, None, reserve_soc_percent=Decimal("20"))
    solis.intent_matches.assert_called_once()
    solis.apply.assert_not_awaited()
    assert result.health is ControllerHealth.HEALTHY
    assert coordinator._cycle_state is CycleState.CHARGING


async def test_plan_issue_is_degraded_without_adapter_call_and_preserves_cycle(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    solis = adapter(coordinator)
    deadline = NOW + timedelta(minutes=8)
    coordinator._cycle_state = CycleState.CYCLE_DISCHARGING
    coordinator._cycle_deadline = deadline
    unavailable = plan(state=CycleState.CYCLE_DISCHARGING, deadline=deadline, issue="tariff unavailable")
    with (
        patch.object(Coordinator, "_now", return_value=NOW),
        patch("custom_components.house_battery_control.coordinator.read_state", return_value=observation()),
        patch("custom_components.house_battery_control.coordinator.build_plan", AsyncMock(return_value=unavailable)),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.DEGRADED
    assert result.last_error == "tariff unavailable"
    assert coordinator._cycle_state is CycleState.CYCLE_DISCHARGING
    assert coordinator._cycle_deadline == deadline
    solis.next_start_change.assert_not_called()
    solis.apply.assert_not_awaited()


async def test_one_successful_change_stays_degraded_until_fresh_state_proves_complete(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    solis = adapter(coordinator)
    change = MagicMock(spec=SolisChange)
    solis.next_start_change.return_value = change
    with (
        patch.object(Coordinator, "_now", return_value=NOW),
        patch("custom_components.house_battery_control.coordinator.read_state", return_value=observation()),
        patch("custom_components.house_battery_control.coordinator.build_plan", AsyncMock(return_value=plan())),
    ):
        result = await coordinator._async_update_data()
    assert result.health is ControllerHealth.DEGRADED
    assert "advanced one change" in result.reason
    solis.apply.assert_awaited_once()
    solis.intent_matches.assert_not_called()


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


async def test_transitional_stop_is_idempotent_and_does_not_invent_shutdown_writes(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    solis = adapter(coordinator)
    unsubscribe = MagicMock()
    coordinator._unsub_sources = unsubscribe
    await coordinator.async_stop()
    await coordinator.async_stop()
    unsubscribe.assert_called_once_with()
    solis.apply.assert_not_awaited()
    solis.set_mode.assert_not_awaited()


def test_heartbeat_interval_is_one_minute() -> None:
    assert HEARTBEAT_INTERVAL == timedelta(minutes=1)
