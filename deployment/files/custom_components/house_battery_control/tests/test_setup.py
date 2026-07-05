from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, HomeAssistant

from custom_components.house_battery_control import async_setup
from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.const import DOMAIN
from custom_components.house_battery_control.coordinator import Coordinator


def config() -> integration_config.Config:
    return integration_config.Config(
        battery=integration_config.BatteryConfig(
            capacity_kwh=Decimal("32"),
            minimum_state_of_charge_percent=Decimal("10"),
            charge_efficiency=Decimal("0.95"),
            discharge_efficiency=Decimal("0.95"),
            state_of_charge_entity_id="input_number.soc",
            power_limit_entity_id="input_number.power_limit",
        ),
        tariff=integration_config.TariffConfig(
            import_price_entity_id="sensor.import_price",
            export_price_entity_id="sensor.export_price",
        ),
        solar=integration_config.SolarConfig(
            config_entry_id="forecast-solar-entry",
        ),
        policy=integration_config.PolicyConfig(
            reserve_margin_entity_id="input_number.reserve_margin",
            export_hysteresis_entity_id="input_number.export_hysteresis",
        ),
        inverter=integration_config.InverterConfig(
            operating_mode_entity_id="input_select.operating_mode",
            state_of_charge_target_entity_id="input_number.soc_target",
        ),
    )


def coordinator() -> MagicMock:
    result = MagicMock(spec=Coordinator)
    result.async_start = AsyncMock()
    result.async_stop = AsyncMock()
    return result


async def test_starts_immediately_when_home_assistant_is_running(
    hass: HomeAssistant,
) -> None:
    instance = coordinator()

    with patch(
        "custom_components.house_battery_control.Coordinator",
        return_value=instance,
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    assert hass.data[DOMAIN] is instance
    instance.async_start.assert_awaited_once_with()


async def test_loads_diagnostic_sensor_platform(hass: HomeAssistant) -> None:
    instance = coordinator()
    load_platform = AsyncMock()
    source = {DOMAIN: config()}

    with (
        patch(
            "custom_components.house_battery_control.Coordinator",
            return_value=instance,
        ),
        patch(
            "custom_components.house_battery_control.async_load_platform",
            load_platform,
        ),
    ):
        assert await async_setup(hass, source)

    load_platform.assert_awaited_once_with(
        hass,
        "sensor",
        DOMAIN,
        {},
        source,
    )


async def test_defers_start_until_home_assistant_started(
    hass: HomeAssistant,
) -> None:
    hass.set_state(CoreState.starting)
    instance = coordinator()

    with patch(
        "custom_components.house_battery_control.Coordinator",
        return_value=instance,
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    instance.async_start.assert_not_awaited()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    instance.async_start.assert_awaited_once_with()


async def test_stops_coordinator_on_home_assistant_stop(
    hass: HomeAssistant,
) -> None:
    instance = coordinator()

    with patch(
        "custom_components.house_battery_control.Coordinator",
        return_value=instance,
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    instance.async_stop.assert_awaited_once_with()


async def test_setup_without_typed_configuration_is_no_op(
    hass: HomeAssistant,
) -> None:
    assert await async_setup(hass, {})

    assert DOMAIN not in hass.data
