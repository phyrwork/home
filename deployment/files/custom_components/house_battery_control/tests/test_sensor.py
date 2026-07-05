from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

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
    ControlSensor,
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

    sensor = async_add_entities.call_args.args[0][0]
    assert isinstance(sensor, ControlSensor)
    assert sensor.native_value == "force_export"
    assert sensor.available
    assert sensor.extra_state_attributes == {
        "battery_energy_kwh": 16.0,
        "battery_state_of_charge_percent": 50.0,
        "load_kwh": 1.5,
        "solar_kwh": 0.5,
        "net_load_kwh": 1.0,
        "import_price_per_kwh": 0.3,
        "export_price_per_kwh": 0.15,
        "import_price_is_off_peak": False,
        "reserve_start_energy_kwh": 8.0,
        "reserve_end_energy_kwh": 7.0,
        "command_target_energy_kwh": 8.0,
        "target_state_of_charge_percent": 25.0,
        "power_w": 6000.0,
        "expires_at": END.isoformat(),
        "planning_horizon_end": (END + timedelta(hours=23)).isoformat(),
        "tariff_forecast_end": (END + timedelta(hours=23)).isoformat(),
        "load_forecast_end": (END + timedelta(hours=23)).isoformat(),
        "solar_forecast_end": (END + timedelta(hours=12)).isoformat(),
    }


def test_unavailable_without_a_successful_decision(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, MagicMock())
    sensor = ControlSensor(coordinator)

    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None

    coordinator.async_set_updated_data(snapshot())
    coordinator.last_update_success = False

    assert not sensor.available
