from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_IMPORT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_cost_saving_tracker import DOMAIN, async_setup


async def test_yaml_rename_updates_existing_entry_and_renames_entities(hass, monkeypatch):
    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        source=SOURCE_IMPORT,
        unique_id="test_ev_charger",
        title="Test EV Charger",
        data={
            "power_sensor": "sensor.ev_charger_energy_meter_power",
            "active_power_threshold": 100.0,
            "current_rate_sensor": "sensor.current_rate",
            "baseline_rate_sensor": "sensor.baseline_rate",
            "name": "Test EV Charger",
            "power_integration_method": "left",
        },
    )
    existing_entry.add_to_hass(hass)

    rename_entry_entities = []
    monkeypatch.setattr(
        "custom_components.energy_cost_saving_tracker._rename_entry_entities",
        lambda hass, entry_id, old_title, new_title: rename_entry_entities.append(
            (entry_id, old_title, new_title)
        ),
    )

    updated_entries = []
    original_update_entry = hass.config_entries.async_update_entry

    def record_update(entry, **kwargs):
        updated_entries.append((entry, kwargs))
        return original_update_entry(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", record_update)
    remove_entry = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_remove", remove_entry)
    flow_init = AsyncMock()
    monkeypatch.setattr(hass.config_entries.flow, "async_init", flow_init)

    assert await async_setup(
        hass,
        {
            DOMAIN: [
                {
                    "name": "EV Charger",
                    "power_sensor": "sensor.ev_charger_energy_meter_power",
                    "active_power_threshold": 100.0,
                    "current_rate_sensor": "sensor.current_rate",
                    "baseline_rate_sensor": "sensor.baseline_rate",
                    "power_integration_method": "left",
                }
            ]
        },
    )

    assert len(updated_entries) == 1
    updated_entry, kwargs = updated_entries[0]
    assert updated_entry.entry_id == existing_entry.entry_id
    assert kwargs["title"] == "EV Charger"
    assert kwargs["unique_id"] == "ev_charger"
    assert rename_entry_entities == [
        (existing_entry.entry_id, "Test EV Charger", "EV Charger")
    ]
    flow_init.assert_not_awaited()
    remove_entry.assert_not_awaited()
