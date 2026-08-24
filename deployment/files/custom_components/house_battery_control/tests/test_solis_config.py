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
    assert parsed.slots[1].discharge.owner is SolisSlotOwner.RESERVE_EXPORT

    for slot in range(1, 7):
        for direction in ("charge", "discharge"):
            item = getattr(parsed.slots[slot - 1], direction)
            base = f"garage_inverter_control_slot{slot}_{direction}"
            assert item.enable_entity_id == f"switch.{base}"
            assert item.time_entity_id == f"text.{base}_time"
            assert item.current_entity_id == f"number.{base}_current"
            assert item.target_soc_entity_id == f"number.{base}_soc"


def test_compact_mapping_has_expected_owner_allocations() -> None:
    parsed = config.from_mapping(deployed()).solis
    owners = {
        (slot.physical_slot, "charge"): slot.charge.owner
        for slot in parsed.slots
    }
    owners.update(
        {(slot.physical_slot, "discharge"): slot.discharge.owner for slot in parsed.slots}
    )
    assert [key for key, owner in owners.items() if owner is SolisSlotOwner.CHEAP_CHARGING] == [
        (1, "charge"), (2, "charge")
    ]
    assert [key for key, owner in owners.items() if owner is SolisSlotOwner.FULL_SOC_CYCLING] == [
        (1, "discharge"), (3, "discharge")
    ]
    assert [key for key, owner in owners.items() if owner is SolisSlotOwner.RESERVE_EXPORT] == [
        (2, "discharge"), (4, "discharge")
    ]


def test_slot_mapping_is_strict() -> None:
    source = deployed()
    source["solis"]["slot_allocations"].pop("reserve_export")  # type: ignore[index]
    with pytest.raises(ValueError, match="exactly the three owners"):
        config.from_mapping(source)


def test_global_entity_duplicates_are_rejected() -> None:
    source = deployed()
    source["solis"]["capability"]["battery_max_discharge_current_entity_id"] = source["solis"]["capability"]["battery_max_charge_current_entity_id"]  # type: ignore[index]
    with pytest.raises(ValueError, match="globally unique"):
        config.from_mapping(source)


def test_power_sign_is_explicit() -> None:
    source = deepcopy(deployed())
    source["solis"]["telemetry"]["battery_power_sign"] = "unknown"  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown power sign"):
        config.from_mapping(source)
