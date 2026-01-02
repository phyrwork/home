from datetime import datetime, timedelta, timezone

from costing_helpers import (
    build_rates,
    cost_profile,
    find_best_start,
    profile_kwh,
    read_finish_time,
    read_profile_segments,
    select_override_rate,
)
from washing_machine_constants import (
    EXPORT_POWER_SENSOR,
    EXPORT_RATE_SENSOR,
    IMPORT_RATE_EVENTS,
    IMPORT_RATE_SENSOR,
)

DEFAULT_PROFILE_SEGMENTS = [
    (timedelta(minutes=30), 1.5),
    (timedelta(minutes=90), 0.2),
]
DEFAULT_LATEST_FINISH_TIME = "06:00:00"
# Note: Deployment edits trigger pyscript reloads for debugging.

SENSORS = {
    "sensor.dishwasher_start_now_cost": {
        "friendly_name": "Dishwasher Start Now Cost",
        "unit_of_measurement": "GBP",
    },
    "sensor.dishwasher_best_start_time": {
        "friendly_name": "Dishwasher Best Start Time",
        "device_class": "timestamp",
    },
    "sensor.dishwasher_best_cost": {
        "friendly_name": "Dishwasher Best Cost",
        "unit_of_measurement": "GBP",
    },
}


def set_all_unavailable(reason):
    for entity_id, base_attrs in SENSORS.items():
        attrs = base_attrs.copy()
        attrs["error"] = reason
        state.set(entity_id, "unavailable", attrs)


@service
def dishwasher_costing():
    now_local = datetime.now().astimezone()
    now_utc = now_local.astimezone(timezone.utc)

    profile_segments, profile_input, profile_error = read_profile_segments(
        "input_text.dishwasher_profile_segments",
        DEFAULT_PROFILE_SEGMENTS,
    )
    latest_finish_time, finish_input, finish_error = read_finish_time(
        "input_text.dishwasher_latest_finish_time",
        DEFAULT_LATEST_FINISH_TIME,
    )

    rates = build_rates(IMPORT_RATE_EVENTS)
    if not rates:
        set_all_unavailable("missing_rate_data")
        return

    best = find_best_start(
        now_local,
        now_utc,
        rates,
        profile_segments,
        latest_finish_time,
    )
    best_start = best["best_start"]
    best_cost = best["best_cost"]
    latest_finish_utc = best["latest_finish_utc"]
    latest_start_utc = best["latest_start_utc"]
    evaluated_starts = best["evaluated_starts"]

    if best_start is None or latest_finish_utc is None or latest_start_utc is None:
        set_all_unavailable("no_candidate_starts")
        return

    override_rate, rate_source = select_override_rate(
        EXPORT_POWER_SENSOR,
        IMPORT_RATE_SENSOR,
        EXPORT_RATE_SENSOR,
    )

    start_now_cost = None
    if override_rate is not None:
        start_now_cost = cost_profile(
            now_utc,
            rates,
            profile_segments,
            override_rate=override_rate,
            override_start=now_utc,
        )

    common_attrs = {
        "profile_kwh": profile_kwh(profile_segments),
        "latest_finish": latest_finish_utc.isoformat(),
        "latest_start": latest_start_utc.isoformat(),
        "profile_input": profile_input,
        "latest_finish_input": finish_input,
        "profile_error": profile_error,
        "latest_finish_error": finish_error,
    }

    if start_now_cost is None:
        attrs = SENSORS["sensor.dishwasher_start_now_cost"].copy()
        attrs.update(common_attrs)
        attrs["error"] = "missing_start_now_rate"
        state.set("sensor.dishwasher_start_now_cost", "unavailable", attrs)
    else:
        attrs = SENSORS["sensor.dishwasher_start_now_cost"].copy()
        attrs.update(common_attrs)
        attrs["rate_source"] = rate_source
        attrs["start_now_time"] = now_utc.isoformat()
        state.set("sensor.dishwasher_start_now_cost", round(start_now_cost, 4), attrs)

    if best_start is None or best_cost is None:
        attrs = SENSORS["sensor.dishwasher_best_start_time"].copy()
        attrs.update(common_attrs)
        attrs["error"] = "no_valid_start"
        attrs["evaluated_starts"] = evaluated_starts
        state.set("sensor.dishwasher_best_start_time", "unavailable", attrs)

        attrs = SENSORS["sensor.dishwasher_best_cost"].copy()
        attrs.update(common_attrs)
        attrs["error"] = "no_valid_start"
        attrs["evaluated_starts"] = evaluated_starts
        state.set("sensor.dishwasher_best_cost", "unavailable", attrs)
    else:
        attrs = SENSORS["sensor.dishwasher_best_start_time"].copy()
        attrs.update(common_attrs)
        attrs["best_cost"] = round(best_cost, 4)
        attrs["evaluated_starts"] = evaluated_starts
        state.set("sensor.dishwasher_best_start_time", best_start.isoformat(), attrs)

        attrs = SENSORS["sensor.dishwasher_best_cost"].copy()
        attrs.update(common_attrs)
        attrs["best_start"] = best_start.isoformat()
        attrs["evaluated_starts"] = evaluated_starts
        state.set("sensor.dishwasher_best_cost", round(best_cost, 4), attrs)
