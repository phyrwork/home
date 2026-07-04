"""Tests for Octopus Energy dependency mappings."""

from custom_components.house_battery_control.dependencies import octopus_energy


def test_to_tariff_intervals_classifies_rates_below_maximum_as_off_peak() -> None:
    result = octopus_energy.to_tariff_intervals(
        rates=(
            {
                "start": "2026-07-04T00:00:00+01:00",
                "end": "2026-07-04T00:30:00+01:00",
                "value_inc_vat": "0.07",
            },
            {
                "start": "2026-07-04T00:30:00+01:00",
                "end": "2026-07-04T01:00:00+01:00",
                "value_inc_vat": "0.30",
            },
        ),
        export_price_per_kwh="0.12",
    )

    assert [item.tariff.import_price_is_off_peak for item in result] == [True, False]


def test_to_tariff_intervals_treats_single_rate_as_peak() -> None:
    result = octopus_energy.to_tariff_intervals(
        rates=(
            {
                "start": "2026-07-04T00:00:00+01:00",
                "end": "2026-07-04T00:30:00+01:00",
                "value_inc_vat": "0.30",
            },
        ),
        export_price_per_kwh="0.12",
    )

    assert not result[0].tariff.import_price_is_off_peak
