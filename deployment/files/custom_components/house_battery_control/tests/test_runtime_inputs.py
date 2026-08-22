from decimal import Decimal

import pytest

from custom_components.house_battery_control.runtime_inputs import _runtime_powers
from custom_components.house_battery_control.solis_reader import read_solis_state
from custom_components.house_battery_control.tests.test_solis_reader import NOW, fixture


def test_runtime_power_is_derived_from_voltage_current_and_inverter_limit() -> None:
    parsed, states = fixture()
    result = read_solis_state(parsed, states, NOW)

    assert result.snapshot is not None
    assert _runtime_powers(result.snapshot) == (Decimal("5"), Decimal("5"))


def test_runtime_power_rejects_unknown_output_unit() -> None:
    parsed, states = fixture()
    result = read_solis_state(parsed, states, NOW)

    assert result.snapshot is not None
    object.__setattr__(result.snapshot.capabilities.maximum_output_power, "unit", "MW")
    with pytest.raises(ValueError, match="unsupported"):
        _runtime_powers(result.snapshot)
