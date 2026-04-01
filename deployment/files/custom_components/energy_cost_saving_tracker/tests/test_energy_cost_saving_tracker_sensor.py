from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_cost_saving_tracker.const import (
    DOMAIN,
    STATE_LAST_UPDATED_TIME,
)
from custom_components.energy_cost_saving_tracker.coordinator import (
    EnergyCostSavingTrackerCoordinator,
)
from custom_components.energy_cost_saving_tracker.sensor import (
    EnergyCostSavingTrackerSensor,
    SENSORS,
)


async def test_timestamp_sensor_parses_last_updated_time(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="Test Tracker")
    entry.add_to_hass(hass)

    coordinator = EnergyCostSavingTrackerCoordinator(hass, entry)
    coordinator.data[STATE_LAST_UPDATED_TIME] = "2024-01-01T00:00:00+00:00"

    description = next(item for item in SENSORS if item.key == STATE_LAST_UPDATED_TIME)
    sensor = EnergyCostSavingTrackerSensor(
        coordinator,
        entry,
        description,
        DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Test Tracker"),
    )

    assert sensor.native_value == dt_util.parse_datetime("2024-01-01T00:00:00+00:00")
