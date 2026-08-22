from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.solis_config import BatteryPowerSign, SolisSlotOwner


def deployed() -> dict[str, object]:
    value = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    assert isinstance(value, dict)
    return value


def test_live_mapping_has_real_entities_and_six_physical_slots() -> None:
    parsed = config.from_mapping(deployed()).solis

    assert parsed.telemetry.battery_power_sign is BatteryPowerSign.POSITIVE_MEANS_CHARGING
    assert len(parsed.slots) == 6
    assert parsed.slots[0].charge.owner is SolisSlotOwner.CHEAP_CHARGING
    assert parsed.slots[1].discharge.owner is SolisSlotOwner.PRE_DISCHARGE


def test_slot_mapping_is_strict() -> None:
    source = deployed()
    source["solis"]["slots"].pop()  # type: ignore[index]
    with pytest.raises(ValueError, match="exactly six"):
        config.from_mapping(source)


def test_global_entity_duplicates_are_rejected() -> None:
    source = deployed()
    source["solis"]["capability"]["max_export_power_entity_id"] = source["solis"]["capability"]["max_output_power_entity_id"]  # type: ignore[index]
    with pytest.raises(ValueError, match="globally unique"):
        config.from_mapping(source)


def test_power_sign_is_explicit() -> None:
    source = deepcopy(deployed())
    source["solis"]["telemetry"]["battery_power_sign"] = "unknown"  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown power sign"):
        config.from_mapping(source)
