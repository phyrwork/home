from decimal import Decimal

from custom_components.house_battery_control import battery, controller
from custom_components.house_battery_control.dependencies import solis_cloud


def spec() -> battery.Spec:
    return battery.Spec(
        capacity_kwh=Decimal("32"),
        minimum_energy_kwh=Decimal("3.2"),
        maximum_charge_power_kw=Decimal("6"),
        maximum_discharge_power_kw=Decimal("6"),
        charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"),
    )


def test_rounds_target_state_of_charge_up_to_protect_reserve() -> None:
    result = solis_cloud.to_control(
        controller.SelfConsumption(minimum_energy_kwh=Decimal("3.21")),
        spec(),
    )

    assert result.target_state_of_charge_percent == Decimal("11")


def test_maps_grid_charge_to_full_power_control() -> None:
    result = solis_cloud.to_control(
        controller.GridCharge(target_energy_kwh=Decimal("32")),
        spec(),
    )

    assert result == solis_cloud.Control(
        operating_mode=solis_cloud.OperatingMode.GRID_CHARGE,
        target_state_of_charge_percent=Decimal("100"),
        power_w=Decimal("6000"),
    )
