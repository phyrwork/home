"""Focused offline tests for the read-only Solis state boundary."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.contracts import ControllerHealth, SlotDirection
from custom_components.house_battery_control.solis_reader import read_solis_state
from custom_components.house_battery_control.solis_config import SolisConfig
from custom_components.house_battery_control.solis_state import (
    MAXIMUM_FUTURE_CLOCK_SKEW,
    MAXIMUM_TELEMETRY_AGE,
    IssueSeverity,
)


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def test_hard_freshness_limits_are_named_and_exact() -> None:
    assert MAXIMUM_TELEMETRY_AGE == timedelta(minutes=5)
    assert MAXIMUM_FUTURE_CLOCK_SKEW == timedelta(minutes=1)


def deployed() -> dict[str, object]:
    path = Path(__file__).parents[3] / "house_battery_control.yaml"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def state(
    value: object,
    *,
    unit: str | None = None,
    last_updated: datetime | None = NOW,
) -> dict[str, object]:
    attributes: dict[str, object] = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    result: dict[str, object] = {"state": value, "attributes": attributes}
    if last_updated is not None:
        result["last_updated"] = last_updated
    return result


def capability(
    value: str,
    unit: str,
    minimum: str = "0",
    maximum: str = "100",
    step: str = "1",
) -> dict[str, object]:
    return {
        "state": value,
        "attributes": {
            "min": minimum,
            "max": maximum,
            "step": step,
            "unit_of_measurement": unit,
        },
    }


def valid_fixture() -> tuple[SolisConfig, dict[str, object]]:
    source = deployed()
    solis = source["solis"]
    solis["telemetry"].update(
        battery_power_entity_id="sensor.garage_battery_power",
        battery_power_sign="positive_means_charging",
        device_timestamp_entity_id="sensor.garage_device_time",
    )
    parsed = config.from_mapping(source)
    assert parsed.solis is not None
    states: dict[str, object] = {}
    telemetry = parsed.solis.telemetry
    states[telemetry.state_of_charge_entity_id] = state("55", unit="%")
    states[telemetry.battery_power_entity_id] = state("2500", unit="W")
    states[telemetry.device_timestamp_entity_id] = state(NOW.isoformat())
    persistent = parsed.solis.persistent
    states[persistent.storage_mode_entity_id] = {
        "state": "Feed-In Priority",
        "attributes": {"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]},
    }
    for entity_id in (
        persistent.allow_grid_charging_entity_id,
        persistent.allow_export_entity_id,
        persistent.grid_peak_shaving_entity_id,
        persistent.inverter_on_off_entity_id,
    ):
        states[entity_id] = state("on")
    states[persistent.inverter_time_entity_id] = state(NOW.isoformat())
    protection = parsed.solis.protection
    for entity_id in (
        protection.battery_over_discharge_soc_entity_id,
        protection.battery_force_charge_soc_entity_id,
        protection.battery_recovery_soc_entity_id,
        protection.battery_max_charge_soc_entity_id,
        protection.battery_reserve_soc_entity_id,
    ):
        states[entity_id] = capability("10", "%")
    states[protection.battery_reserve_entity_id] = state("off")
    caps = parsed.solis.capability
    states[caps.battery_max_charge_current_entity_id] = capability("100", "A", maximum="200", step="1")
    states[caps.battery_max_discharge_current_entity_id] = capability("100", "A", maximum="200", step="1")
    states[caps.max_output_power_entity_id] = capability("5000", "W", maximum="6000", step="1")
    states[caps.max_export_power_entity_id] = capability("5000", "W", maximum="6000", step="1")
    for slot in parsed.solis.slots:
        for direction in (slot.charge, slot.discharge):
            states[direction.enable_entity_id] = state("off")
            states[direction.time_entity_id] = state("00:00-00:00")
            states[direction.current_entity_id] = capability(
                str(slot.physical_slot),
                "A",
                maximum=str(slot.physical_slot + 10),
            )
            states[direction.target_soc_entity_id] = capability("50", "%")
    return parsed.solis, states


def test_complete_snapshot_normalizes_power_and_preserves_slot_capabilities() -> None:
    solis, states = valid_fixture()

    result = read_solis_state(solis, states, NOW)

    assert result.health is ControllerHealth.HEALTHY
    assert result.snapshot is not None
    assert result.snapshot.telemetry.battery_power_kw == 2.5
    assert result.snapshot.telemetry.soc_percent == 55
    assert len(result.snapshot.slots) == 6
    assert [slot.capability.charge_current.maximum for slot in result.snapshot.slots] == [
        11,
        12,
        13,
        14,
        15,
        16,
    ]
    assert result.snapshot.slots[0].charge.direction is SlotDirection.CHARGE
    assert result.snapshot.capabilities.maximum_charge_current is not None


def test_power_units_and_source_signs_are_normalized() -> None:
    solis, states = valid_fixture()
    power_id = solis.telemetry.battery_power_entity_id
    states[power_id] = state("2", unit="kW")
    charging = read_solis_state(solis, states, NOW)
    assert charging.snapshot is not None
    assert charging.snapshot.telemetry.battery_power_kw == 2

    source = deployed()
    source["solis"]["telemetry"].update(
        battery_power_entity_id="sensor.garage_battery_power",
        battery_power_sign="positive_means_discharging",
        device_timestamp_entity_id="sensor.garage_device_time",
    )
    parsed = config.from_mapping(source)
    assert parsed.solis is not None
    states[power_id] = state("2", unit="kW")
    states[parsed.solis.telemetry.device_timestamp_entity_id] = state(NOW.isoformat())
    discharging = read_solis_state(parsed.solis, states, NOW)
    assert discharging.snapshot is not None
    assert discharging.snapshot.telemetry.battery_power_kw == -2


@pytest.mark.parametrize("bad_unit", (None, "MW"))
def test_missing_or_unknown_power_unit_is_critical(bad_unit: str | None) -> None:
    solis, states = valid_fixture()
    power_state = states[solis.telemetry.battery_power_entity_id]
    if bad_unit is None:
        power_state["attributes"].pop("unit_of_measurement")
    else:
        power_state["attributes"]["unit_of_measurement"] = bad_unit
    result = read_solis_state(solis, states, NOW)
    assert result.health is ControllerHealth.DEGRADED
    assert any(issue.code.startswith("battery_power_unit") for issue in result.issues)


def test_unresolved_configuration_and_bad_timestamp_are_critical() -> None:
    source = deployed()
    parsed = config.from_mapping(source)
    assert parsed.solis is not None
    result = read_solis_state(parsed.solis, {}, NOW)
    assert result.health is ControllerHealth.DEGRADED
    assert any(
        issue.code == "battery_power_unresolved"
        and issue.severity is IssueSeverity.CRITICAL
        for issue in result.issues
    )
    assert any(issue.code == "device_timestamp_unresolved" for issue in result.issues)

    solis, states = valid_fixture()
    states[solis.telemetry.device_timestamp_entity_id]["state"] = (
        NOW - MAXIMUM_TELEMETRY_AGE - timedelta(seconds=1)
    ).isoformat()
    stale = read_solis_state(solis, states, NOW)
    assert stale.health is ControllerHealth.DEGRADED
    assert any(issue.code == "device_timestamp_stale" for issue in stale.issues)

    solis, states = valid_fixture()
    states[solis.telemetry.device_timestamp_entity_id]["state"] = (
        NOW + timedelta(minutes=1, seconds=1)
    ).isoformat()
    future = read_solis_state(solis, states, NOW)
    assert any(issue.code == "device_timestamp_future" for issue in future.issues)

    solis, states = valid_fixture()
    states[solis.telemetry.device_timestamp_entity_id]["state"] = NOW.replace(
        tzinfo=None
    ).isoformat()
    naive = read_solis_state(solis, states, NOW)
    assert any(issue.code == "device_timestamp_invalid" for issue in naive.issues)


@pytest.mark.parametrize("entity_kind", ("soc", "power"))
def test_home_assistant_observation_timestamps_are_required_and_compared(
    entity_kind: str,
) -> None:
    solis, states = valid_fixture()
    entity_id = (
        solis.telemetry.state_of_charge_entity_id
        if entity_kind == "soc"
        else solis.telemetry.battery_power_entity_id
    )
    states[entity_id].pop("last_updated")
    missing = read_solis_state(solis, states, NOW)
    assert any(
        issue.code == "ha_last_updated_missing" and issue.entity_id == entity_id
        for issue in missing.issues
    )

    solis, states = valid_fixture()
    states[entity_id]["last_updated"] = NOW - MAXIMUM_TELEMETRY_AGE - timedelta(seconds=1)
    disagreement = read_solis_state(solis, states, NOW)
    assert any(
        issue.code == "device_timestamp_disagrees" and issue.entity_id == entity_id
        for issue in disagreement.issues
    )


def test_nan_inf_and_invalid_soc_capability_metadata_are_critical() -> None:
    for value in ("nan", "inf"):
        solis, states = valid_fixture()
        entity_id = solis.slots[0].charge.target_soc_entity_id
        states[entity_id]["state"] = value
        result = read_solis_state(solis, states, NOW)
        assert result.health is ControllerHealth.DEGRADED

    solis, states = valid_fixture()
    entity_id = solis.slots[0].charge.target_soc_entity_id
    states[entity_id]["attributes"]["min"] = "-1"
    result = read_solis_state(solis, states, NOW)
    assert any(issue.code == "capability_soc_out_of_range" for issue in result.issues)


@pytest.mark.parametrize(
    ("attribute", "value"),
    (("min", "101"), ("max", "-1"), ("step", "0"), ("unit_of_measurement", "A")),
)
def test_invalid_capability_bounds_step_and_unit_are_critical(
    attribute: str, value: str
) -> None:
    solis, states = valid_fixture()
    entity_id = solis.slots[0].charge.target_soc_entity_id
    states[entity_id]["attributes"][attribute] = value
    result = read_solis_state(solis, states, NOW)
    assert result.health is ControllerHealth.DEGRADED
    assert any(issue.entity_id == entity_id for issue in result.issues)


@pytest.mark.parametrize("value", ("nan", "inf", "101", "-1"))
def test_invalid_soc_never_becomes_healthy(value: str) -> None:
    solis, states = valid_fixture()
    states[solis.telemetry.state_of_charge_entity_id]["state"] = value
    result = read_solis_state(solis, states, NOW)
    assert result.health is ControllerHealth.DEGRADED


@pytest.mark.parametrize("state_value", (None, "unknown", "unavailable"))
@pytest.mark.parametrize("entity_kind", ("soc", "power", "switch"))
def test_missing_unknown_and_unavailable_required_entities_are_degraded(
    state_value: str | None,
    entity_kind: str,
) -> None:
    solis, states = valid_fixture()
    entity_id = {
        "soc": solis.telemetry.state_of_charge_entity_id,
        "power": solis.telemetry.battery_power_entity_id,
        "switch": solis.persistent.allow_export_entity_id,
    }[entity_kind]
    if state_value is None:
        del states[entity_id]
    else:
        states[entity_id]["state"] = state_value
    result = read_solis_state(solis, states, NOW)
    assert result.health is ControllerHealth.DEGRADED
    assert result.snapshot is None


def test_storage_options_switch_and_datetime_validation() -> None:
    solis, states = valid_fixture()
    storage_id = solis.persistent.storage_mode_entity_id
    states[storage_id]["attributes"].pop("options")
    missing_options = read_solis_state(solis, states, NOW)
    assert any(issue.code == "storage_mode_options_invalid" for issue in missing_options.issues)

    solis, states = valid_fixture()
    states[solis.persistent.allow_export_entity_id]["state"] = "unknown"
    states[solis.persistent.inverter_time_entity_id]["state"] = "not-a-datetime"
    invalid = read_solis_state(solis, states, NOW)
    assert any(issue.code == "switch_state_invalid" for issue in invalid.issues)
    assert any(issue.code == "inverter_datetime_invalid" for issue in invalid.issues)


class RecordingStateAccess:
    def __init__(self, states: dict[str, object]) -> None:
        self.states = states
        self.reads = 0
        self.service_calls = 0

    def get(self, entity_id: str, default: object = None) -> object:
        self.reads += 1
        return self.states.get(entity_id, default)

    def call_service(self, *_args: object, **_kwargs: object) -> None:
        self.service_calls += 1


def test_reader_only_uses_injected_state_reads() -> None:
    solis, states = valid_fixture()
    access = RecordingStateAccess(states)
    result = read_solis_state(solis, access, NOW)
    assert result.snapshot is not None
    assert access.reads > 0
    assert access.service_calls == 0


def test_enabled_zero_interval_and_reserved_direction_are_rejected() -> None:
    solis, states = valid_fixture()
    reserved = solis.slots[1].charge
    states[reserved.enable_entity_id]["state"] = "on"
    states[reserved.time_entity_id]["state"] = "00:00-00:00"
    result = read_solis_state(solis, states, NOW)
    assert result.health is ControllerHealth.DEGRADED
    assert any(issue.code == "slot_time_zero_enabled" for issue in result.issues)

    solis, states = valid_fixture()
    reserved = solis.slots[1].discharge
    states[reserved.enable_entity_id]["state"] = "on"
    states[reserved.time_entity_id]["state"] = "23:00-01:00"
    reserved_result = read_solis_state(solis, states, NOW)
    assert any(issue.code == "reserved_slot_enabled" for issue in reserved_result.issues)

    solis, states = valid_fixture()
    states[solis.slots[0].charge.enable_entity_id]["state"] = "on"
    states[solis.slots[0].charge.time_entity_id]["state"] = "23:00-01:00"
    assert read_solis_state(solis, states, NOW).snapshot is not None
    states[solis.slots[0].discharge.enable_entity_id]["state"] = "on"
    states[solis.slots[0].discharge.time_entity_id]["state"] = "01:00-02:00"
    conflict = read_solis_state(solis, states, NOW)
    assert any(issue.code == "multiple_enabled_slots" for issue in conflict.issues)
    assert any(issue.code == "slot_direction_conflict" for issue in conflict.issues)

    solis, states = valid_fixture()
    states[solis.slots[0].charge.enable_entity_id]["state"] = "on"
    states[solis.slots[0].charge.time_entity_id]["state"] = "01:00-02:00"
    states[solis.slots[1].discharge.enable_entity_id]["state"] = "on"
    states[solis.slots[1].discharge.time_entity_id]["state"] = "03:00-04:00"
    across_slots = read_solis_state(solis, states, NOW)
    assert any(issue.code == "multiple_enabled_slots" for issue in across_slots.issues)
