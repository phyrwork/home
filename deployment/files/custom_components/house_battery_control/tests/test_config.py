from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control import CONFIG_SCHEMA, config
from custom_components.house_battery_control.const import DOMAIN


def deployed() -> dict[str, object]:
    path = Path(__file__).parents[3] / "house_battery_control.yaml"
    value = yaml.safe_load(path.read_text())
    assert isinstance(value, dict)
    return value


def test_deployed_mapping_is_strict_and_decimal() -> None:
    parsed = config.from_mapping(deployed())

    assert parsed.battery.capacity_kwh == Decimal("32.1536")
    assert parsed.battery.minimum_soc_percent == Decimal("10")
    assert parsed.battery.minimum_energy_kwh == Decimal("3.21536")
    assert parsed.solis.telemetry.battery_power_entity_id.endswith("battery_power")


def test_home_assistant_schema_returns_typed_config() -> None:
    validated = CONFIG_SCHEMA({DOMAIN: deployed()})
    assert isinstance(validated[DOMAIN], config.Config)


@pytest.mark.parametrize(
    ("path", "value"),
    (("battery.capacity_kwh", 0), ("battery.minimum_soc_percent", 20)),
)
def test_invalid_safety_or_control_values_are_rejected(path: str, value: object) -> None:
    source = deployed()
    if "." in path:
        section, key = path.split(".", 1)
        source[section][key] = value  # type: ignore[index]
    else:
        source[path] = value
    with pytest.raises(ValueError):
        config.from_mapping(source)


def test_unknown_top_level_keys_are_rejected() -> None:
    source = deployed()
    source["legacy_stub"] = True
    with pytest.raises(ValueError, match="unknown keys"):
        config.from_mapping(source)
