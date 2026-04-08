import pytest
import pytz
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_cost_saving_tracker.const import (
    CONF_BASELINE_RATE_SENSOR,
    CONF_CURRENT_RATE_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_POWER_SENSOR,
    DOMAIN,
    STATE_BASELINE_RATE,
    STATE_CURRENT_RATE,
    STATE_CURRENT_SAVINGS_RATE,
    STATE_LAST_TOTAL_ENERGY,
    STATE_TOTAL_ENERGY,
    STATE_TOTAL_SAVINGS,
)
from custom_components.energy_cost_saving_tracker.coordinator import (
    EnergyCostSavingTrackerCoordinator,
)


@pytest.mark.asyncio
async def test_energy_updates_mirror_rates_and_accumulate(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENERGY_SENSOR: "sensor.test_energy",
            CONF_CURRENT_RATE_SENSOR: "sensor.current_rate",
            CONF_BASELINE_RATE_SENSOR: "sensor.baseline_rate",
        },
        title="Test Tracker",
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.current_rate", "0.07")
    hass.states.async_set("sensor.baseline_rate", "0.29")
    hass.states.async_set("sensor.test_energy", "10")

    coordinator = EnergyCostSavingTrackerCoordinator(hass, entry)
    await coordinator.async_initialize()

    hass.states.async_set("sensor.test_energy", "11")
    await coordinator._async_handle_source_update({"data": {"entity_id": "sensor.test_energy"}})

    assert coordinator.data[STATE_CURRENT_RATE] == 0.07
    assert coordinator.data[STATE_BASELINE_RATE] == 0.29
    assert coordinator.data[STATE_CURRENT_SAVINGS_RATE] == pytest.approx(0.22)
    assert coordinator.data[STATE_TOTAL_ENERGY] == pytest.approx(1.0)
    assert coordinator.data[STATE_TOTAL_SAVINGS] == pytest.approx(0.22)
    assert coordinator.data[STATE_LAST_TOTAL_ENERGY] == pytest.approx(11.0)


@pytest.mark.asyncio
async def test_energy_update_skips_accumulation_when_rate_missing(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENERGY_SENSOR: "sensor.test_energy",
            CONF_CURRENT_RATE_SENSOR: "sensor.current_rate",
            CONF_BASELINE_RATE_SENSOR: "sensor.baseline_rate",
        },
        title="Test Tracker",
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.current_rate", "unknown")
    hass.states.async_set("sensor.baseline_rate", "0.29")
    hass.states.async_set("sensor.test_energy", "10")

    coordinator = EnergyCostSavingTrackerCoordinator(hass, entry)
    await coordinator.async_initialize()

    hass.states.async_set("sensor.test_energy", "11")
    await coordinator._async_handle_source_update({"data": {"entity_id": "sensor.test_energy"}})

    assert coordinator.data[STATE_TOTAL_ENERGY] == 0.0
    assert coordinator.data[STATE_TOTAL_SAVINGS] == 0.0
    assert coordinator.data[STATE_LAST_TOTAL_ENERGY] == pytest.approx(11.0)


@pytest.mark.asyncio
async def test_power_tracker_uses_existing_internal_rate_before_rate_change(hass, monkeypatch):
    dt_util.set_default_time_zone(pytz.UTC)
    first = dt_util.parse_datetime("2024-01-01T00:00:00+00:00")
    second = dt_util.parse_datetime("2024-01-01T00:30:00+00:00")
    times = iter([first, second])
    monkeypatch.setattr(dt_util, "utcnow", lambda: next(times))

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POWER_SENSOR: "sensor.test_power",
            CONF_CURRENT_RATE_SENSOR: "sensor.current_rate",
            CONF_BASELINE_RATE_SENSOR: "sensor.baseline_rate",
        },
        title="Test Tracker",
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.current_rate", "0.07")
    hass.states.async_set("sensor.baseline_rate", "0.29")
    hass.states.async_set("sensor.test_power", "1000")

    coordinator = EnergyCostSavingTrackerCoordinator(hass, entry)
    await coordinator.async_initialize()

    hass.states.async_set("sensor.current_rate", "0.10")
    await coordinator._async_handle_source_update({"data": {"entity_id": "sensor.current_rate"}})

    assert coordinator.data[STATE_TOTAL_ENERGY] == pytest.approx(0.5)
    assert coordinator.data[STATE_TOTAL_SAVINGS] == pytest.approx(0.11)
    assert coordinator.data[STATE_CURRENT_RATE] == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_negative_savings_are_accumulated(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENERGY_SENSOR: "sensor.test_energy",
            CONF_CURRENT_RATE_SENSOR: "sensor.current_rate",
            CONF_BASELINE_RATE_SENSOR: "sensor.baseline_rate",
        },
        title="Test Tracker",
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.current_rate", "0.40")
    hass.states.async_set("sensor.baseline_rate", "0.20")
    hass.states.async_set("sensor.test_energy", "1")

    coordinator = EnergyCostSavingTrackerCoordinator(hass, entry)
    await coordinator.async_initialize()

    hass.states.async_set("sensor.test_energy", "2")
    await coordinator._async_handle_source_update({"data": {"entity_id": "sensor.test_energy"}})

    assert coordinator.data[STATE_CURRENT_SAVINGS_RATE] == pytest.approx(-0.20)
    assert coordinator.data[STATE_TOTAL_SAVINGS] == pytest.approx(-0.20)
