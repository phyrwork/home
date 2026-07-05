from decimal import Decimal

import pytest
from homeassistant.components import input_number, input_select
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.house_battery_control.config import InverterConfig
from custom_components.house_battery_control.dependencies import (
    solis_cloud,
    stub_inverter,
)

MODE_ENTITY_ID = "input_select.house_battery_operating_mode"
TARGET_ENTITY_ID = "input_number.house_battery_state_of_charge_target"

CONFIG = InverterConfig(
    operating_mode_entity_id=MODE_ENTITY_ID,
    state_of_charge_target_entity_id=TARGET_ENTITY_ID,
)


def control(
    *,
    mode: solis_cloud.OperatingMode = solis_cloud.OperatingMode.FORCE_EXPORT,
    target: Decimal | None = Decimal("42"),
) -> solis_cloud.Control:
    return solis_cloud.Control(
        operating_mode=mode,
        target_state_of_charge_percent=target,
        power_w=Decimal("6000"),
    )


async def test_applies_target_before_operating_mode(hass: HomeAssistant) -> None:
    hass.states.async_set(TARGET_ENTITY_ID, "50")
    hass.states.async_set(MODE_ENTITY_ID, "self_consumption")
    calls: list[ServiceCall] = []
    _register_services(hass, calls)

    await stub_inverter.async_apply(hass, CONFIG, control())

    assert [
        (service_call.domain, service_call.service, service_call.data)
        for service_call in calls
    ] == [
        (
            input_number.DOMAIN,
            input_number.SERVICE_SET_VALUE,
            {
                ATTR_ENTITY_ID: TARGET_ENTITY_ID,
                input_number.ATTR_VALUE: 42.0,
            },
        ),
        (
            input_select.DOMAIN,
            input_select.SERVICE_SELECT_OPTION,
            {
                ATTR_ENTITY_ID: MODE_ENTITY_ID,
                input_select.ATTR_OPTION: "force_export",
            },
        ),
    ]


async def test_does_not_write_matching_control(hass: HomeAssistant) -> None:
    hass.states.async_set(TARGET_ENTITY_ID, "42.0")
    hass.states.async_set(MODE_ENTITY_ID, "force_export")

    await stub_inverter.async_apply(hass, CONFIG, control())


async def test_leaves_target_unchanged_when_control_has_no_target(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(TARGET_ENTITY_ID, "42")
    hass.states.async_set(MODE_ENTITY_ID, "self_consumption")
    calls: list[ServiceCall] = []
    _register_services(hass, calls)

    await stub_inverter.async_apply(
        hass,
        CONFIG,
        control(mode=solis_cloud.OperatingMode.HOLD, target=None),
    )

    assert len(calls) == 1
    assert calls[0].domain == input_select.DOMAIN
    assert calls[0].service == input_select.SERVICE_SELECT_OPTION
    assert calls[0].data == {
        ATTR_ENTITY_ID: MODE_ENTITY_ID,
        input_select.ATTR_OPTION: "hold",
    }


async def test_does_not_change_mode_if_target_write_fails(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(TARGET_ENTITY_ID, "50")
    hass.states.async_set(MODE_ENTITY_ID, "self_consumption")
    mode_calls: list[ServiceCall] = []

    async def fail_target(service_call: ServiceCall) -> None:
        raise RuntimeError("write failed")

    async def record_mode(service_call: ServiceCall) -> None:
        mode_calls.append(service_call)

    hass.services.async_register(
        input_number.DOMAIN,
        input_number.SERVICE_SET_VALUE,
        fail_target,
    )
    hass.services.async_register(
        input_select.DOMAIN,
        input_select.SERVICE_SELECT_OPTION,
        record_mode,
    )

    with pytest.raises(RuntimeError, match="write failed"):
        await stub_inverter.async_apply(hass, CONFIG, control())

    assert not mode_calls


def _register_services(
    hass: HomeAssistant,
    calls: list[ServiceCall],
) -> None:
    async def record(service_call: ServiceCall) -> None:
        calls.append(service_call)

    hass.services.async_register(
        input_number.DOMAIN,
        input_number.SERVICE_SET_VALUE,
        record,
    )
    hass.services.async_register(
        input_select.DOMAIN,
        input_select.SERVICE_SELECT_OPTION,
        record,
    )
