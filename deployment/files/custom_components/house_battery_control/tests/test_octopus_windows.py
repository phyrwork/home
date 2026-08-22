"""Pure tests for trusted Octopus rates and export-cycle windows."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.house_battery_control.octopus_windows import (
    AdjustedRateInterval,
    CheapClassification,
    CoverageStatus,
    DispatchSourceObservation,
    ExportRateInterval,
    RateSourceObservation,
    evaluate_cheap_windows,
    parse_fused_import_rates,
    parse_public_export_event,
    parse_public_import_event,
    value_for_stored_energy,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 4, 12, tzinfo=UTC)


def _event(*, values: tuple[str, ...] = ("0.07", "0.30"), adjusted: tuple[bool, ...] | None = None) -> dict:
    adjusted = adjusted or (False,) * len(values)
    return {
        "min_rate": min(values, key=Decimal),
        "tariff_code": "E-2R-TEST-A",
        "rates": [
            {
                "start": (NOW + timedelta(minutes=30 * index)).isoformat(),
                "end": (NOW + timedelta(minutes=30 * (index + 1))).isoformat(),
                "value_inc_vat": value,
                "is_capped": False,
                "is_intelligent_adjusted": is_adjusted,
            }
            for index, (value, is_adjusted) in enumerate(zip(values, adjusted, strict=True))
        ],
    }


def _export(values: tuple[str, ...] = ("0.15", "0.15")) -> tuple[ExportRateInterval, ...]:
    return parse_public_export_event(
        {
            "tariff_code": "E-EXPORT-TEST",
            "rates": [
                {
                    "start": (NOW + timedelta(minutes=30 * index)).isoformat(),
                    "end": (NOW + timedelta(minutes=30 * (index + 1))).isoformat(),
                    "value_inc_vat": value,
                    "is_capped": False,
                }
                for index, value in enumerate(values)
            ],
        },
        source="export-rates",
        source_event="export-current",
        retrieved_at=NOW,
    )


def _result(import_rates, export_rates=None, **kwargs):
    import_source = kwargs.pop("import_source", RateSourceObservation(NOW, "import-rates"))
    export_source = kwargs.pop("export_source", RateSourceObservation(NOW, "export-rates"))
    return evaluate_cheap_windows(
        import_rates=import_rates,
        export_rates=export_rates or _export(),
        start=NOW,
        end=NOW + timedelta(hours=1),
        now=NOW,
        import_source=import_source,
        export_source=export_source,
        **kwargs,
    )


def test_public_event_normalises_omitted_adjustment_and_classifies_two_rate_minimum() -> None:
    event = _event()
    event["rates"][0].pop("is_intelligent_adjusted")
    rates = parse_public_import_event(
        event,
        source="import-rates",
        source_day="current",
        source_event="current-day",
        source_revision_at=NOW,
    )
    assert rates[0].classification is CheapClassification.STANDARD_CHEAP
    assert rates[0].is_intelligent_adjusted is False
    assert rates[0].is_capped is False
    assert rates[0].import_price == Decimal("0.07")


def test_public_event_keeps_minimums_independent_between_days() -> None:
    first = parse_public_import_event(
        _event(values=("0.07", "0.30")),
        source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    second_event = _event(values=("0.08", "0.31"))
    second = parse_public_import_event(
        second_event,
        source="import-rates", source_day="next", source_event="next-day", source_revision_at=NOW,
    )
    assert first[0].classification is CheapClassification.STANDARD_CHEAP
    assert second[0].classification is CheapClassification.STANDARD_CHEAP
    assert first[0].event_minimum != second[0].event_minimum


@pytest.mark.parametrize("unit", ["pence/kWh", "GBP", "GBP/MWh"])
def test_pence_or_wrong_unit_is_rejected(unit: str) -> None:
    with pytest.raises(ValueError, match="unsupported rate unit"):
        parse_public_import_event(
            _event(), source="import-rates", source_day="current", source_event="current-day",
            source_revision_at=NOW, unit=unit,
        )


def test_fused_schema_requires_boolean_adjustment_and_rejects_legacy_record() -> None:
    record = {
        "start": NOW.isoformat(), "end": (NOW + timedelta(minutes=30)).isoformat(),
        "value_inc_vat": "0.07", "unit": "GBP/kWh", "classification": "STANDARD_CHEAP",
        "source": "import-rates", "source_event": "current-day", "source_day": "current",
        "tariff": "T", "source_revision_at": NOW.isoformat(), "event_min_rate": "0.07",
        "event_unique_price_count": 2,
    }
    with pytest.raises(ValueError, match="mandatory fields"):
        parse_fused_import_rates([record])
    record["is_intelligent_adjusted"] = None
    with pytest.raises(ValueError, match="Boolean"):
        parse_fused_import_rates([record])


def test_fused_schema_rejects_inconsistent_minimum_and_adjusted_rate() -> None:
    records = [
        {
            "start": NOW.isoformat(), "end": (NOW + timedelta(minutes=30)).isoformat(),
            "value_inc_vat": "0.08", "unit": "GBP/kWh", "is_intelligent_adjusted": True,
            "classification": "BONUS_DISPATCH", "source": "i", "source_event": "e",
            "source_day": "current", "tariff": "T", "source_revision_at": NOW.isoformat(),
            "event_min_rate": "0.07", "event_unique_price_count": 2,
        },
        {
            "start": (NOW + timedelta(minutes=30)).isoformat(), "end": (NOW + timedelta(minutes=60)).isoformat(),
            "value_inc_vat": "0.07", "unit": "GBP/kWh", "is_intelligent_adjusted": False,
            "classification": "STANDARD_CHEAP", "source": "i", "source_event": "e",
            "source_day": "current", "tariff": "T", "source_revision_at": NOW.isoformat(),
            "event_min_rate": "0.07", "event_unique_price_count": 2,
        },
    ]
    with pytest.raises(ValueError, match="adjusted rate"):
        parse_fused_import_rates(records)


def test_bonus_dispatch_at_minimum_is_explicit() -> None:
    rates = parse_public_import_event(
        _event(adjusted=(True, False)), source="import-rates", source_day="current",
        source_event="current-day", source_revision_at=NOW,
    )
    assert rates[0].classification is CheapClassification.BONUS_DISPATCH


def test_flat_tariff_is_not_cheap() -> None:
    rates = parse_public_import_event(
        _event(values=("0.07", "0.07", "0.07")), source="import-rates", source_day="current",
        source_event="current-day", source_revision_at=NOW,
    )
    assert all(rate.classification is CheapClassification.NOT_CHEAP for rate in rates)
    assert _result(rates).coverage_status is CoverageStatus.TRUSTED_EMPTY


def test_exact_cycle_margin_and_value() -> None:
    rates = parse_public_import_event(
        _event(), source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    result = _result(rates)
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.windows[0].components[0].margin_per_stored_kwh == (
        Decimal("0.15") * Decimal("0.95") - Decimal("0.07") / Decimal("0.95") - Decimal("0.0165")
    )
    assert value_for_stored_energy(result.windows[0].components[0].margin_per_stored_kwh, Decimal("2")) == (
        result.windows[0].components[0].margin_per_stored_kwh * 2
    )


@pytest.mark.parametrize("export_price", ["0.08", "0.07"])
def test_zero_or_negative_margin_is_trusted_empty(export_price: str) -> None:
    rates = parse_public_import_event(
        _event(), source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    result = _result(rates, _export((export_price, export_price)))
    assert result.coverage_status is CoverageStatus.TRUSTED_EMPTY
    assert not result.windows
    assert result.diagnostic_components


def test_adjacent_profitable_components_merge_without_averaging_prices() -> None:
    first = parse_public_import_event(
        _event(values=("0.07", "0.08")), source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    second = parse_public_import_event(
        _event(values=("0.08", "0.31")), source="import-rates", source_day="next", source_event="next-day", source_revision_at=NOW,
    )
    second = tuple(
        replace(item, start=item.start + timedelta(minutes=30), end=item.end + timedelta(minutes=30))
        for item in second
    )
    rates = (first[0],) + second[:1]
    result = _result(rates, _export(("0.15", "0.16")))
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert len(result.windows) == 1
    assert len(result.windows[0].components) == 2
    assert [item.rate_interval.import_price for item in result.windows[0].components] == [Decimal("0.07"), Decimal("0.08")]


def test_gaps_and_overlaps_never_expose_diagnostic_components_as_windows() -> None:
    rates = parse_public_import_event(
        _event(), source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    gapped = rates[:1] + (
        AdjustedRateInterval(
            start=NOW + timedelta(minutes=45), end=NOW + timedelta(hours=1), import_price=Decimal("0.30"),
            classification=CheapClassification.NOT_CHEAP, source="import-rates", tariff="T", source_day="current",
            source_event="current-day", source_revision_at=NOW,
        ),
    )
    result = _result(gapped)
    assert result.coverage_status is CoverageStatus.GAPPED
    assert not result.windows


def test_stale_and_future_sources_fail_closed() -> None:
    rates = parse_public_import_event(
        _event(), source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    stale = _result(rates, import_source=RateSourceObservation(NOW - timedelta(hours=27), "import-rates"))
    assert stale.coverage_status is CoverageStatus.UNAVAILABLE
    future = _result(rates, export_source=RateSourceObservation(NOW + timedelta(minutes=3), "export-rates"))
    assert future.coverage_status is CoverageStatus.UNAVAILABLE


def test_dispatch_freshness_required_only_for_actionable_bonus() -> None:
    rates = parse_public_import_event(
        _event(adjusted=(True, False)), source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    stale_dispatch = DispatchSourceObservation(NOW - timedelta(minutes=11), "dispatches")
    result = _result(rates, dispatch_source=stale_dispatch)
    assert result.coverage_status is CoverageStatus.UNAVAILABLE
    ordinary = parse_public_import_event(
        _event(), source="import-rates", source_day="current", source_event="current-day", source_revision_at=NOW,
    )
    result = _result(ordinary, dispatch_source=stale_dispatch)
    assert result.coverage_status is CoverageStatus.COMPLETE


def test_cross_midnight_and_dst_fold_are_compared_by_utc_instant() -> None:
    start = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    end = datetime(2026, 10, 25, 2, 30, tzinfo=UTC)
    import_rates = (
        AdjustedRateInterval(
            start=datetime.fromisoformat("2026-10-25T01:30:00+01:00"),
            end=datetime.fromisoformat("2026-10-25T01:30:00+00:00"),
            import_price=Decimal("0.07"), classification=CheapClassification.STANDARD_CHEAP,
            source="i", tariff="T", source_day="current", source_event="e", source_revision_at=NOW,
        ),
    )
    # The fold interval above is a real one-hour UTC interval even though the
    # local wall clock repeats 01:30; its exact UTC coverage is what matters.
    export_rates = (
        ExportRateInterval(
            start=datetime.fromisoformat("2026-10-25T01:30:00+01:00"),
            end=datetime.fromisoformat("2026-10-25T01:30:00+00:00"),
            export_price=Decimal("0.15"), source="e", tariff="E", retrieved_at=NOW,
        ),
    )
    result = evaluate_cheap_windows(
        import_rates=import_rates, export_rates=export_rates, start=start, end=end, now=NOW,
        import_source=RateSourceObservation(NOW, "i"), export_source=RateSourceObservation(NOW, "e"),
    )
    assert result.coverage_status is CoverageStatus.GAPPED
