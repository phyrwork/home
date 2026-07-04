"""Tests for battery planning."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.house_battery_control import battery, energy, planner, tariff
from custom_components.house_battery_control.interval import TimeInterval

NOW = datetime(2026, 7, 4, 10, 30, tzinfo=UTC)
HOUR = timedelta(hours=1)
TARIFF = tariff.Tariff(
    import_price_per_kwh=Decimal("0.30"),
    export_price_per_kwh=Decimal("0.12"),
    import_price_is_off_peak=False,
)
SPEC = battery.Spec(
    capacity_kwh=Decimal("32"),
    minimum_energy_kwh=Decimal("3.2"),
    maximum_charge_power_kw=Decimal("6"),
    maximum_discharge_power_kw=Decimal("6"),
    charge_efficiency=Decimal("0.95"),
    discharge_efficiency=Decimal("0.95"),
)


def test_fuse_aligns_boundaries_and_prorates_energy() -> None:
    source = planner.Input(
        now=NOW,
        battery_spec=SPEC,
        battery_state=battery.State(energy_kwh=Decimal("16")),
        tariff_forecast=(
            tariff.TariffInterval(
                interval=TimeInterval(NOW - HOUR, NOW + HOUR),
                tariff=TARIFF,
            ),
        ),
        load_forecast=(
            energy.EnergyInterval(
                interval=TimeInterval(NOW - timedelta(minutes=30), NOW + HOUR),
                energy_kwh=Decimal("3"),
            ),
        ),
        solar_forecast=(
            energy.EnergyInterval(
                interval=TimeInterval(NOW + timedelta(minutes=30), NOW + HOUR),
                energy_kwh=Decimal("0.5"),
            ),
        ),
    )

    result = planner.fuse_forecasts(
        now=source.now,
        tariff_forecast=source.tariff_forecast,
        load_forecast=source.load_forecast,
        solar_forecast=source.solar_forecast,
    )

    assert [item.interval for item in result] == [
        TimeInterval(NOW, NOW + timedelta(minutes=30)),
        TimeInterval(NOW + timedelta(minutes=30), NOW + HOUR),
    ]
    assert [item.load_kwh for item in result] == [Decimal("1"), Decimal("1")]
    assert [item.solar_kwh for item in result] == [Decimal("0"), Decimal("0.5")]


def test_fuse_rejects_load_forecast_gaps() -> None:
    source = planner.Input(
        now=NOW,
        battery_spec=SPEC,
        battery_state=battery.State(energy_kwh=Decimal("16")),
        tariff_forecast=(
            tariff.TariffInterval(
                interval=TimeInterval(NOW, NOW + HOUR),
                tariff=TARIFF,
            ),
        ),
        load_forecast=(
            energy.EnergyInterval(
                interval=TimeInterval(NOW, NOW + timedelta(minutes=30)),
                energy_kwh=Decimal("1"),
            ),
            energy.EnergyInterval(
                interval=TimeInterval(
                    NOW + timedelta(minutes=45),
                    NOW + HOUR,
                ),
                energy_kwh=Decimal("0.5"),
            ),
        ),
        solar_forecast=(),
    )

    with pytest.raises(ValueError, match="Load forecast must cover"):
        planner.fuse_forecasts(
            now=source.now,
            tariff_forecast=source.tariff_forecast,
            load_forecast=source.load_forecast,
            solar_forecast=source.solar_forecast,
        )


def test_fuse_rejects_overlapping_tariffs() -> None:
    interval = TimeInterval(NOW, NOW + HOUR)
    source = planner.Input(
        now=NOW,
        battery_spec=SPEC,
        battery_state=battery.State(energy_kwh=Decimal("16")),
        tariff_forecast=(
            tariff.TariffInterval(interval=interval, tariff=TARIFF),
            tariff.TariffInterval(interval=interval, tariff=TARIFF),
        ),
        load_forecast=(
            energy.EnergyInterval(interval=interval, energy_kwh=Decimal("2")),
        ),
        solar_forecast=(),
    )

    with pytest.raises(ValueError, match="Tariff must cover"):
        planner.fuse_forecasts(
            now=source.now,
            tariff_forecast=source.tariff_forecast,
            load_forecast=source.load_forecast,
            solar_forecast=source.solar_forecast,
        )


def test_reserve_calculates_requirement_backward_from_horizon() -> None:
    spec = battery.Spec(
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("2"),
        maximum_charge_power_kw=Decimal("4"),
        maximum_discharge_power_kw=Decimal("6"),
        charge_efficiency=Decimal("1"),
        discharge_efficiency=Decimal("1"),
    )
    intervals = (
        _input_interval(load_kwh="3", solar_kwh="0"),
        _input_interval(
            load_kwh="0",
            solar_kwh="0",
            off_peak=True,
            start=NOW + HOUR,
        ),
        _input_interval(
            load_kwh="7",
            solar_kwh="0",
            start=NOW + 2 * HOUR,
        ),
    )

    result = planner.reserve(
        spec=spec,
        intervals=intervals,
        reserve_margin_kwh=Decimal(),
    )

    assert result == Decimal("7")


def test_reserve_intervals_describe_required_energy_at_each_boundary() -> None:
    spec = battery.Spec(
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("2"),
        maximum_charge_power_kw=Decimal("6"),
        maximum_discharge_power_kw=Decimal("6"),
        charge_efficiency=Decimal("1"),
        discharge_efficiency=Decimal("1"),
    )
    intervals = (
        _input_interval(load_kwh="4", solar_kwh="0"),
        _input_interval(
            load_kwh="2",
            solar_kwh="0",
            start=NOW + HOUR,
        ),
    )

    result = planner.reserve_intervals(
        spec=spec,
        intervals=intervals,
        reserve_margin_kwh=Decimal(),
    )

    assert [
        (item.start_energy_kwh, item.end_energy_kwh)
        for item in result
    ] == [
        (Decimal("8"), Decimal("4")),
        (Decimal("4"), Decimal("2")),
    ]


def test_reserve_accounts_for_efficiency_and_solar_charging() -> None:
    intervals = (
        _input_interval(load_kwh="0", solar_kwh="2"),
        _input_interval(
            load_kwh="5",
            solar_kwh="0",
            start=NOW + HOUR,
        ),
    )

    result = planner.reserve(
        spec=SPEC,
        intervals=intervals,
        reserve_margin_kwh=Decimal(),
    )

    assert result == (
        SPEC.minimum_energy_kwh
        + Decimal("5") / SPEC.discharge_efficiency
        - Decimal("2") * SPEC.charge_efficiency
    )


def test_reserve_excludes_demand_above_discharge_power_limit() -> None:
    intervals = (
        _input_interval(load_kwh="10", solar_kwh="0"),
        _input_interval(
            load_kwh="30",
            solar_kwh="0",
            start=NOW + HOUR,
        ),
    )

    result = planner.reserve(
        spec=SPEC,
        intervals=intervals,
        reserve_margin_kwh=Decimal(),
    )

    assert result == (
        SPEC.minimum_energy_kwh
        + 2 * SPEC.maximum_discharge_power_kw / SPEC.discharge_efficiency
    )


def test_reserve_clamps_energy_shortfall_to_capacity() -> None:
    spec = battery.Spec(
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("2"),
        maximum_charge_power_kw=Decimal("6"),
        maximum_discharge_power_kw=Decimal("20"),
        charge_efficiency=Decimal("1"),
        discharge_efficiency=Decimal("1"),
    )
    intervals = (
        _input_interval(load_kwh="6", solar_kwh="0"),
        _input_interval(
            load_kwh="6",
            solar_kwh="0",
            start=NOW + HOUR,
        ),
    )

    result = planner.reserve(
        spec=spec,
        intervals=intervals,
        reserve_margin_kwh=Decimal(),
    )

    assert result == spec.capacity_kwh


def test_reserve_rejects_discontinuous_intervals() -> None:
    intervals = (
        _input_interval(load_kwh="0", solar_kwh="0"),
        _input_interval(
            load_kwh="0",
            solar_kwh="0",
            start=NOW + 2 * HOUR,
        ),
    )

    with pytest.raises(ValueError, match="contiguous and ordered"):
        planner.reserve(
            spec=SPEC,
            intervals=intervals,
            reserve_margin_kwh=Decimal(),
        )


def test_reserve_adds_margin_and_clamps_to_capacity() -> None:
    intervals = (_input_interval(load_kwh="1", solar_kwh="0"),)

    result = planner.reserve(
        spec=SPEC,
        intervals=intervals,
        reserve_margin_kwh=Decimal("2"),
    )
    clamped = planner.reserve(
        spec=SPEC,
        intervals=intervals,
        reserve_margin_kwh=SPEC.capacity_kwh,
    )

    assert result == (
        SPEC.minimum_energy_kwh
        + Decimal("1") / SPEC.discharge_efficiency
        + Decimal("2")
    )
    assert clamped == SPEC.capacity_kwh


def test_reserve_rejects_negative_margin() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        planner.reserve(
            spec=SPEC,
            intervals=(),
            reserve_margin_kwh=Decimal("-1"),
        )


def _input_interval(
    *,
    load_kwh: str,
    solar_kwh: str,
    off_peak: bool = False,
    start: datetime = NOW,
) -> planner.InputInterval:
    interval_tariff = tariff.Tariff(
        import_price_per_kwh=TARIFF.import_price_per_kwh,
        export_price_per_kwh=TARIFF.export_price_per_kwh,
        import_price_is_off_peak=off_peak,
    )
    return planner.InputInterval(
        interval=TimeInterval(start, start + HOUR),
        load_kwh=Decimal(load_kwh),
        solar_kwh=Decimal(solar_kwh),
        tariff=interval_tariff,
    )
