from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control import config
from custom_components.house_battery_control.contracts import ControllerHealth, SlotDirection
from custom_components.house_battery_control.solis_reader import read_solis_state
from custom_components.house_battery_control.solis_state import MAXIMUM_TELEMETRY_AGE


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


def deployed() -> dict[str, object]:
    value = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    assert isinstance(value, dict)
    return value


def state(value: object, *, unit: str | None = None, updated: datetime = NOW) -> dict[str, object]:
    attributes: dict[str, object] = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return {"state": value, "attributes": attributes, "last_updated": updated}


def capability(value: str, unit: str, *, maximum: str = "100") -> dict[str, object]:
    return state(value, unit=unit) | {"attributes": {"min": "0", "max": maximum, "step": "1", "unit_of_measurement": unit}}


def fixture():
    source = deployed()
    parsed = config.from_mapping(source).solis
    states: dict[str, object] = {}
    telemetry = parsed.telemetry
    states[telemetry.state_of_charge_entity_id] = state("55", unit="%")
    states[telemetry.battery_power_entity_id] = state("-288", unit="W")
    states[telemetry.battery_voltage_entity_id] = state("51.2", unit="V")
    states[telemetry.device_timestamp_entity_id] = state(str(NOW.timestamp()))

    persistent = parsed.persistent
    states[persistent.storage_mode_entity_id] = {"state": "Feed-In Priority", "attributes": {"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]}}
    states[persistent.allow_grid_charging_entity_id] = state("on")
    states[persistent.inverter_time_entity_id] = state(NOW.isoformat())

    protection = parsed.protection
    for entity_id in (protection.battery_reserve_soc_entity_id,):
        states[entity_id] = capability("10", "%")
    states[protection.battery_reserve_entity_id] = state("off")

    capability_config = parsed.capability
    states[capability_config.battery_max_charge_current_entity_id] = capability("100", "A", maximum="100")
    states[capability_config.battery_max_discharge_current_entity_id] = capability("100", "A", maximum="100")
    for slot in parsed.slots:
        for direction in (slot.charge, slot.discharge):
            states[direction.enable_entity_id] = state("off")
            states[direction.time_entity_id] = state("00:00-00:00")
            states[direction.current_entity_id] = capability("100", "A", maximum="100")
            states[direction.target_soc_entity_id] = capability("50", "%")
    return parsed, states


def test_reader_accepts_numeric_epoch_and_normalizes_signed_watts() -> None:
    parsed, states = fixture()
    result = read_solis_state(parsed, states, NOW)

    assert result.health is ControllerHealth.HEALTHY
    assert result.snapshot is not None
    assert result.snapshot.telemetry.device_timestamp == datetime.fromtimestamp(NOW.timestamp(), timezone.utc)
    assert result.snapshot.telemetry.battery_power_kw == Decimal("-0.288")
    assert result.snapshot.telemetry.battery_voltage_v == Decimal("51.2")
    assert result.snapshot.slots[0].charge.direction is SlotDirection.CHARGE


def test_stale_numeric_epoch_is_degraded() -> None:
    parsed, states = fixture()
    states[parsed.telemetry.device_timestamp_entity_id] = state(str((NOW - MAXIMUM_TELEMETRY_AGE - timedelta(seconds=1)).timestamp()))

    result = read_solis_state(parsed, states, NOW)
    assert result.health is ControllerHealth.DEGRADED
    assert any(issue.code == "device_timestamp_stale" for issue in result.issues)


def test_numeric_epoch_at_maximum_age_is_healthy() -> None:
    parsed, states = fixture()
    states[parsed.telemetry.device_timestamp_entity_id] = state(
        str((NOW - MAXIMUM_TELEMETRY_AGE).timestamp())
    )

    result = read_solis_state(parsed, states, NOW)
    assert result.health is ControllerHealth.HEALTHY
    assert not any(issue.code == "device_timestamp_stale" for issue in result.issues)


def test_unknown_power_unit_is_degraded() -> None:
    parsed, states = fixture()
    states[parsed.telemetry.battery_power_entity_id] = state("1", unit="MW")

    result = read_solis_state(parsed, states, NOW)
    assert result.health is ControllerHealth.DEGRADED
    assert any(issue.code == "battery_power_unit_unknown" for issue in result.issues)


def test_reader_extrapolates_sampled_inverter_clock() -> None:
    parsed, states = fixture()
    sampled_at = NOW - timedelta(minutes=15)
    states[parsed.persistent.inverter_time_entity_id] = state(
        (sampled_at + timedelta(seconds=30)).isoformat(), updated=sampled_at
    )

    result = read_solis_state(parsed, states, NOW)

    assert result.health is ControllerHealth.HEALTHY
    assert result.snapshot is not None
    assert result.snapshot.persistent.inverter_time == NOW + timedelta(seconds=30)


@pytest.mark.parametrize("missing", (True, False))
def test_missing_or_naive_inverter_clock_observation_timestamp_is_degraded(missing: bool) -> None:
    parsed, states = fixture()
    inverter_state = state(NOW.isoformat())
    if missing:
        inverter_state.pop("last_updated")
    else:
        inverter_state["last_updated"] = datetime(2026, 8, 22, 12)
    states[parsed.persistent.inverter_time_entity_id] = inverter_state

    result = read_solis_state(parsed, states, NOW)

    assert result.health is ControllerHealth.DEGRADED
    assert any(issue.code == ("ha_last_updated_missing" if missing else "ha_last_updated_naive") for issue in result.issues)
