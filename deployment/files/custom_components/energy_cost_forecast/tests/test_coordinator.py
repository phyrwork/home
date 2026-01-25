from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_cost_forecast.const import (
    CONF_IMPORT_RATE_SENSOR,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from custom_components.energy_cost_forecast.coordinator import EnergyCostForecastCoordinator


@pytest.mark.asyncio
async def test_update_interval_default(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_IMPORT_RATE_SENSOR: "sensor.test_import"},
    )
    entry.add_to_hass(hass)

    coordinator = EnergyCostForecastCoordinator(hass, entry)
    assert coordinator.update_interval == timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)


@pytest.mark.asyncio
async def test_update_interval_disabled(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_IMPORT_RATE_SENSOR: "sensor.test_import",
            CONF_UPDATE_INTERVAL_MINUTES: 0,
        },
    )
    entry.add_to_hass(hass)

    coordinator = EnergyCostForecastCoordinator(hass, entry)
    assert coordinator.update_interval is None
