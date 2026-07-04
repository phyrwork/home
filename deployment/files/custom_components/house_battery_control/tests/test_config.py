from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control import CONFIG_SCHEMA
from custom_components.house_battery_control import config
from custom_components.house_battery_control.const import DOMAIN


def source() -> dict[str, object]:
    return {
        "battery": {
            "capacity_kwh": 32.1536,
            "minimum_state_of_charge_percent": 10,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "state_of_charge_entity_id": "input_number.house_battery_state_of_charge",
            "power_limit_entity_id": "input_number.house_battery_power_limit",
        },
        "tariff": {
            "import_price_entity_id": "sensor.import_price",
            "export_price_entity_id": "sensor.export_price",
        },
        "solar": {
            "config_entry_id": "forecast-solar-entry",
        },
        "policy": {
            "reserve_margin_entity_id": "input_number.reserve_margin",
            "export_hysteresis_entity_id": "input_number.export_hysteresis",
        },
        "inverter": {
            "operating_mode_entity_id": "input_select.operating_mode",
            "state_of_charge_target_entity_id": "input_number.state_of_charge_target",
        },
    }


def test_maps_yaml_values_without_float_artifacts() -> None:
    result = config.from_mapping(source())

    assert result.battery.capacity_kwh == Decimal("32.1536")
    assert result.battery.minimum_state_of_charge_percent == Decimal("10")
    assert result.battery.charge_efficiency == Decimal("0.95")


def test_deployed_yaml_maps_to_typed_config() -> None:
    config_path = Path(__file__).parents[3] / "house_battery_control.yaml"
    deployed = yaml.safe_load(config_path.read_text())

    result = config.from_mapping(deployed)

    assert result.battery.capacity_kwh == Decimal("32.1536")
    assert result.solar.config_entry_id == "79c4415a9ea776404127c1b61ba240cf"


def test_home_assistant_schema_returns_typed_config() -> None:
    validated = CONFIG_SCHEMA({DOMAIN: source()})

    assert isinstance(validated[DOMAIN], config.Config)


def test_builds_spec_with_derived_floor_and_live_power_limit() -> None:
    result = config.from_mapping(source())

    spec = result.battery.to_spec(Decimal("6"))

    assert spec.capacity_kwh == Decimal("32.1536")
    assert spec.minimum_energy_kwh == Decimal("3.21536")
    assert spec.maximum_charge_power_kw == Decimal("6")
    assert spec.maximum_discharge_power_kw == Decimal("6")


@pytest.mark.parametrize(
    ("section", "key", "value"),
    (
        ("battery", "capacity_kwh", 0),
        ("battery", "minimum_state_of_charge_percent", 100),
        ("battery", "charge_efficiency", 1.01),
        ("battery", "state_of_charge_entity_id", "not_an_entity"),
    ),
)
def test_rejects_invalid_values(section: str, key: str, value: object) -> None:
    invalid = source()
    nested = invalid[section]
    assert isinstance(nested, dict)
    nested[key] = value

    with pytest.raises(ValueError):
        config.from_mapping(invalid)


def test_rejects_unknown_keys() -> None:
    invalid = source()
    invalid["surprise"] = True

    with pytest.raises(ValueError, match="unknown keys: surprise"):
        config.from_mapping(invalid)
