"""Focused diagnostic sensor tests for the event-driven controller."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from pathlib import Path

import yaml
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.config import DOMAIN
from custom_components.house_battery_control.model import ControllerHealth
from custom_components.house_battery_control.controller import Controller, Snapshot
from custom_components.house_battery_control.sensor import (
    ActionSensor,
    HealthSensor,
    HeartbeatSensor,
    BatteryEnergySensor,
    ReserveBalanceSensor,
    ReserveSensor,
    ReserveTargetSensor,
    async_setup_platform,
)
from custom_components.house_battery_control.model import CycleState, StrategyAction


NOW = datetime(2026, 8, 22, 10, tzinfo=UTC)


def controller(hass: HomeAssistant, value: Snapshot | None = None) -> Controller:
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    result = Controller(hass, integration_config.from_mapping(source))
    if value is not None:
        result.async_set_updated_data(value)
    return result


def snapshot() -> Snapshot:
    return Snapshot(
        heartbeat_at=NOW,
        health=ControllerHealth.HEALTHY,
        action=StrategyAction.IDLE,
        reason="dynamic control is disabled",
        cycle_state=CycleState.IDLE,
        reserve_soc_percent=Decimal("10"),
        battery_energy_kwh=Decimal("17.68448"),
        reserve_target_energy_kwh=Decimal("12.345678"),
        reserve_balance_kwh=Decimal("5.338802"),
        state_of_charge_percent=Decimal("55"),
        battery_power_kw=Decimal("-0.2"),
        last_healthy_at=NOW,
        degraded_since=NOW - timedelta(minutes=2),
        fail_safe_since=NOW - timedelta(minutes=1),
        pending_operation="stop slot 2 discharge",
        attempt=3,
        next_retry_at=NOW + timedelta(seconds=60),
    )


async def test_platform_exposes_only_small_diagnostic_surface(hass: HomeAssistant) -> None:
    instance = controller(hass, snapshot())
    hass.data[DOMAIN] = instance
    add_entities = MagicMock()

    await async_setup_platform(hass, {}, add_entities)

    entities = add_entities.call_args.args[0]
    assert [type(entity) for entity in entities] == [
        HeartbeatSensor,
        HealthSensor,
        ActionSensor,
        ReserveSensor,
        BatteryEnergySensor,
        ReserveTargetSensor,
        ReserveBalanceSensor,
    ]


def test_sensors_report_disabled_snapshot(hass: HomeAssistant) -> None:
    instance = controller(hass, snapshot())
    assert HeartbeatSensor(instance).native_value == NOW
    assert HealthSensor(instance).native_value == "healthy"
    action = ActionSensor(instance)
    assert action.native_value == "IDLE"
    assert action.extra_state_attributes["reason"] == "dynamic control is disabled"
    assert action.extra_state_attributes["battery_power_kw"] == -0.2
    assert action.extra_state_attributes["pending_operation"] == "stop slot 2 discharge"
    assert action.extra_state_attributes["attempt"] == 3
    assert action.extra_state_attributes["next_retry_at"] == (NOW + timedelta(seconds=60)).isoformat()
    heartbeat = HeartbeatSensor(instance)
    assert heartbeat.extra_state_attributes["degraded_since"] == (NOW - timedelta(minutes=2)).isoformat()
    assert heartbeat.extra_state_attributes["fail_safe_since"] == (NOW - timedelta(minutes=1)).isoformat()
    assert ReserveSensor(instance).native_value == 10.0

    energy = BatteryEnergySensor(instance)
    reserve_target = ReserveTargetSensor(instance)
    reserve_balance = ReserveBalanceSensor(instance)
    assert energy.native_value == 17.68448
    assert reserve_target.native_value == 12.345678
    assert reserve_balance.native_value == 5.338802
    assert [sensor.unique_id for sensor in (energy, reserve_target, reserve_balance)] == [
        "house_battery_control_energy",
        "house_battery_control_reserve_target",
        "house_battery_control_reserve_balance",
    ]
    assert [sensor.name for sensor in (energy, reserve_target, reserve_balance)] == [
        "House Battery Energy",
        "House Battery Reserve Target",
        "House Battery Reserve Balance",
    ]
    for sensor in (energy, reserve_target, reserve_balance):
        assert sensor.device_class is SensorDeviceClass.ENERGY
        assert sensor.native_unit_of_measurement is UnitOfEnergy.KILO_WATT_HOUR
        assert sensor.state_class is None
        assert sensor.suggested_display_precision == 2


def test_sensors_are_unavailable_without_data(hass: HomeAssistant) -> None:
    instance = controller(hass)
    sensors = (
        HeartbeatSensor(instance),
        HealthSensor(instance),
        ActionSensor(instance),
        ReserveSensor(instance),
        BatteryEnergySensor(instance),
        ReserveTargetSensor(instance),
        ReserveBalanceSensor(instance),
    )
    assert all(not sensor.available for sensor in sensors)
    assert all(sensor.native_value is None for sensor in sensors)
