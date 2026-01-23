from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COSTS,
    ATTR_LATEST_FINISH,
    ATTR_LATEST_START,
    ATTR_PROFILE,
    ATTR_PROFILE_ERROR,
    ATTR_PROFILE_INPUT,
    ATTR_PROFILE_SOURCE,
    ATTR_RATE_SOURCE,
    CONF_EXPORT_POWER_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_PROFILE,
    CONF_PROFILE_FILE,
    CONF_PROFILE_SENSOR,
    CONF_START_BY,
)
from .helpers import (
    RateWindow,
    candidate_starts,
    cost_profile,
    cost_profile_with_export_offset,
    load_profile,
    parse_rates,
    profile_duration,
)


class EnergyCostForecastCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, logger=logging.getLogger(__name__), name="energy_cost_forecast")
        self.entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        now_local = dt_util.now()
        now_utc = dt_util.as_utc(now_local)

        import_rate_sensor = self.entry.data[CONF_IMPORT_RATE_SENSOR]
        import_state = self.hass.states.get(import_rate_sensor)
        import_attrs = import_state.attributes if import_state else {}
        rates = parse_rates(import_attrs.get("rates"))
        rate_unit = import_attrs.get("unit_of_measurement") or import_attrs.get("rate_unit")
        cost_unit = None
        if rate_unit:
            unit_text = str(rate_unit)
            if "/" in unit_text:
                left, right = unit_text.split("/", 1)
                if "kwh" in right.lower():
                    cost_unit = left.strip() or None
            if cost_unit is None:
                cost_unit = unit_text
        if rates:
            filtered_rates = []
            for rate in rates:
                if rate.end <= now_utc:
                    continue
                if rate.start < now_utc:
                    filtered_rates.append(
                        RateWindow(start=now_utc, end=rate.end, value=rate.value)
                    )
                else:
                    filtered_rates.append(rate)
            rates = filtered_rates

        profile_segments, profile_input, profile_error = await load_profile(
            self.hass,
            self.entry.data.get(CONF_PROFILE),
            self.entry.data.get(CONF_PROFILE_FILE),
            self.entry.data.get(CONF_PROFILE_SENSOR),
        )
        profile_source = None
        if self.entry.data.get(CONF_PROFILE_SENSOR):
            profile_source = "sensor"
        elif self.entry.data.get(CONF_PROFILE_FILE):
            profile_source = "file"
        elif self.entry.data.get(CONF_PROFILE):
            profile_source = "inline"

        if not rates or not profile_segments:
            return {
                "now": None,
                "later": [],
                "min_time": None,
                "min": None,
                "max": None,
                "now_percentile": None,
                "rate_unit": rate_unit,
                "cost_unit": cost_unit,
                ATTR_PROFILE: [],
                ATTR_PROFILE_INPUT: profile_input,
                ATTR_PROFILE_SOURCE: profile_source,
                ATTR_PROFILE_ERROR: profile_error or ("missing_rate_data" if not rates else None),
                ATTR_LATEST_START: None,
                ATTR_LATEST_FINISH: None,
                ATTR_RATE_SOURCE: None,
            }

        total_duration = profile_duration(profile_segments)

        latest_start_utc = None
        latest_finish_utc = None

        starts = candidate_starts(rates, now_utc, latest_start_utc)
        costs = []
        for start_dt in starts:
            cost = cost_profile(start_dt, rates, profile_segments)
            if cost is None:
                continue
            finish_dt = start_dt + total_duration
            costs.append(
                {
                    "start": dt_util.as_local(start_dt).isoformat(),
                    "finish": dt_util.as_local(finish_dt).isoformat(),
                    "cost": round(cost, 4),
                }
            )

        cost_min = None
        cost_max = None
        cost_min_time = None
        if costs:
            values = [item["cost"] for item in costs]
            cost_min = round(min(values), 4)
            cost_max = round(max(values), 4)
            min_item = min(costs, key=lambda item: item["cost"])
            cost_min_time = {
                "start": min_item["start"],
                "finish": min_item["finish"],
                "cost": min_item["cost"],
            }

        export_power_sensor = self.entry.data.get(CONF_EXPORT_POWER_SENSOR)
        export_rate_sensor = self.entry.data.get(CONF_EXPORT_RATE_SENSOR)
        export_power_kw = 0.0
        export_rate = None
        rate_source = "import"
        if export_power_sensor:
            try:
                export_power_kw = float(self.hass.states.get(export_power_sensor).state) / 1000.0
            except Exception:
                export_power_kw = 0.0
        if export_rate_sensor:
            try:
                export_rate = float(self.hass.states.get(export_rate_sensor).state)
            except Exception:
                export_rate = None

        cost_now = None
        if export_power_kw > 0 and export_rate is not None:
            cost_now = cost_profile_with_export_offset(
                now_utc,
                rates,
                profile_segments,
                export_power_kw,
                export_rate,
            )
            rate_source = "export"
        else:
            cost_now = cost_profile(now_utc, rates, profile_segments)

        cost_now_percentile = None
        if cost_now is not None and cost_min is not None and cost_max is not None:
            if abs(cost_max - cost_min) < 1e-9:
                cost_now_percentile = 0.0
            else:
                cost_now_percentile = round(
                    100.0 * (cost_now - cost_min) / (cost_max - cost_min),
                    2,
                )
                if cost_now_percentile < 0:
                    cost_now_percentile = 0.0

        profile_attr = [
            {"power_w": round(seg.power_kw * 1000.0, 3), "duration_s": int(seg.duration.total_seconds())}
            for seg in profile_segments
        ]

        return {
            "now": round(cost_now, 4) if cost_now is not None else None,
            "later": costs,
            "min_time": cost_min_time,
            "min": cost_min,
            "max": cost_max,
            "now_percentile": cost_now_percentile,
            "rate_unit": rate_unit,
            "cost_unit": cost_unit,
            "start_now_time": now_utc.isoformat(),
            ATTR_PROFILE: profile_attr,
            ATTR_PROFILE_INPUT: profile_input,
            ATTR_PROFILE_SOURCE: profile_source,
            ATTR_PROFILE_ERROR: profile_error,
            ATTR_LATEST_START: latest_start_utc.isoformat() if latest_start_utc else None,
            ATTR_LATEST_FINISH: latest_finish_utc.isoformat() if latest_finish_utc else None,
            ATTR_RATE_SOURCE: rate_source,
        }
