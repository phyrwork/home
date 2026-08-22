from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import (
    battery,
    controller,
    planner,
    tariff,
)
from custom_components.house_battery_control.const import DOMAIN
from custom_components.house_battery_control.coordinator import (
    Coordinator,
    Decision,
    Snapshot,
)
from custom_components.house_battery_control.dependencies import solis_cloud
from custom_components.house_battery_control.interval import TimeInterval
from custom_components.house_battery_control.sensor import (
    BatteryEnergySensor,
    ControlSensor,
    HealthSensor,
    HeartbeatSensor,
    ReserveBalanceSensor,
    ReserveTargetSensor,
    async_setup_platform,
)

NOW = datetime(2026, 7, 5, 10, tzinfo=UTC)
END = NOW + timedelta(minutes=30)


def snapshot() -> Snapshot:
    spec = battery.Spec(
        capacity_kwh=Decimal("32"),
        minimum_energy_kwh=Decimal("3.2"),
        maximum_charge_power_kw=Decimal("6"),
        maximum_discharge_power_kw=Decimal("6"),
        charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"),
    )
    reserve = planner.ReserveInterval(
        interval=TimeInterval(NOW, END),
        start_energy_kwh=Decimal("8"),
        end_energy_kwh=Decimal("7"),
    )
    command = controller.ForceExport(target_energy_kwh=Decimal("8"))
    return Snapshot(
        heartbeat_at=NOW,
        diagnostic_energy_kwh=Decimal("16"),
        recommendation=Decision(reserve=reserve, command=command),
        reserve=reserve,
        source_quality=("OBSERVATION_ONLY_LEGACY_POWER_LIMIT",),
        decision=Decision(reserve=reserve, command=command),
        battery_spec=spec,
        battery_state=battery.State(energy_kwh=Decimal("16")),
        input_interval=planner.InputInterval(
            interval=TimeInterval(NOW, END),
            load_kwh=Decimal("1.5"),
            solar_kwh=Decimal("0.5"),
            tariff=tariff.Tariff(
                import_price_per_kwh=Decimal("0.3"),
                export_price_per_kwh=Decimal("0.15"),
                import_price_is_off_peak=False,
            ),
        ),
        control=solis_cloud.Control(
            operating_mode=solis_cloud.OperatingMode.FORCE_EXPORT,
            target_state_of_charge_percent=Decimal("25"),
            power_w=Decimal("6000"),
        ),
        planning_horizon_end=END + timedelta(hours=23),
        tariff_forecast_end=END + timedelta(hours=23),
        load_forecast_end=END + timedelta(hours=23),
        solar_forecast_end=END + timedelta(hours=12),
    )


async def test_exposes_decision_and_diagnostic_context(
    hass: HomeAssistant,
) -> None:
    coordinator = Coordinator(hass, MagicMock())
    coordinator.async_set_updated_data(snapshot())
    hass.data[DOMAIN] = coordinator
    async_add_entities = MagicMock()

    await async_setup_platform(hass, {}, async_add_entities)

    entities = async_add_entities.call_args.args[0]
    assert [type(entity) for entity in entities] == [
        ControlSensor,
        HeartbeatSensor,
        HealthSensor,
        BatteryEnergySensor,
        ReserveTargetSensor,
        ReserveBalanceSensor,
    ]

    sensor = entities[0]
    assert isinstance(sensor, ControlSensor)
    assert sensor.native_value == "observation_only:force_export"
    assert sensor.available
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["observation_only"] is True
    assert attributes["battery_energy_kwh"] == 16.0
    assert attributes["reserve_start_energy_kwh"] == 8.0
    assert attributes["source_quality"] == ("OBSERVATION_ONLY_LEGACY_POWER_LIMIT",)
    assert entities[1].native_value == NOW
    assert entities[2].native_value == "degraded"


def test_exposes_battery_and_reserve_energy(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, MagicMock())
    coordinator.async_set_updated_data(snapshot())

    energy = BatteryEnergySensor(coordinator)
    reserve_target = ReserveTargetSensor(coordinator)
    reserve_balance = ReserveBalanceSensor(coordinator)

    assert energy.native_value == 16.0
    assert reserve_target.native_value == 8.0
    assert reserve_balance.native_value == 8.0
    for sensor in (energy, reserve_target, reserve_balance):
        assert sensor.available
        assert sensor.device_class is SensorDeviceClass.ENERGY
        assert sensor.native_unit_of_measurement is UnitOfEnergy.KILO_WATT_HOUR
        assert sensor.suggested_display_precision == 2
        assert sensor.state_class is SensorStateClass.MEASUREMENT


def test_reserve_balance_reports_a_shortfall_as_negative(
    hass: HomeAssistant,
) -> None:
    coordinator = Coordinator(hass, MagicMock())
    coordinator.async_set_updated_data(
        replace(
            snapshot(),
            diagnostic_energy_kwh=Decimal("6"),
        )
    )

    assert ReserveBalanceSensor(coordinator).native_value == -2.0


def test_unavailable_without_a_successful_decision(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, MagicMock())
    sensors = (
        ControlSensor(coordinator),
        BatteryEnergySensor(coordinator),
        ReserveTargetSensor(coordinator),
        ReserveBalanceSensor(coordinator),
    )

    for sensor in sensors:
        assert not sensor.available
        assert sensor.native_value is None
    assert sensors[0].extra_state_attributes is None

    coordinator.async_set_updated_data(snapshot())
    coordinator.last_update_success = False

    assert all(not sensor.available for sensor in sensors)


def test_partial_sensor_availability_is_source_specific(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, MagicMock())
    coordinator.async_set_updated_data(
        Snapshot(
            heartbeat_at=NOW,
            diagnostic_energy_kwh=Decimal("12"),
            issues=("planner_input_invalid",),
        )
    )
    assert HeartbeatSensor(coordinator).available
    assert HealthSensor(coordinator).available
    assert BatteryEnergySensor(coordinator).native_value == 12.0
    assert BatteryEnergySensor(coordinator).available
    assert ReserveTargetSensor(coordinator).native_value is None
    assert not ReserveTargetSensor(coordinator).available
    assert ReserveBalanceSensor(coordinator).native_value is None
    assert not ReserveBalanceSensor(coordinator).available

    coordinator.async_set_updated_data(
        replace(snapshot(), diagnostic_energy_kwh=None)
    )
    assert not BatteryEnergySensor(coordinator).available
    assert ReserveTargetSensor(coordinator).available
    assert not ReserveBalanceSensor(coordinator).available
