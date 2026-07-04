"""Tests for battery controller decisions."""

from decimal import Decimal

import pytest

from custom_components.house_battery_control import battery, controller, tariff

SPEC = battery.Spec(
    capacity_kwh=Decimal("32"),
    minimum_energy_kwh=Decimal("3.2"),
    maximum_charge_power_kw=Decimal("6"),
    maximum_discharge_power_kw=Decimal("6"),
    charge_efficiency=Decimal("0.95"),
    discharge_efficiency=Decimal("0.95"),
)
PEAK = tariff.Tariff(
    import_price_per_kwh=Decimal("0.30"),
    export_price_per_kwh=Decimal("0.12"),
    import_price_is_off_peak=False,
)
OFF_PEAK = tariff.Tariff(
    import_price_per_kwh=Decimal("0.07"),
    export_price_per_kwh=Decimal("0.12"),
    import_price_is_off_peak=True,
)
RESERVE = Decimal("10")
HYSTERESIS = Decimal("1")


def test_select_command_charges_to_capacity_throughout_off_peak() -> None:
    result = _select(
        energy_kwh=SPEC.capacity_kwh,
        current_tariff=OFF_PEAK,
        previous_command=controller.ForceExport(target_energy_kwh=RESERVE),
    )

    assert result == controller.GridCharge(target_energy_kwh=SPEC.capacity_kwh)


def test_select_command_starts_export_above_hysteresis() -> None:
    result = _select(energy_kwh=Decimal("11.1"))

    assert result == controller.ForceExport(target_energy_kwh=RESERVE)


def test_select_command_does_not_start_export_at_hysteresis_boundary() -> None:
    result = _select(energy_kwh=Decimal("11"))

    assert result == controller.SelfConsumption(minimum_energy_kwh=RESERVE)


def test_select_command_continues_export_to_reserve_once_started() -> None:
    result = _select(
        energy_kwh=Decimal("10.5"),
        previous_command=controller.ForceExport(target_energy_kwh=RESERVE),
    )

    assert result == controller.ForceExport(target_energy_kwh=RESERVE)


def test_select_command_stops_export_at_reserve() -> None:
    result = _select(
        energy_kwh=RESERVE,
        previous_command=controller.ForceExport(target_energy_kwh=RESERVE),
    )

    assert result == controller.SelfConsumption(minimum_energy_kwh=RESERVE)


@pytest.mark.parametrize(
    ("reserve_energy_kwh", "export_hysteresis_kwh", "message"),
    (
        (Decimal("2"), HYSTERESIS, "within battery limits"),
        (RESERVE, Decimal("-1"), "must not be negative"),
    ),
)
def test_select_command_rejects_invalid_policy(
    reserve_energy_kwh: Decimal,
    export_hysteresis_kwh: Decimal,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        controller.select_command(
            spec=SPEC,
            state=battery.State(energy_kwh=Decimal("16")),
            tariff=PEAK,
            reserve_energy_kwh=reserve_energy_kwh,
            export_hysteresis_kwh=export_hysteresis_kwh,
            previous_command=None,
        )


def _select(
    *,
    energy_kwh: Decimal,
    current_tariff: tariff.Tariff = PEAK,
    previous_command: controller.Command | None = None,
) -> controller.Command:
    return controller.select_command(
        spec=SPEC,
        state=battery.State(energy_kwh=energy_kwh),
        tariff=current_tariff,
        reserve_energy_kwh=RESERVE,
        export_hysteresis_kwh=HYSTERESIS,
        previous_command=previous_command,
    )
