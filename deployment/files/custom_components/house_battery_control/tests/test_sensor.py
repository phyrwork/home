"""Focused diagnostic sensor tests for the MVP coordinator."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from pathlib import Path

import yaml
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.const import DOMAIN
from custom_components.house_battery_control.contracts import ControllerHealth
from custom_components.house_battery_control.coordinator import Coordinator, Snapshot
from custom_components.house_battery_control.sensor import (
    ActionSensor,
    HealthSensor,
    HeartbeatSensor,
    ReserveSensor,
    async_setup_platform,
)
from custom_components.house_battery_control.strategy import CycleState, StrategyAction


NOW = datetime(2026, 8, 22, 10, tzinfo=UTC)


def coordinator(hass: HomeAssistant, value: Snapshot | None = None) -> Coordinator:
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    result = Coordinator(hass, integration_config.from_mapping(source))
    if value is not None:
        result.async_set_updated_data(value)
    return result


def snapshot() -> Snapshot:
    return Snapshot(
        heartbeat_at=NOW,
        health=ControllerHealth.HEALTHY,
        action=StrategyAction.STOP,
        reason="dynamic control is disabled",
        cycle_state=CycleState.IDLE,
        reserve_soc_percent=Decimal("10"),
        state_of_charge_percent=Decimal("55"),
        battery_power_kw=Decimal("-0.2"),
        last_healthy_at=NOW,
    )


async def test_platform_exposes_only_small_diagnostic_surface(hass: HomeAssistant) -> None:
    instance = coordinator(hass, snapshot())
    hass.data[DOMAIN] = instance
    add_entities = MagicMock()

    await async_setup_platform(hass, {}, add_entities)

    entities = add_entities.call_args.args[0]
    assert [type(entity) for entity in entities] == [
        HeartbeatSensor,
        HealthSensor,
        ActionSensor,
        ReserveSensor,
    ]


def test_sensors_report_disabled_snapshot(hass: HomeAssistant) -> None:
    instance = coordinator(hass, snapshot())
    assert HeartbeatSensor(instance).native_value == NOW
    assert HealthSensor(instance).native_value == "healthy"
    action = ActionSensor(instance)
    assert action.native_value == "STOP"
    assert action.extra_state_attributes["reason"] == "dynamic control is disabled"
    assert action.extra_state_attributes["battery_power_kw"] == -0.2
    assert ReserveSensor(instance).native_value == 10.0


def test_sensors_are_unavailable_without_data(hass: HomeAssistant) -> None:
    instance = coordinator(hass)
    sensors = (
        HeartbeatSensor(instance),
        HealthSensor(instance),
        ActionSensor(instance),
        ReserveSensor(instance),
    )
    assert all(not sensor.available for sensor in sensors)
    assert all(sensor.native_value is None for sensor in sensors)
