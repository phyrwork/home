from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_NAME,
    CONF_EXPORT_POWER_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_MAX_COST_PERCENTILE,
    CONF_PROFILE,
    CONF_PROFILE_FILE,
    CONF_PROFILE_SENSOR,
    CONF_START_STEP_MODE,
    CONF_START_STEP_MINUTES,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_MAX_COST_PERCENTILE,
    DEFAULT_START_MODE,
    DEFAULT_START_STEP_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    START_MODE_OPTIONS,
)


class EnergyCostForecastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            data = dict(user_input)
            unique_id = f"{data[CONF_NAME]}::{data[CONF_IMPORT_RATE_SENSOR]}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=data[CONF_NAME], data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_IMPORT_RATE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["event", "sensor"])
                ),
                vol.Optional(CONF_EXPORT_RATE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_EXPORT_POWER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_PROFILE): str,
                vol.Optional(CONF_PROFILE_FILE): str,
                vol.Optional(CONF_PROFILE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["sensor", "input_text"])
                ),
                vol.Optional(CONF_MAX_COST_PERCENTILE, default=DEFAULT_MAX_COST_PERCENTILE): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100, step=1, mode="slider")
                ),
                vol.Optional(CONF_START_STEP_MODE, default=DEFAULT_START_MODE): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=START_MODE_OPTIONS, mode="dropdown")
                ),
                vol.Optional(CONF_START_STEP_MINUTES, default=DEFAULT_START_STEP_MINUTES): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=1440, step=1, mode="box")
                ),
                vol.Optional(CONF_UPDATE_INTERVAL_MINUTES, default=DEFAULT_UPDATE_INTERVAL_MINUTES): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=1440, step=1, mode="box")
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_import(self, user_input):
        data = dict(user_input)
        unique_id = f"{data[CONF_NAME]}::{data[CONF_IMPORT_RATE_SENSOR]}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_NAME], data=data)
