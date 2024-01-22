"""Config flow for Bindicator integration."""
from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from . import wastecalendar as wc


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bindicator."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await wc.next()
            except Exception as err:
                errors["base"] = str(err)
            else:
                return self.async_create_entry(
                    title="bindicator",  # TODO: Something better
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", errors=errors
        )
