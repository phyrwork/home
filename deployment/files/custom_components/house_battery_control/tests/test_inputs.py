from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control import inputs, planner

NOW = datetime(2026, 7, 4, 10, tzinfo=UTC)
SOC_ENTITY_ID = "input_number.house_battery_state_of_charge"
POWER_ENTITY_ID = "input_number.house_battery_power_limit"
IMPORT_ENTITY_ID = "sensor.import_price"
EXPORT_ENTITY_ID = "sensor.export_price"


def config() -> integration_config.Config:
    return integration_config.from_mapping(
        {
            "battery": {
                "capacity_kwh": 32.1536,
                "minimum_state_of_charge_percent": 10,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.95,
                "state_of_charge_entity_id": SOC_ENTITY_ID,
                "power_limit_entity_id": POWER_ENTITY_ID,
            },
            "tariff": {
                "import_price_entity_id": IMPORT_ENTITY_ID,
                "export_price_entity_id": EXPORT_ENTITY_ID,
            },
            "solar": {"config_entry_id": "forecast-solar-entry"},
            "policy": {
                "reserve_margin_entity_id": "input_number.reserve_margin",
                "export_hysteresis_entity_id": "input_number.export_hysteresis",
            },
            "inverter": {
                "operating_mode_entity_id": "input_select.operating_mode",
                "state_of_charge_target_entity_id": "input_number.soc_target",
            },
        }
    )


def set_valid_states(hass: HomeAssistant) -> None:
    hass.states.async_set(SOC_ENTITY_ID, "50")
    hass.states.async_set(POWER_ENTITY_ID, "6")
    hass.states.async_set(EXPORT_ENTITY_ID, "0.12")
    hass.states.async_set(
        IMPORT_ENTITY_ID,
        "0.30",
        {
            "rates": [
                {
                    "start": NOW.isoformat(),
                    "end": (NOW + timedelta(hours=1)).isoformat(),
                    "value_inc_vat": 0.30,
                },
                {
                    "start": (NOW + timedelta(hours=1)).isoformat(),
                    "end": (NOW + timedelta(hours=2)).isoformat(),
                    "value_inc_vat": 0.07,
                },
            ]
        },
    )


def solar_forecast() -> dict[str, dict[str, int]]:
    return {
        "wh_hours": {
            NOW.isoformat(): 500,
            (NOW + timedelta(hours=1)).isoformat(): 750,
            (NOW + timedelta(hours=2)).isoformat(): 0,
        }
    }


async def test_reads_planner_input(hass: HomeAssistant) -> None:
    set_valid_states(hass)

    with patch.object(
        inputs,
        "_async_get_solar_forecast",
        return_value=solar_forecast(),
    ):
        result = await inputs.async_read_input(hass, config(), now=NOW)

    assert result.battery_spec.capacity_kwh == Decimal("32.1536")
    assert result.battery_spec.minimum_energy_kwh == Decimal("3.21536")
    assert result.battery_spec.maximum_charge_power_kw == Decimal("6")
    assert result.battery_state.energy_kwh == Decimal("16.07680")
    assert result.tariff_forecast[0].tariff.import_price_per_kwh == Decimal("0.3")
    assert result.tariff_forecast[1].tariff.import_price_is_off_peak
    assert result.load_forecast[-1].interval.end >= NOW + timedelta(hours=2)
    assert result.solar_forecast[0].energy_kwh == Decimal("0.5")
    assert planner.fuse_forecasts(
        now=result.now,
        tariff_forecast=result.tariff_forecast,
        load_forecast=result.load_forecast,
        solar_forecast=result.solar_forecast,
    )


async def test_rejects_missing_required_entity(hass: HomeAssistant) -> None:
    set_valid_states(hass)
    hass.states.async_remove(POWER_ENTITY_ID)

    with pytest.raises(ValueError, match=f"does not exist: {POWER_ENTITY_ID}"):
        await inputs.async_read_input(hass, config(), now=NOW)


async def test_rejects_unavailable_solar_entry(hass: HomeAssistant) -> None:
    set_valid_states(hass)

    with (
        patch.object(inputs, "_async_get_solar_forecast", return_value=None),
        pytest.raises(ValueError, match="config entry is unavailable"),
    ):
        await inputs.async_read_input(hass, config(), now=NOW)


async def test_rejects_non_finite_state(hass: HomeAssistant) -> None:
    set_valid_states(hass)
    hass.states.async_set(POWER_ENTITY_ID, "NaN")

    with pytest.raises(ValueError, match=f"is not finite: {POWER_ENTITY_ID}"):
        await inputs.async_read_input(hass, config(), now=NOW)
