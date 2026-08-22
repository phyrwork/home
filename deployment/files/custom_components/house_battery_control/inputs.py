"""Read Home Assistant state into planner inputs."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from . import load, planner
from .config import Config
from .dependencies import forecast_solar, octopus_energy, solis_cloud
from .dependencies.octopus_energy import Rate
from .solis_state import SolisStateReadResult


async def async_read_input(
    hass: HomeAssistant,
    config: Config,
    *,
    now: datetime,
    solis_result: SolisStateReadResult | None = None,
) -> planner.Input:
    """Read and map all Home Assistant sources required by the planner."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Planning time must be timezone-aware")

    power_limit_kw = read_decimal(hass, config.battery.power_limit_entity_id)
    battery_spec = config.battery.to_spec(power_limit_kw)
    if solis_result is not None:
        if solis_result.telemetry is None or not solis_result.telemetry.state_of_charge_percent.is_finite():
            raise ValueError("real Solis SOC is unavailable")
        soc = solis_result.telemetry.state_of_charge_percent
    else:
        # Kept only for the pre-Solis compatibility boundary.  The live
        # coordinator always supplies a Solis result and never reads the
        # retired stub SOC helper.
        soc = read_decimal(hass, config.battery.state_of_charge_entity_id)
    battery_state = solis_cloud.to_battery_state(
        {"state_of_charge_percent": soc}, battery_spec
    )

    import_price_state = _state(hass, config.tariff.import_price_entity_id)
    rates = import_price_state.attributes.get("rates")
    if not isinstance(rates, (str, list, tuple)):
        raise ValueError(
            f"{config.tariff.import_price_entity_id} has no valid rates attribute"
        )
    tariff_forecast = octopus_energy.to_tariff_intervals(
        cast(str | Sequence[Rate], rates),
        read_decimal(hass, config.tariff.export_price_entity_id),
    )
    future_tariffs = tuple(
        item for item in tariff_forecast if item.interval.end > now
    )
    if not future_tariffs:
        raise ValueError("Import price forecast does not extend beyond planning time")
    horizon_end = max(item.interval.end for item in future_tariffs)

    raw_solar_forecast = await _async_get_solar_forecast(
        hass,
        config.solar.config_entry_id,
    )
    if raw_solar_forecast is None:
        raise ValueError(
            f"Forecast.Solar config entry is unavailable: {config.solar.config_entry_id}"
        )
    solar_forecast = forecast_solar.to_energy_intervals(
        cast(forecast_solar.Forecast, raw_solar_forecast)
    )
    if not solar_forecast:
        raise ValueError("Forecast.Solar returned fewer than two energy periods")

    timezone = dt_util.get_time_zone(hass.config.time_zone)
    if timezone is None:
        raise ValueError(f"Unknown Home Assistant timezone: {hass.config.time_zone}")

    return planner.Input(
        now=now,
        battery_spec=battery_spec,
        battery_state=battery_state,
        tariff_forecast=future_tariffs,
        load_forecast=load.forecast(
            now=now,
            horizon_end=horizon_end,
            timezone=timezone,
        ),
        solar_forecast=solar_forecast,
    )


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    if state is None:
        raise ValueError(f"Required entity does not exist: {entity_id}")
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise ValueError(f"Required entity is {state.state}: {entity_id}")
    return state


def read_decimal(hass: HomeAssistant, entity_id: str) -> Decimal:
    """Read a finite decimal value from a required Home Assistant entity."""
    value = _state(hass, entity_id).state
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Required entity is not numeric: {entity_id}") from None
    if not result.is_finite():
        raise ValueError(f"Required entity is not finite: {entity_id}")
    return result


async def _async_get_solar_forecast(
    hass: HomeAssistant,
    config_entry_id: str,
) -> dict[str, Any] | None:
    """Read Forecast.Solar through Home Assistant's energy API."""
    from homeassistant.components.forecast_solar.energy import (
        async_get_solar_forecast,
    )

    return await async_get_solar_forecast(hass, config_entry_id)
