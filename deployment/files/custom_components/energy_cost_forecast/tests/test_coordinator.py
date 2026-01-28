from datetime import timedelta

import pytest
import pytz
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_cost_forecast.const import (
    CONF_IMPORT_RATE_SENSOR,
    CONF_PROFILE,
    CONF_START_AFTER,
    CONF_START_BEFORE,
    CONF_UPDATE_INTERVAL_MINUTES,
    DATA_START_STEP_MINUTES,
    DATA_START_STEP_MODE,
    DATA_TARGET_PERCENTILE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DEFAULT_START_MODE,
    DOMAIN,
)
from custom_components.energy_cost_forecast.coordinator import EnergyCostForecastCoordinator


def _set_rates(hass, entity_id, rates):
    hass.states.async_set(
        entity_id,
        "0.1",
        {
            "rates": rates,
            "unit_of_measurement": "GBP/kWh",
        },
    )


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


@pytest.mark.asyncio
async def test_time_filters_apply_before_percentile(hass, monkeypatch):
    dt_util.set_default_time_zone(pytz.UTC)
    fixed = dt_util.parse_datetime("2024-01-01T00:00:00+00:00")
    monkeypatch.setattr(dt_util, "now", lambda: dt_util.as_local(fixed))

    rates = [
        {
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-01T00:30:00+00:00",
            "value": 0.1,
        },
        {
            "start": "2024-01-01T00:30:00+00:00",
            "end": "2024-01-01T01:00:00+00:00",
            "value": 0.2,
        },
        {
            "start": "2024-01-01T01:00:00+00:00",
            "end": "2024-01-01T01:30:00+00:00",
            "value": 0.4,
        },
        {
            "start": "2024-01-01T01:30:00+00:00",
            "end": "2024-01-01T02:00:00+00:00",
            "value": 0.6,
        },
    ]
    _set_rates(hass, "sensor.test_import", rates)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_IMPORT_RATE_SENSOR: "sensor.test_import",
            CONF_PROFILE: [[1000, "30m"]],
            CONF_START_AFTER: "2024-01-01T00:15:00",
            CONF_START_BEFORE: "2024-01-01T01:15:00",
        },
    )
    entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        entry.entry_id: {
            DATA_TARGET_PERCENTILE: 100.0,
            DATA_START_STEP_MINUTES: 30,
            DATA_START_STEP_MODE: DEFAULT_START_MODE,
        }
    }

    coordinator = EnergyCostForecastCoordinator(hass, entry)
    data = await coordinator._async_update_data()

    assert dt_util.parse_datetime(data["max_percentile_time"]["start"]) == dt_util.as_local(
        dt_util.parse_datetime("2024-01-01T00:30:00+00:00")
    )
    assert dt_util.parse_datetime(data["latest_percentile_time"]["start"]) == dt_util.as_local(
        dt_util.parse_datetime("2024-01-01T01:00:00+00:00")
    )
    assert data["min"] == 0.1
    assert data["max"] == 0.2


@pytest.mark.asyncio
async def test_latest_start_with_percentile(hass, monkeypatch):
    dt_util.set_default_time_zone(pytz.UTC)
    fixed = dt_util.parse_datetime("2024-01-01T00:00:00+00:00")
    monkeypatch.setattr(dt_util, "now", lambda: dt_util.as_local(fixed))

    rates = [
        {
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-01T00:30:00+00:00",
            "value": 0.1,
        },
        {
            "start": "2024-01-01T00:30:00+00:00",
            "end": "2024-01-01T01:00:00+00:00",
            "value": 0.2,
        },
        {
            "start": "2024-01-01T01:00:00+00:00",
            "end": "2024-01-01T01:30:00+00:00",
            "value": 0.3,
        },
        {
            "start": "2024-01-01T01:30:00+00:00",
            "end": "2024-01-01T02:00:00+00:00",
            "value": 0.4,
        },
    ]
    _set_rates(hass, "sensor.test_import", rates)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_IMPORT_RATE_SENSOR: "sensor.test_import",
            CONF_PROFILE: [[1000, "30m"]],
        },
    )
    entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        entry.entry_id: {
            DATA_TARGET_PERCENTILE: 50.0,
            DATA_START_STEP_MINUTES: 30,
            DATA_START_STEP_MODE: DEFAULT_START_MODE,
        }
    }

    coordinator = EnergyCostForecastCoordinator(hass, entry)
    data = await coordinator._async_update_data()

    assert dt_util.parse_datetime(data["max_percentile_time"]["start"]) == dt_util.as_local(
        dt_util.parse_datetime("2024-01-01T00:00:00+00:00")
    )
    assert dt_util.parse_datetime(data["latest_percentile_time"]["start"]) == dt_util.as_local(
        dt_util.parse_datetime("2024-01-01T00:30:00+00:00")
    )
    assert data["min"] == 0.05
    assert data["max"] == 0.1
