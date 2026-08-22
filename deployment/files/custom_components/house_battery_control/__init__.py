"""Control the Garage battery from tariff and forecast inputs."""

from typing import Any

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.helpers.discovery import async_load_platform

from .config import Config, from_mapping
from .const import DOMAIN
from .coordinator import Coordinator

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.All(dict, from_mapping)}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    typed_config = config.get(DOMAIN)
    if not isinstance(typed_config, Config):
        return True

    previous = hass.data.get(DOMAIN)
    if previous is not None and hasattr(previous, "async_stop"):
        await _async_teardown(hass, previous)

    coordinator = Coordinator(hass, typed_config)
    hass.data[DOMAIN] = coordinator
    await async_load_platform(hass, "sensor", DOMAIN, {}, config)

    async def async_start(_event: Event[dict[str, object]] | None = None) -> None:
        await coordinator.async_start()

    async def async_stop(_event: Event[dict[str, object]]) -> None:
        await _async_teardown(hass, coordinator)

    stop_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop)
    listeners = [stop_listener]
    hass.data[f"{DOMAIN}.stop_listener"] = stop_listener
    if hass.state is CoreState.running:
        await async_start()
    else:
        started_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, async_start)
        listeners.append(started_listener)
        hass.data[f"{DOMAIN}.started_listener"] = started_listener
    hass.data[f"{DOMAIN}.listeners"] = listeners
    return True


async def _async_teardown(hass: HomeAssistant, coordinator: Coordinator) -> None:
    """Stop one instance and remove its event subscriptions exactly once."""

    listeners = hass.data.pop(f"{DOMAIN}.listeners", ())
    for listener in listeners:
        listener()
    await coordinator.async_stop()
    if hass.data.get(DOMAIN) is coordinator:
        hass.data.pop(DOMAIN, None)
        hass.data.pop(f"{DOMAIN}.stop_listener", None)
        hass.data.pop(f"{DOMAIN}.started_listener", None)


__all__ = ["CONFIG_SCHEMA", "async_setup"]
