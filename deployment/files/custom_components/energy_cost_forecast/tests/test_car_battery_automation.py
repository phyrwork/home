from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

AUTOMATION_ID = "car_battery_start_charge_at_best_time"
AUTOMATIONS_PATH = (
    Path(__file__).resolve().parents[4] / "files/automations/car_battery.yaml"
)


def _load_automation():
    automations = yaml.safe_load(AUTOMATIONS_PATH.read_text())
    return next(item for item in automations if item.get("id") == AUTOMATION_ID)


async def _setup_automation(hass):
    automation = _load_automation()
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()


def _set_base_states(hass, start_time, latest_end):
    hass.states.async_set("switch.ev_charger", "off")
    hass.states.async_set("sensor.car_battery_energy_needed", "10")
    hass.states.async_set(
        "sensor.car_battery_target_cost_next_lowest_start_cost_time",
        start_time.isoformat(),
    )
    hass.states.async_set(
        "sensor.octopus_energy_electricity_21l4421345_2700007165105_fused_day_rates",
        "0.1",
        {"rates": [{"end": latest_end.isoformat()}]},
    )


def _freeze_time(hass, freezer, when):
    tz = dt_util.get_time_zone("UTC")
    hass.config.time_zone = "UTC"
    dt_util.set_default_time_zone(tz)
    freezer.move_to(when)
    async_fire_time_changed(hass, when)


@pytest.mark.asyncio
async def test_start_at_next_lowest_start_when_tomorrow_rates_available(hass, freezer):
    await _setup_automation(hass)
    service_calls = async_mock_service(hass, "homeassistant", "turn_on")

    now = dt_util.parse_datetime("2026-01-25T06:00:00+00:00")
    start_time = now - timedelta(minutes=5)
    latest_end = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
    _set_base_states(hass, start_time, latest_end)

    _freeze_time(hass, freezer, now)
    await hass.async_block_till_done()

    assert len(service_calls) == 1
    assert service_calls[0].data["entity_id"] == ["switch.ev_charger"]


@pytest.mark.asyncio
async def test_daytime_gate_blocks_without_tomorrow_rates(hass, freezer):
    await _setup_automation(hass)
    service_calls = async_mock_service(hass, "homeassistant", "turn_on")

    now = dt_util.parse_datetime("2026-01-25T10:00:00+00:00")
    start_time = now - timedelta(minutes=5)
    latest_end = now.replace(hour=0, minute=30, second=0, microsecond=0)
    _set_base_states(hass, start_time, latest_end)

    _freeze_time(hass, freezer, now)
    await hass.async_block_till_done()

    assert service_calls == []
