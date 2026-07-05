"""Temporary Home Assistant helper-backed inverter control."""

from decimal import Decimal, InvalidOperation

from homeassistant.components import input_number, input_select
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from ..config import InverterConfig
from .solis_cloud import Control, OperatingMode


async def async_apply(
    hass: HomeAssistant,
    config: InverterConfig,
    control: Control,
) -> None:
    """Apply a normalized inverter control to temporary helper entities."""
    target = control.target_state_of_charge_percent
    if target is not None and not _target_matches(
        hass,
        config.state_of_charge_target_entity_id,
        target,
    ):
        await hass.services.async_call(
            input_number.DOMAIN,
            input_number.SERVICE_SET_VALUE,
            {
                ATTR_ENTITY_ID: config.state_of_charge_target_entity_id,
                input_number.ATTR_VALUE: float(target),
            },
            blocking=True,
        )

    if not _mode_matches(
        hass,
        config.operating_mode_entity_id,
        control.operating_mode,
    ):
        await hass.services.async_call(
            input_select.DOMAIN,
            input_select.SERVICE_SELECT_OPTION,
            {
                ATTR_ENTITY_ID: config.operating_mode_entity_id,
                input_select.ATTR_OPTION: control.operating_mode.value,
            },
            blocking=True,
        )


def _target_matches(
    hass: HomeAssistant,
    entity_id: str,
    target: Decimal,
) -> bool:
    state = hass.states.get(entity_id)
    if state is None:
        return False
    try:
        return Decimal(state.state) == target
    except InvalidOperation:
        return False


def _mode_matches(
    hass: HomeAssistant,
    entity_id: str,
    mode: OperatingMode,
) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state == mode
