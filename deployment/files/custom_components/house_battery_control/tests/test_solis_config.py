"""Offline validation tests for the live Solis entity configuration."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.solis_config import (
    BatteryPowerSign,
    SolisSlotOwner,
)


def deployed() -> dict[str, object]:
    path = Path(__file__).parents[3] / "house_battery_control.yaml"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_legacy_configuration_without_solis_remains_valid() -> None:
    source = deployed()
    source.pop("solis")

    parsed = config.from_mapping(source)

    assert parsed.solis is None


def test_deployed_solis_map_is_complete_and_typed() -> None:
    parsed = config.from_mapping(deployed())

    assert parsed.solis is not None
    assert parsed.solis.telemetry.state_of_charge_entity_id == (
        "sensor.garage_inverter_telemetry_garage_inverter_remaining_battery_capacity"
    )
    assert parsed.solis.telemetry.battery_power_entity_id is None
    assert parsed.solis.telemetry.battery_power_sign is None
    assert parsed.solis.telemetry.device_timestamp_entity_id is None
    assert len(parsed.solis.slots) == 6
    assert sum(
        1
        for slot in parsed.solis.slots
        for direction in (slot.charge, slot.discharge)
        for _ in (
            direction.enable_entity_id,
            direction.time_entity_id,
            direction.current_entity_id,
            direction.target_soc_entity_id,
        )
    ) == 48
    assert parsed.solis.slots[0].charge.owner is SolisSlotOwner.CHEAP_CHARGING
    assert parsed.solis.slots[0].discharge.owner is SolisSlotOwner.FULL_SOC_CYCLING
    assert parsed.solis.slots[1].discharge.owner is SolisSlotOwner.PRE_DISCHARGE


def test_wrong_domain_is_rejected() -> None:
    source = deployed()
    source["solis"]["persistent"]["storage_mode_entity_id"] = "switch.wrong_domain"

    with pytest.raises(ValueError, match="storage_mode_entity_id must be a select"):
        config.from_mapping(source)


def test_duplicate_entity_ids_are_rejected_globally() -> None:
    source = deployed()
    source["solis"]["capability"]["max_export_power_entity_id"] = (
        source["solis"]["capability"]["max_output_power_entity_id"]
    )

    with pytest.raises(ValueError, match="globally unique"):
        config.from_mapping(source)


@pytest.mark.parametrize(
    "change",
    (
        lambda source: source["solis"]["slots"].pop(),
        lambda source: source["solis"]["slots"][0].update(
            physical_slot=7
        ),
        lambda source: source["solis"]["slots"][0]["charge"].pop("current_entity_id"),
        lambda source: source["solis"]["slots"][0].update(
            physical_slot=source["solis"]["slots"][1]["physical_slot"]
        ),
    ),
)
def test_incomplete_duplicate_or_out_of_range_slots_are_rejected(change) -> None:
    source = deployed()
    change(source)

    with pytest.raises(ValueError):
        config.from_mapping(source)


def test_incorrect_slot_owner_is_rejected() -> None:
    source = deployed()
    source["solis"]["slots"][0]["discharge"]["owner"] = "reserved"

    with pytest.raises(ValueError, match="owner must be full_soc_cycling"):
        config.from_mapping(source)


def test_unresolved_power_facts_are_explicit_and_sign_is_restricted() -> None:
    source = deployed()
    source["solis"]["telemetry"]["battery_power_entity_id"] = (
        "sensor.garage_battery_power"
    )
    source["solis"]["telemetry"]["battery_power_sign"] = "positive_means_charging"

    parsed = config.from_mapping(source)

    assert parsed.solis.telemetry.battery_power_sign is BatteryPowerSign.POSITIVE_MEANS_CHARGING
    invalid = deepcopy(source)
    invalid["solis"]["telemetry"]["battery_power_sign"] = "unknown"
    with pytest.raises(ValueError, match="unknown power sign"):
        config.from_mapping(invalid)


@pytest.mark.parametrize(
    ("entity_id", "power_sign"),
    (
        ("sensor.garage_battery_power", None),
        (None, "positive_means_charging"),
    ),
)
def test_power_entity_and_sign_must_be_configured_as_a_pair(
    entity_id: str | None, power_sign: str | None
) -> None:
    source = deployed()
    source["solis"]["telemetry"]["battery_power_entity_id"] = entity_id
    source["solis"]["telemetry"]["battery_power_sign"] = power_sign

    with pytest.raises(ValueError, match="both be null or both be configured"):
        config.from_mapping(source)


@pytest.mark.parametrize("power_sign", ("charging", "discharging"))
def test_power_sign_rejects_undocumented_aliases(power_sign: str) -> None:
    source = deployed()
    source["solis"]["telemetry"]["battery_power_entity_id"] = (
        "sensor.garage_battery_power"
    )
    source["solis"]["telemetry"]["battery_power_sign"] = power_sign

    with pytest.raises(ValueError, match="unknown power sign"):
        config.from_mapping(source)


def test_grid_import_policy_is_a_literal_not_an_entity() -> None:
    source = deployed()
    source["solis"]["maximum_grid_import_policy"] = "number.grid_import_limit"

    with pytest.raises(ValueError, match="manual_commissioning"):
        config.from_mapping(source)


def test_legacy_stub_ids_are_rejected_inside_solis_map() -> None:
    source = deployed()
    source["solis"]["telemetry"]["state_of_charge_entity_id"] = (
        "input_number.house_battery_state_of_charge"
    )

    with pytest.raises(ValueError, match="legacy stub"):
        config.from_mapping(source)
