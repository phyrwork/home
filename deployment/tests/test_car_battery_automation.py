from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from jinja2 import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

AUTOMATION_ID = "car_battery_start_charge_at_best_time"
SYNC_AUTOMATION_ID = "car_battery_sync_soc_target_to_intelligent_delta"
AUTOMATIONS_PATH = (
    Path(__file__).resolve().parents[1] / "templates/automations/car_battery.yaml.j2"
)
TEST_EV_CHARGER_DEVICE_ID = "test_ev_charger_device_id"
DISPATCH_SENSOR_ID = (
    f"binary_sensor.octopus_energy_{TEST_EV_CHARGER_DEVICE_ID}_intelligent_dispatching"
)
TARGET_NUMBER_ID = (
    f"number.octopus_energy_{TEST_EV_CHARGER_DEVICE_ID}_intelligent_charge_target"
)
SMART_CHARGE_SWITCH_ID = (
    f"switch.octopus_energy_{TEST_EV_CHARGER_DEVICE_ID}_intelligent_smart_charge"
)


def _load_automation(automation_id=AUTOMATION_ID):
    rendered = Template(AUTOMATIONS_PATH.read_text()).render(
        ev_charger_device_id=TEST_EV_CHARGER_DEVICE_ID
    )
    automations = yaml.safe_load(rendered)
    return next(item for item in automations if item.get("id") == automation_id)


async def _setup_automation(hass, automation_id=AUTOMATION_ID):
    automation = _load_automation(automation_id=automation_id)
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()


def _set_base_states(hass, start_time, rates_attr):
    hass.states.async_set("switch.ev_charger", "off")
    hass.states.async_set("sensor.car_battery_energy_needed", "10")
    hass.states.async_set(
        "sensor.car_battery_target_cost_next_lowest_start_cost_time",
        start_time.isoformat(),
    )
    hass.states.async_set(
        "sensor.octopus_energy_electricity_21l4421345_2700007165105_fused_day_rates",
        "0.1",
        {"rates": rates_attr},
    )


def _freeze_time(hass, freezer, when):
    tz = dt_util.get_time_zone("UTC")
    hass.config.time_zone = "UTC"
    dt_util.set_default_time_zone(tz)
    freezer.move_to(when)
    async_fire_time_changed(hass, when)


def _set_sync_base_states(
    hass,
    *,
    target_level,
    current_level,
    applied_delta,
    charging,
    planned_dispatches,
    started_dispatches=None,
    charger_power=0,
    last_increase_update=None,
    smart_charge_on=True,
):
    hass.states.async_set("input_number.car_battery_level_target", str(target_level))
    hass.states.async_set("sensor.car_battery_level", str(current_level))
    hass.states.async_set(TARGET_NUMBER_ID, str(applied_delta))
    hass.states.async_set("binary_sensor.car_charging", "on" if charging else "off")
    hass.states.async_set("sensor.ev_charger_power", str(charger_power))
    hass.states.async_set(SMART_CHARGE_SWITCH_ID, "on" if smart_charge_on else "off")
    if last_increase_update is None:
        hass.states.async_set(
            "input_datetime.car_battery_intelligent_target_last_increase_update",
            "unknown",
        )
    else:
        hass.states.async_set(
            "input_datetime.car_battery_intelligent_target_last_increase_update",
            last_increase_update,
        )
    hass.states.async_set(
        DISPATCH_SENSOR_ID,
        "off",
        {
            "planned_dispatches": planned_dispatches,
            "started_dispatches": started_dispatches or [],
        },
    )


@pytest.mark.asyncio
async def test_start_at_next_lowest_start_when_tomorrow_rates_available(hass, freezer):
    await _setup_automation(hass)
    service_calls = async_mock_service(hass, "homeassistant", "turn_on")

    now = dt_util.parse_datetime("2026-01-25T06:00:00+00:00")
    start_time = now - timedelta(minutes=5)
    latest_end = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
    _set_base_states(hass, start_time, [{"end": latest_end.isoformat()}])

    _freeze_time(hass, freezer, now)
    await hass.async_block_till_done()

    assert len(service_calls) == 1
    assert service_calls[0].data["entity_id"] == ["switch.ev_charger"]


@pytest.mark.asyncio
async def test_sync_increase_is_rate_limited_for_15_minutes(hass, freezer):
    now = dt_util.parse_datetime("2026-01-25T10:00:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=70,
        current_level=50,
        applied_delta=20,
        charging=True,
        planned_dispatches=[],
        last_increase_update=now.isoformat(),
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "input_datetime", "set_datetime")

    _freeze_time(hass, freezer, now + timedelta(minutes=10))
    hass.states.async_set("input_number.car_battery_level_target", "80")
    await hass.async_block_till_done()

    assert service_calls == []


@pytest.mark.asyncio
async def test_sync_increase_allowed_after_15_minutes(hass, freezer):
    now = dt_util.parse_datetime("2026-01-25T10:00:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=70,
        current_level=50,
        applied_delta=20,
        charging=True,
        planned_dispatches=[],
        last_increase_update=now.isoformat(),
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "input_datetime", "set_datetime")

    _freeze_time(hass, freezer, now + timedelta(minutes=16))
    hass.states.async_set("input_number.car_battery_level_target", "80")
    await hass.async_block_till_done()

    assert len(service_calls) == 1
    assert service_calls[0].data["entity_id"] == [TARGET_NUMBER_ID]
    assert int(float(service_calls[0].data["value"])) == 30


@pytest.mark.asyncio
async def test_sync_decrease_blocked_during_planned_dispatch_span(hass, freezer):
    now = dt_util.parse_datetime("2026-01-25T09:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=95,
        current_level=80,
        applied_delta=20,
        charging=True,
        planned_dispatches=[
            {
                "start": "2026-01-25T09:00:00+00:00",
                "end": "2026-01-25T10:00:00+00:00",
            }
        ],
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set("input_number.car_battery_level_target", "90")
    await hass.async_block_till_done()

    assert service_calls == []


@pytest.mark.asyncio
async def test_sync_decrease_allowed_before_first_planned_dispatch(hass, freezer):
    now = dt_util.parse_datetime("2026-01-25T08:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=95,
        current_level=80,
        applied_delta=20,
        charging=True,
        planned_dispatches=[
            {
                "start": "2026-01-25T09:00:00+00:00",
                "end": "2026-01-25T10:00:00+00:00",
            }
        ],
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set("input_number.car_battery_level_target", "90")
    await hass.async_block_till_done()

    assert len(service_calls) == 1
    assert service_calls[0].data["entity_id"] == [TARGET_NUMBER_ID]
    assert int(float(service_calls[0].data["value"])) == 10


@pytest.mark.asyncio
async def test_sync_decrease_allowed_after_last_planned_dispatch(hass, freezer):
    now = dt_util.parse_datetime("2026-01-25T10:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=95,
        current_level=80,
        applied_delta=20,
        charging=True,
        planned_dispatches=[
            {
                "start": "2026-01-25T09:00:00+00:00",
                "end": "2026-01-25T10:00:00+00:00",
            }
        ],
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set("input_number.car_battery_level_target", "90")
    await hass.async_block_till_done()

    assert len(service_calls) == 1
    assert service_calls[0].data["entity_id"] == [TARGET_NUMBER_ID]
    assert int(float(service_calls[0].data["value"])) == 10


@pytest.mark.asyncio
async def test_sync_decrease_allowed_when_dispatches_explicitly_empty(
    hass, freezer
):
    now = dt_util.parse_datetime("2026-01-25T10:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=95,
        current_level=80,
        applied_delta=20,
        charging=True,
        planned_dispatches=[],
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set("input_number.car_battery_level_target", "90")
    await hass.async_block_till_done()

    assert len(service_calls) == 1
    assert service_calls[0].data["entity_id"] == [TARGET_NUMBER_ID]
    assert int(float(service_calls[0].data["value"])) == 10


@pytest.mark.asyncio
async def test_sync_decrease_blocked_when_dispatches_unavailable(
    hass, freezer
):
    now = dt_util.parse_datetime("2026-01-25T10:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=95,
        current_level=80,
        applied_delta=20,
        charging=False,
        planned_dispatches=[],
    )
    hass.states.async_set(DISPATCH_SENSOR_ID, "off", {})
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set("input_number.car_battery_level_target", "90")
    await hass.async_block_till_done()

    assert service_calls == []


@pytest.mark.asyncio
async def test_sync_decrease_blocked_when_dispatches_unknown(
    hass, freezer
):
    now = dt_util.parse_datetime("2026-01-25T10:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=95,
        current_level=80,
        applied_delta=20,
        charging=True,
        planned_dispatches=[],
    )
    hass.states.async_set(
        DISPATCH_SENSOR_ID,
        "off",
        {"planned_dispatches": "unknown"},
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set("input_number.car_battery_level_target", "90")
    await hass.async_block_till_done()

    assert service_calls == []


@pytest.mark.asyncio
async def test_sync_decrease_blocked_during_started_dispatch_when_planned_empty(
    hass, freezer
):
    now = dt_util.parse_datetime("2026-01-25T09:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=95,
        current_level=80,
        applied_delta=20,
        charging=True,
        planned_dispatches=[],
        started_dispatches=[
            {
                "start": "2026-01-25T09:00:00+00:00",
                "end": "2026-01-25T10:00:00+00:00",
            }
        ],
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    service_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set("input_number.car_battery_level_target", "90")
    await hass.async_block_till_done()

    assert service_calls == []


@pytest.mark.asyncio
async def test_sync_turns_off_smart_charge_when_required_delta_is_zero(hass, freezer):
    now = dt_util.parse_datetime("2026-01-25T10:30:00+00:00")
    _freeze_time(hass, freezer, now)
    _set_sync_base_states(
        hass,
        target_level=75,
        current_level=80,
        applied_delta=20,
        charging=False,
        planned_dispatches=[],
        smart_charge_on=True,
    )
    await _setup_automation(hass, automation_id=SYNC_AUTOMATION_ID)
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")

    hass.states.async_set("input_number.car_battery_level_target", "70")
    await hass.async_block_till_done()

    assert len(turn_off_calls) == 1
    assert turn_off_calls[0].data["entity_id"] == [SMART_CHARGE_SWITCH_ID]


@pytest.mark.asyncio
async def test_daytime_gate_blocks_without_tomorrow_rates(hass, freezer):
    await _setup_automation(hass)
    service_calls = async_mock_service(hass, "homeassistant", "turn_on")

    now = dt_util.parse_datetime("2026-01-25T10:00:00+00:00")
    start_time = now - timedelta(minutes=5)
    latest_end = now.replace(hour=0, minute=30, second=0, microsecond=0)
    _set_base_states(hass, start_time, [{"end": latest_end.isoformat()}])

    _freeze_time(hass, freezer, now)
    await hass.async_block_till_done()

    assert service_calls == []


@pytest.mark.asyncio
async def test_daytime_gate_allows_when_rates_json_string_for_tomorrow(hass, freezer):
    await _setup_automation(hass)
    service_calls = async_mock_service(hass, "homeassistant", "turn_on")

    now = dt_util.parse_datetime("2026-01-25T10:00:00+00:00")
    start_time = now - timedelta(minutes=5)
    latest_end = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
    _set_base_states(
        hass,
        start_time,
        '[{"end": "' + latest_end.isoformat() + '"}]',
    )

    _freeze_time(hass, freezer, now)
    await hass.async_block_till_done()

    assert len(service_calls) == 1
    assert service_calls[0].data["entity_id"] == ["switch.ev_charger"]
