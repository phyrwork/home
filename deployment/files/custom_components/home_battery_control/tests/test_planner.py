"""Tests for battery planner input fusion."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.home_battery_control import battery, energy, planner, tariff
from custom_components.home_battery_control.interval import TimeInterval

NOW = datetime(2026, 7, 4, 10, 30, tzinfo=UTC)
HOUR = timedelta(hours=1)
TARIFF = tariff.Tariff(
    import_price_per_kwh=Decimal("0.30"),
    export_price_per_kwh=Decimal("0.12"),
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
