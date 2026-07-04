"""Controller-level battery simulation tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.house_battery_control import battery, controller, planner, tariff
from custom_components.house_battery_control.interval import TimeInterval

from simulation import State, simulate

NOW = datetime(2026, 7, 4, 10, tzinfo=UTC)
PEAK = tariff.Tariff(Decimal("0.30"), Decimal("0.12"), False)
OFF_PEAK = tariff.Tariff(Decimal("0.07"), Decimal("0.12"), True)
SPEC = battery.Spec(
    capacity_kwh=Decimal("10"),
    minimum_energy_kwh=Decimal("2"),
    maximum_charge_power_kw=Decimal("6"),
    maximum_discharge_power_kw=Decimal("6"),
    charge_efficiency=Decimal("1"),
    discharge_efficiency=Decimal("1"),
)


def test_reserve_energy_can_supply_the_demand_it_protects() -> None:
    """Reserve for current demand must remain available to self-consumption."""
    result = _run(
        (_interval(load="4"),),
        initial_energy="6",
    )

    assert result[0].command == controller.SelfConsumption(
        minimum_energy_kwh=Decimal("2")
    )
    assert result[0].battery.energy_kwh == Decimal("2")
    assert result[0].grid_import_kwh == 0


def test_peak_demand_is_preserved_across_multiple_intervals() -> None:
    result = _run(
        (
            _interval(load="2"),
            _interval(load="3", start=NOW + timedelta(hours=1)),
        ),
        initial_energy="7",
    )

    assert [state.battery.energy_kwh for state in result] == [
        Decimal("5"),
        Decimal("2"),
    ]
    assert sum(state.peak_grid_import_kwh for state in result) == 0


def test_demand_above_discharge_power_is_unavoidable_import() -> None:
    result = _run((_interval(load="8"),), initial_energy="10")

    assert tuple(state.command for state in result) == (
        controller.ForceExport(target_energy_kwh=Decimal("8")),
        controller.SelfConsumption(minimum_energy_kwh=Decimal("2")),
    )
    assert result[0].interval.end == result[1].interval.start
    assert result[-1].battery.energy_kwh == Decimal("4")
    assert sum(state.grid_import_kwh for state in result) == Decimal("2")
    assert sum(state.peak_grid_import_kwh for state in result) == Decimal("2")


def test_off_peak_charges_at_power_limit_and_supplies_house() -> None:
    result = _run(
        (_interval(load="1", current_tariff=OFF_PEAK),),
        initial_energy="2",
    )

    assert result[0].command == controller.GridCharge(
        target_energy_kwh=Decimal("10")
    )
    assert result[0].battery.energy_kwh == Decimal("8")
    assert result[0].battery_power_kw == Decimal("6")
    assert result[0].grid_import_kwh == Decimal("7")
    assert result[0].peak_grid_import_kwh == 0


def test_off_peak_charge_is_reserved_for_following_peak_demand() -> None:
    result = _run(
        (
            _interval(current_tariff=OFF_PEAK),
            _interval(load="6", start=NOW + timedelta(hours=1)),
        ),
        initial_energy="2",
    )

    assert [state.battery.energy_kwh for state in result] == [
        Decimal("8"),
        Decimal("2"),
    ]
    assert sum(state.peak_grid_import_kwh for state in result) == 0


def test_full_battery_does_not_cycle_during_off_peak() -> None:
    result = _run(
        (_interval(load="1", current_tariff=OFF_PEAK),),
        initial_energy="10",
    )

    assert result[0].battery.energy_kwh == Decimal("10")
    assert result[0].grid_import_kwh == Decimal("1")
    assert result[0].grid_export_kwh == 0


def test_surplus_solar_charges_before_exporting() -> None:
    result = _run(
        (_interval(load="1", solar="9"),),
        initial_energy="2",
    )

    assert result[0].battery.energy_kwh == Decimal("8")
    assert result[0].grid_export_kwh == Decimal("2")


def test_export_hysteresis_prevents_export_at_boundary() -> None:
    result = _run(
        (_interval(),),
        initial_energy="3",
        hysteresis="1",
    )

    assert result[0].command == controller.SelfConsumption(
        minimum_energy_kwh=Decimal("2")
    )
    assert result[0].grid_export_kwh == 0


def test_energy_above_hysteresis_is_exported_to_reserve() -> None:
    result = _run(
        (_interval(),),
        initial_energy="3.1",
        hysteresis="1",
    )

    assert tuple(state.command for state in result) == (
        controller.ForceExport(target_energy_kwh=Decimal("2")),
        controller.SelfConsumption(minimum_energy_kwh=Decimal("2")),
    )
    assert result[-1].battery.energy_kwh == Decimal("2")
    assert sum(state.grid_export_kwh for state in result) == Decimal("1.1")


def test_reserve_margin_is_retained_after_serving_peak_demand() -> None:
    result = _run(
        (_interval(load="4"),),
        initial_energy="7",
        margin="1",
    )

    assert result[0].battery.energy_kwh == Decimal("3")
    assert result[0].grid_import_kwh == 0


def test_efficiency_is_applied_to_discharge_and_cost_metrics() -> None:
    spec = battery.Spec(
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("2"),
        maximum_charge_power_kw=Decimal("6"),
        maximum_discharge_power_kw=Decimal("6"),
        charge_efficiency=Decimal("0.8"),
        discharge_efficiency=Decimal("0.8"),
    )
    result = _run(
        (_interval(load="5"),),
        initial_energy="8.25",
        spec=spec,
    )

    assert result[0].battery.energy_kwh == Decimal("2")
    assert result[0].grid_import_kwh == 0
    assert result[0].grid_import_cost == 0


def test_import_cost_and_export_revenue_use_interval_tariff() -> None:
    imported = _run((_interval(load="8"),), initial_energy="10")
    exported = _run((_interval(),), initial_energy="4", hysteresis="1")

    assert sum(state.grid_import_cost for state in imported) == Decimal("0.60")
    assert sum(state.grid_export_revenue for state in exported) == Decimal("0.24")


def test_simulation_rejects_discontinuous_forecast() -> None:
    with pytest.raises(ValueError, match="contiguous and ordered"):
        _run(
            (
                _interval(),
                _interval(start=NOW + timedelta(hours=2)),
            ),
            initial_energy="2",
        )


def _run(
    intervals: tuple[planner.InputInterval, ...],
    *,
    initial_energy: str,
    spec: battery.Spec = SPEC,
    margin: str = "0",
    hysteresis: str = "1",
) -> tuple[State, ...]:
    return tuple(
        simulate(
            spec=spec,
            initial_state=battery.State(Decimal(initial_energy)),
            intervals=intervals,
            reserve_margin_kwh=Decimal(margin),
            export_hysteresis_kwh=Decimal(hysteresis),
        )
    )


def _interval(
    *,
    load: str = "0",
    solar: str = "0",
    current_tariff: tariff.Tariff = PEAK,
    start: datetime = NOW,
) -> planner.InputInterval:
    return planner.InputInterval(
        interval=TimeInterval(start, start + timedelta(hours=1)),
        load_kwh=Decimal(load),
        solar_kwh=Decimal(solar),
        tariff=current_tariff,
    )
