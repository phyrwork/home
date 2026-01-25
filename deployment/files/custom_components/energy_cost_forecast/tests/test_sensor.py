from datetime import datetime

import pytest
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_cost_forecast.const import DOMAIN
from custom_components.energy_cost_forecast.coordinator import EnergyCostForecastCoordinator
from custom_components.energy_cost_forecast.sensor import EnergyCostForecastSensor, SENSORS


@pytest.mark.asyncio
async def test_later_sensor_state_and_attributes(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    coordinator = EnergyCostForecastCoordinator(hass, entry)
    start_now = dt_util.as_utc(datetime(2024, 1, 1, 0, 0, 0)).isoformat()
    coordinator.data = {
        "start_now_time": start_now,
        "later": [
            {
                "start": "2024-01-01T00:00:00+00:00",
                "finish": "2024-01-01T01:00:00+00:00",
                "cost": 0.42,
            }
        ],
        "cost_unit": "GBP",
    }

    device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Test")
    later_description = next(item for item in SENSORS if item.key == "later")
    sensor = EnergyCostForecastSensor(coordinator, entry, later_description, device_info)

    assert sensor.native_value == dt_util.parse_datetime(start_now)
    attrs = sensor.extra_state_attributes
    assert "rates" not in attrs
    assert attrs.get("cost_unit") == "GBP"


@pytest.mark.asyncio
async def test_timestamp_sensors_parse_times(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    coordinator = EnergyCostForecastCoordinator(hass, entry)
    coordinator.data = {
        "min_time": {"start": "2024-01-02T03:04:05+00:00"},
        "max_percentile_time": {"start": "2024-01-02T05:06:07+00:00"},
    }

    device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Test")
    min_description = next(item for item in SENSORS if item.key == "min_time")
    max_description = next(item for item in SENSORS if item.key == "max_percentile_time")

    min_sensor = EnergyCostForecastSensor(coordinator, entry, min_description, device_info)
    max_sensor = EnergyCostForecastSensor(coordinator, entry, max_description, device_info)

    assert min_sensor.native_value == dt_util.parse_datetime("2024-01-02T03:04:05+00:00")
    assert max_sensor.native_value == dt_util.parse_datetime("2024-01-02T05:06:07+00:00")
