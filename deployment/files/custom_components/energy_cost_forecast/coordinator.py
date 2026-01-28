from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COSTS,
    ATTR_PROFILE,
    ATTR_PROFILE_ERROR,
    ATTR_PROFILE_INPUT,
    ATTR_PROFILE_SOURCE,
    ATTR_RATE_SOURCE,
    CONF_EXPORT_POWER_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_PROFILE,
    CONF_POWER_PROFILE_FILE,
    CONF_POWER_PROFILE_ENTITY,
    CONF_START_AFTER,
    CONF_START_BEFORE,
    CONF_FINISH_AFTER,
    CONF_FINISH_BEFORE,
    CONF_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    DATA_START_STEP_MODE,
    DATA_START_STEP_MINUTES,
    DEFAULT_START_MODE,
    DEFAULT_START_STEP_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    START_MODE_FIXED_INTERVAL_LABEL,
    DATA_TARGET_PERCENTILE,
    DEFAULT_TARGET_PERCENTILE,
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
        update_minutes = entry.data.get(
            CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        update_interval = None
        if update_minutes:
            update_interval = timedelta(minutes=update_minutes)
        super().__init__(
            hass,
            logger=logging.getLogger(__name__),
            name="energy_cost_forecast",
            update_interval=update_interval,
        )
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
            self.entry.data.get(CONF_POWER_PROFILE_FILE),
            self.entry.data.get(CONF_POWER_PROFILE_ENTITY),
        )
        profile_source = None
        if self.entry.data.get(CONF_POWER_PROFILE_ENTITY):
            profile_source = "sensor"
        elif self.entry.data.get(CONF_POWER_PROFILE_FILE):
            profile_source = "file"
        elif self.entry.data.get(CONF_PROFILE):
            profile_source = "inline"

        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {})
        target_percentile = float(
            entry_data.get(DATA_TARGET_PERCENTILE, DEFAULT_TARGET_PERCENTILE)
        )
        start_step_minutes = int(
            entry_data.get(DATA_START_STEP_MINUTES, DEFAULT_START_STEP_MINUTES)
        )
        start_mode = entry_data.get(DATA_START_STEP_MODE, DEFAULT_START_MODE)
        fixed_interval = start_mode == START_MODE_FIXED_INTERVAL_LABEL

        profile_attr = [
            {"power_w": round(seg.power_kw * 1000.0, 3), "duration_s": int(seg.duration.total_seconds())}
            for seg in profile_segments
        ] if profile_segments else []

        if not rates or not profile_segments:
            return {
                "now": None,
                "later": [],
                "min": None,
                "max": None,
                "now_percentile": None,
                "max_percentile": None,
                "max_percentile_time": None,
                "latest_percentile": None,
                "latest_percentile_time": None,
                "rate_unit": rate_unit,
                "cost_unit": cost_unit,
                ATTR_PROFILE: profile_attr,
                ATTR_PROFILE_INPUT: profile_input,
                ATTR_PROFILE_SOURCE: profile_source,
                ATTR_PROFILE_ERROR: profile_error or ("missing_rate_data" if not rates else None),
                ATTR_RATE_SOURCE: None,
            }

        total_duration = profile_duration(profile_segments)

        def _parse_local_time_filter(
            value: object | None,
        ) -> tuple[datetime | None, time | None]:
            if value in (None, ""):
                return None, None
            parsed = dt_util.parse_datetime(str(value))
            if parsed is None:
                parsed_time = dt_util.parse_time(str(value))
                return None, parsed_time
            if parsed.tzinfo is None:
                tz = dt_util.DEFAULT_TIME_ZONE
                if hasattr(tz, "localize"):
                    parsed = tz.localize(parsed)
                else:
                    parsed = parsed.replace(tzinfo=tz)
            return dt_util.as_utc(parsed), None

        start_after_dt, start_after_time = _parse_local_time_filter(
            self.entry.data.get(CONF_START_AFTER)
        )
        start_before_dt, start_before_time = _parse_local_time_filter(
            self.entry.data.get(CONF_START_BEFORE)
        )
        finish_after_dt, finish_after_time = _parse_local_time_filter(
            self.entry.data.get(CONF_FINISH_AFTER)
        )
        finish_before_dt, finish_before_time = _parse_local_time_filter(
            self.entry.data.get(CONF_FINISH_BEFORE)
        )

        starts = candidate_starts(
            rates,
            now_utc,
            None,
            fixed_interval,
            start_step_minutes,
        )
        costs = []
        for start_dt in starts:
            finish_dt = start_dt + total_duration
            start_local = dt_util.as_local(start_dt)
            finish_local = dt_util.as_local(finish_dt)

            if start_after_dt and start_dt < start_after_dt:
                continue
            if start_before_dt and start_dt >= start_before_dt:
                continue
            if finish_after_dt and finish_dt < finish_after_dt:
                continue
            if finish_before_dt and finish_dt >= finish_before_dt:
                continue
            if start_after_time and start_local.time() < start_after_time:
                continue
            if start_before_time and start_local.time() >= start_before_time:
                continue
            if finish_after_time and finish_local.time() < finish_after_time:
                continue
            if finish_before_time and finish_local.time() >= finish_before_time:
                continue
            cost = cost_profile(start_dt, rates, profile_segments)
            if cost is None:
                continue
            costs.append(
                {
                    "start": dt_util.as_local(start_dt).isoformat(),
                    "finish": dt_util.as_local(finish_dt).isoformat(),
                    "cost": round(cost, 4),
                }
            )

        cost_min = None
        cost_max = None
        cost_max_percentile_time = None
        cost_max_percentile = None
        cost_latest_percentile_time = None
        cost_latest_percentile = None
        cost_min_all = None
        cost_max_all = None
        if costs:
            values = [item["cost"] for item in costs]
            cost_min_all = min(values)
            cost_max_all = max(values)
            threshold = None
            if cost_max_all is not None and cost_min_all is not None:
                if abs(cost_max_all - cost_min_all) < 1e-9:
                    threshold = cost_min_all
                else:
                    threshold = cost_min_all + (
                        (cost_max_all - cost_min_all) * target_percentile / 100.0
                    )
            eligible = []
            if threshold is not None:
                for item in costs:
                    if item["cost"] <= threshold + 1e-9:
                        eligible.append(item)
            if eligible:
                eligible_values = [item["cost"] for item in eligible]
                cost_min = round(min(eligible_values), 4)
                cost_max = round(max(eligible_values), 4)
                next_item = eligible[0]
                latest_item = eligible[-1]
                cost_max_percentile_time = {
                    "start": next_item["start"],
                    "finish": next_item["finish"],
                    "cost": next_item["cost"],
                }
                cost_max_percentile = next_item["cost"]
                cost_latest_percentile_time = {
                    "start": latest_item["start"],
                    "finish": latest_item["finish"],
                    "cost": latest_item["cost"],
                }
                cost_latest_percentile = latest_item["cost"]

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

        if cost_now is None or not costs:
            return {
                "now": None,
                "later": costs,
                "min": cost_min,
                "max": cost_max,
                "now_percentile": None,
                "max_percentile": cost_max_percentile,
                "max_percentile_time": cost_max_percentile_time,
                "latest_percentile": cost_latest_percentile,
                "latest_percentile_time": cost_latest_percentile_time,
                "rate_unit": rate_unit,
                "cost_unit": cost_unit,
                "start_now_time": now_utc.isoformat(),
                ATTR_PROFILE: profile_attr,
                ATTR_PROFILE_INPUT: profile_input,
                ATTR_PROFILE_SOURCE: profile_source,
                ATTR_PROFILE_ERROR: profile_error,
                ATTR_RATE_SOURCE: rate_source,
            }

        cost_now_percentile = None
        if cost_now is not None and cost_min_all is not None and cost_max_all is not None:
            if abs(cost_max_all - cost_min_all) < 1e-9:
                cost_now_percentile = 0.0
            else:
                cost_now_percentile = round(
                    100.0 * (cost_now - cost_min_all) / (cost_max_all - cost_min_all),
                    2,
                )
                if cost_now_percentile < 0:
                    cost_now_percentile = 0.0

        return {
            "now": round(cost_now, 4) if cost_now is not None else None,
            "later": costs,
            "min": cost_min,
            "max": cost_max,
            "now_percentile": cost_now_percentile,
            "max_percentile": cost_max_percentile,
            "max_percentile_time": cost_max_percentile_time,
            "latest_percentile": cost_latest_percentile,
            "latest_percentile_time": cost_latest_percentile_time,
            "rate_unit": rate_unit,
            "cost_unit": cost_unit,
            "start_now_time": now_utc.isoformat(),
            ATTR_PROFILE: profile_attr,
            ATTR_PROFILE_INPUT: profile_input,
            ATTR_PROFILE_SOURCE: profile_source,
            ATTR_PROFILE_ERROR: profile_error,
            ATTR_RATE_SOURCE: rate_source,
        }
