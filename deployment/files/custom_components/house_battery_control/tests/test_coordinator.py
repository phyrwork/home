from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import (
    battery,
    config,
    controller,
    energy,
    planner,
    tariff,
)
from custom_components.house_battery_control import coordinator as coordinator_module
from custom_components.house_battery_control.dependencies import (
    solis_cloud,
    stub_inverter,
)
from custom_components.house_battery_control.interval import TimeInterval

NOW = datetime(2026, 7, 4, 10, tzinfo=UTC)
END = NOW + timedelta(hours=1)

RESERVE_MARGIN_ENTITY_ID = "input_number.reserve_margin"
EXPORT_HYSTERESIS_ENTITY_ID = "input_number.export_hysteresis"


def integration_config() -> config.Config:
    return config.from_mapping(
        {
            "battery": {
                "capacity_kwh": 32,
                "minimum_state_of_charge_percent": 10,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.95,
                "state_of_charge_entity_id": "input_number.soc",
                "power_limit_entity_id": "input_number.power_limit",
            },
            "tariff": {
                "import_price_entity_id": "sensor.import_price",
                "export_price_entity_id": "sensor.export_price",
            },
            "solar": {"config_entry_id": "forecast-solar-entry"},
            "policy": {
                "reserve_margin_entity_id": RESERVE_MARGIN_ENTITY_ID,
                "export_hysteresis_entity_id": EXPORT_HYSTERESIS_ENTITY_ID,
            },
            "inverter": {
                "operating_mode_entity_id": "input_select.operating_mode",
                "state_of_charge_target_entity_id": "input_number.soc_target",
            },
        }
    )


def source(*, energy_kwh: Decimal = Decimal("20")) -> planner.Input:
    interval = TimeInterval(NOW, END)
    return planner.Input(
        now=NOW,
        battery_spec=battery.Spec(
            capacity_kwh=Decimal("32"),
            minimum_energy_kwh=Decimal("3.2"),
            maximum_charge_power_kw=Decimal("6"),
            maximum_discharge_power_kw=Decimal("6"),
            charge_efficiency=Decimal("0.95"),
            discharge_efficiency=Decimal("0.95"),
        ),
        battery_state=battery.State(energy_kwh=energy_kwh),
        tariff_forecast=(
            tariff.TariffInterval(
                interval=interval,
                tariff=tariff.Tariff(
                    import_price_per_kwh=Decimal("0.3"),
                    export_price_per_kwh=Decimal("0.15"),
                    import_price_is_off_peak=False,
                ),
            ),
        ),
        load_forecast=(
            energy.EnergyInterval(
                interval=interval,
                energy_kwh=Decimal("1"),
            ),
        ),
        solar_forecast=(
            energy.EnergyInterval(
                interval=interval,
                energy_kwh=Decimal(),
            ),
        ),
    )


def set_policy_states(hass: HomeAssistant) -> None:
    hass.states.async_set(RESERVE_MARGIN_ENTITY_ID, "2")
    hass.states.async_set(EXPORT_HYSTERESIS_ENTITY_ID, "1")


async def test_calculates_applies_and_schedules_decision(
    hass: HomeAssistant,
) -> None:
    set_policy_states(hass)
    apply = AsyncMock()
    cancel_timer = MagicMock()

    with (
        patch.object(
            coordinator_module.inputs,
            "async_read_input",
            AsyncMock(return_value=source()),
        ),
        patch.object(stub_inverter, "async_apply", apply),
        patch.object(
            coordinator_module,
            "async_track_point_in_utc_time",
            return_value=cancel_timer,
        ) as track_timer,
        patch.object(coordinator_module.dt_util, "now", return_value=NOW),
    ):
        coordinator = coordinator_module.Coordinator(hass, integration_config())
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert isinstance(
        coordinator.data.decision.command,
        controller.ForceExport,
    )
    assert coordinator.data.decision.reserve.interval == TimeInterval(NOW, END)
    assert coordinator.data.input_interval.load_kwh == Decimal("1")
    assert coordinator.data.planning_horizon_end == END
    apply.assert_awaited_once_with(
        hass,
        coordinator.config.inverter,
        solis_cloud.Control(
            operating_mode=solis_cloud.OperatingMode.FORCE_EXPORT,
            target_state_of_charge_percent=Decimal("20"),
            power_w=Decimal("6000"),
        ),
    )
    track_timer.assert_called_once_with(
        hass,
        coordinator._async_timer_elapsed,
        END,
    )


async def test_failure_applies_fallback_retries_and_clears_export_latch(
    hass: HomeAssistant,
) -> None:
    set_policy_states(hass)
    apply = AsyncMock()
    read = AsyncMock(
        side_effect=[
            source(),
            ValueError("forecast unavailable"),
            source(energy_kwh=Decimal("6.5")),
        ]
    )

    with (
        patch.object(coordinator_module.inputs, "async_read_input", read),
        patch.object(stub_inverter, "async_apply", apply),
        patch.object(
            coordinator_module,
            "async_track_point_in_utc_time",
            return_value=MagicMock(),
        ) as track_timer,
        patch.object(coordinator_module.dt_util, "now", return_value=NOW),
    ):
        coordinator = coordinator_module.Coordinator(hass, integration_config())
        await coordinator.async_refresh()
        await coordinator.async_refresh()

        assert not coordinator.last_update_success
        assert apply.await_args_list[-1].args[2] == solis_cloud.Control(
            operating_mode=solis_cloud.OperatingMode.SELF_CONSUMPTION,
            target_state_of_charge_percent=Decimal("10"),
            power_w=None,
        )
        assert track_timer.call_args.args[2] == NOW + timedelta(minutes=1)

        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert isinstance(
        coordinator.data.decision.command,
        controller.SelfConsumption,
    )


async def test_stop_applies_fallback(hass: HomeAssistant) -> None:
    apply = AsyncMock()
    coordinator = coordinator_module.Coordinator(hass, integration_config())

    with patch.object(stub_inverter, "async_apply", apply):
        await coordinator.async_stop()

    assert apply.await_args.args[2] == solis_cloud.Control(
        operating_mode=solis_cloud.OperatingMode.SELF_CONSUMPTION,
        target_state_of_charge_percent=Decimal("10"),
        power_w=None,
    )
