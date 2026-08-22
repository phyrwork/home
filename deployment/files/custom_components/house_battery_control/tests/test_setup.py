from decimal import Decimal
import asyncio
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


async def test_delayed_workflow_owner_finalizes_stop_once(
    hass: HomeAssistant,
) -> None:
    instance = coordinator()
    with patch(
        "custom_components.house_battery_control.Coordinator",
        return_value=instance,
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    workflow = hass.data[f"{DOMAIN}.commissioning"]
    release = asyncio.Event()

    async def owner() -> None:
        await release.wait()

    owner_task = asyncio.create_task(owner())

    async def delayed_stop() -> None:
        workflow._cleanup_task = owner_task

    workflow.async_stop = delayed_stop
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    instance.async_stop.assert_not_awaited()
    assert hass.data[f"{DOMAIN}.commissioning"] is workflow

    release.set()
    await hass.async_block_till_done()
    await asyncio.sleep(0)
    await hass.async_block_till_done()
    instance.async_stop.assert_awaited_once_with()
    assert DOMAIN not in hass.data
    assert f"{DOMAIN}.commissioning" not in hass.data


async def test_finalizer_failure_is_retryable(
    hass: HomeAssistant,
) -> None:
    instance = coordinator()
    with patch(
        "custom_components.house_battery_control.Coordinator",
        return_value=instance,
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    instance.async_stop.side_effect = RuntimeError("stop failed")
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert hass.data[f"{DOMAIN}.stop_finalizer"]["result"]["ok"] is False
    assert hass.data[f"{DOMAIN}.commissioning"] is not None

    instance.async_stop.side_effect = None
    retry = hass.data[f"{DOMAIN}.stop_finalizer"]["await_result"]
    assert await retry()
    assert instance.async_stop.await_count == 2
    assert DOMAIN not in hass.data


async def test_reload_awaits_same_delayed_finalizer_without_double_stop(
    hass: HomeAssistant,
) -> None:
    instance = coordinator()
    with patch(
        "custom_components.house_battery_control.Coordinator",
        return_value=instance,
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    release = asyncio.Event()

    async def delayed_coordinator_stop() -> None:
        await release.wait()

    instance.async_stop.side_effect = delayed_coordinator_stop
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await asyncio.sleep(0)
    reload_task = asyncio.create_task(async_setup(hass, {DOMAIN: config()}))
    await asyncio.sleep(0)
    assert not reload_task.done()
    release.set()
    assert await reload_task
    assert instance.async_stop.await_count == 1


async def test_setup_without_typed_configuration_is_no_op(
    hass: HomeAssistant,
) -> None:
    assert await async_setup(hass, {})

    assert DOMAIN not in hass.data
