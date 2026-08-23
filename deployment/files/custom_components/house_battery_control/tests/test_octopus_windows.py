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
    evaluate_trusted_import_rates,
    parse_fused_export_rates,
    parse_fused_import_rates,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 4, 12, tzinfo=UTC)
IMPORT_SOURCE = "sensor.octopus_energy_import_rates_data_last_retrieved"
EXPORT_SOURCE = "sensor.octopus_energy_export_rates_data_last_retrieved"
DISPATCH_SOURCE = "sensor.octopus_energy_device_intelligent_dispatches_data_last_retrieved"


def _export(values: tuple[str, ...] = ("0.15", "0.15")) -> tuple[ExportRateInterval, ...]:
    return tuple(
        ExportRateInterval(
            start=NOW + timedelta(minutes=30 * index),
            end=NOW + timedelta(minutes=30 * (index + 1)),
            export_price=Decimal(value), source="export-current", tariff="E-EXPORT-TEST",
            retrieved_at=NOW, source_day="current", source_event="export-current",
            source_revision_at=NOW, retrieval_source_entity_id=EXPORT_SOURCE, is_capped=False,
        )
        for index, value in enumerate(values)
    )


def _import_rates(
    *items: tuple[str, CheapClassification, bool],
    source: str = "current-day",
    source_day: str = "current",
    source_event: str | None = None,
    source_revision_at: datetime = NOW,
    retrieval_source_entity_id: str = IMPORT_SOURCE,
    dispatch_source_entity_id: str = DISPATCH_SOURCE,
) -> tuple[AdjustedRateInterval, ...]:
    source_event = source_event or source
    values = [Decimal(value) for value, _classification, _adjusted in items]
    minimum = min(values)
    unique_count = len(set(values))
    return tuple(
        AdjustedRateInterval(
            start=NOW + timedelta(minutes=30 * index),
            end=NOW + timedelta(minutes=30 * (index + 1)),
            import_price=price,
            classification=classification,
            source=source, tariff="TEST", source_day=source_day,
            source_event=source_event, source_revision_at=source_revision_at,
            retrieval_source_entity_id=retrieval_source_entity_id,
            dispatch_source_entity_id=dispatch_source_entity_id, event_minimum=minimum,
            event_unique_price_count=unique_count,
            is_intelligent_adjusted=adjusted, is_capped=False,
        )
        for index, ((value, classification, adjusted), price) in enumerate(zip(items, values, strict=True))
    )


def _result(import_rates, export_rates=None, **kwargs):
    import_source = kwargs.pop("import_source", RateSourceObservation(NOW, IMPORT_SOURCE))
    export_source = kwargs.pop("export_source", RateSourceObservation(NOW, EXPORT_SOURCE))
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


def test_fused_schema_requires_boolean_adjustment_and_rejects_legacy_record() -> None:
    record = {
        "start": NOW.isoformat(), "end": (NOW + timedelta(minutes=30)).isoformat(),
        "start_fold": 0, "end_fold": 0,
        "value_inc_vat": "0.07", "unit": "GBP/kWh", "classification": "STANDARD_CHEAP",
        "source": "import-rates", "source_event": "current-day", "source_day": "current",
        "tariff": "T", "source_revision_at": NOW.isoformat(), "event_min_rate": "0.07",
        "event_unique_price_count": 2, "retrieval_source_entity_id": IMPORT_SOURCE,
        "dispatch_source_entity_id": DISPATCH_SOURCE,
    }
    with pytest.raises(ValueError, match="mandatory fields"):
        parse_fused_import_rates([record])
    record["is_intelligent_adjusted"] = None
    with pytest.raises(ValueError, match="Boolean"):
        parse_fused_import_rates([record])


def test_fused_schema_restores_explicit_pep495_fold_and_rejects_invalid_bits() -> None:
    record = {
        "start": NOW.isoformat(), "end": (NOW + timedelta(minutes=30)).isoformat(),
        "start_fold": 1, "end_fold": 0,
        "value_inc_vat": "0.07", "unit": "GBP/kWh", "is_intelligent_adjusted": False,
        "classification": "NOT_CHEAP", "source": "import-rates", "source_event": "current-day",
        "source_day": "current", "tariff": "T", "source_revision_at": NOW.isoformat(),
        "event_min_rate": "0.07", "event_unique_price_count": 1,
        "retrieval_source_entity_id": IMPORT_SOURCE, "dispatch_source_entity_id": DISPATCH_SOURCE,
    }
    parsed = parse_fused_import_rates([record])
    assert parsed[0].start.fold == 1
    assert parsed[0].end.fold == 0
    record["start_fold"] = True
    with pytest.raises(ValueError, match="fold bit"):
        parse_fused_import_rates([record])


def test_fused_schema_rejects_inconsistent_minimum_and_adjusted_rate() -> None:
    records = [
        {
            "start": NOW.isoformat(), "end": (NOW + timedelta(minutes=30)).isoformat(),
            "start_fold": 0, "end_fold": 0,
            "value_inc_vat": "0.08", "unit": "GBP/kWh", "is_intelligent_adjusted": True,
            "classification": "BONUS_DISPATCH", "source": "i", "source_event": "e",
            "source_day": "current", "tariff": "T", "source_revision_at": NOW.isoformat(),
            "event_min_rate": "0.07", "event_unique_price_count": 2,
            "retrieval_source_entity_id": IMPORT_SOURCE,
            "dispatch_source_entity_id": DISPATCH_SOURCE,
        },
        {
            "start": (NOW + timedelta(minutes=30)).isoformat(), "end": (NOW + timedelta(minutes=60)).isoformat(),
            "start_fold": 0, "end_fold": 0,
            "value_inc_vat": "0.07", "unit": "GBP/kWh", "is_intelligent_adjusted": False,
            "classification": "STANDARD_CHEAP", "source": "i", "source_event": "e",
            "source_day": "current", "tariff": "T", "source_revision_at": NOW.isoformat(),
            "event_min_rate": "0.07", "event_unique_price_count": 2,
            "retrieval_source_entity_id": IMPORT_SOURCE,
            "dispatch_source_entity_id": DISPATCH_SOURCE,
        },
    ]
    with pytest.raises(ValueError, match="adjusted rate"):
        parse_fused_import_rates(records)


def test_flat_tariff_is_not_cheap() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.NOT_CHEAP, False),
        ("0.07", CheapClassification.NOT_CHEAP, False),
        ("0.07", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    assert all(rate.classification is CheapClassification.NOT_CHEAP for rate in rates)
    assert _result(rates).coverage_status is CoverageStatus.TRUSTED_EMPTY


def test_exact_cycle_margin_and_value() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    result = _result(rates)
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.windows[0].components[0].margin_per_stored_kwh == (
        Decimal("0.15") * Decimal("0.95") - Decimal("0.07") / Decimal("0.95") - Decimal("0.0165")
    )


@pytest.mark.parametrize("export_price", ["0.08", "0.07"])
def test_zero_or_negative_margin_is_trusted_empty(export_price: str) -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    result = _result(rates, _export((export_price, export_price)))
    assert result.coverage_status is CoverageStatus.TRUSTED_EMPTY
    assert not result.windows
    assert result.diagnostic_components


def test_adjacent_profitable_components_merge_without_averaging_prices() -> None:
    first = _import_rates(
        ("0.30", CheapClassification.NOT_CHEAP, False),
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    first = tuple(
        replace(item, start=item.start - timedelta(minutes=30), end=item.end - timedelta(minutes=30))
        for item in first
    )
    second = _import_rates(
        ("0.08", CheapClassification.STANDARD_CHEAP, False),
        ("0.31", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_day="next", source_event="next-day",
    )
    second = tuple(
        replace(item, start=item.start + timedelta(minutes=30), end=item.end + timedelta(minutes=30))
        for item in second
    )
    rates = first + second
    result = _result(rates, _export(("0.15", "0.16")))
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert len(result.windows) == 1
    assert len(result.windows[0].components) == 2
    assert [item.rate_interval.import_price for item in result.windows[0].components] == [Decimal("0.07"), Decimal("0.08")]


def test_gaps_and_overlaps_never_expose_diagnostic_components_as_windows() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    gapped = rates[:1] + (
        replace(rates[1], start=NOW + timedelta(minutes=45)),
    )
    result = _result(gapped)
    assert result.coverage_status is CoverageStatus.GAPPED
    assert not result.windows


def test_stale_and_future_sources_fail_closed() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    stale = _result(rates, import_source=RateSourceObservation(NOW - timedelta(hours=27), IMPORT_SOURCE))
    assert stale.coverage_status is CoverageStatus.UNAVAILABLE
    future = _result(rates, export_source=RateSourceObservation(NOW + timedelta(minutes=3), EXPORT_SOURCE))
    assert future.coverage_status is CoverageStatus.UNAVAILABLE


def test_dispatch_freshness_required_only_for_actionable_bonus() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.BONUS_DISPATCH, True),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    stale_dispatch = DispatchSourceObservation(NOW - timedelta(minutes=11), DISPATCH_SOURCE)
    result = _result(rates, dispatch_source=stale_dispatch)
    assert result.coverage_status is CoverageStatus.UNAVAILABLE
    ordinary = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
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
            import_price=Decimal("0.07"), classification=CheapClassification.NOT_CHEAP,
            source="e", tariff="T", source_day="current", source_event="e", source_revision_at=NOW,
            retrieval_source_entity_id=IMPORT_SOURCE, dispatch_source_entity_id=DISPATCH_SOURCE,
            event_minimum=Decimal("0.07"), event_unique_price_count=1,
            is_intelligent_adjusted=False,
        ),
    )
    # The fold interval above is a real one-hour UTC interval even though the
    # local wall clock repeats 01:30; its exact UTC coverage is what matters.
    export_rates = (
        ExportRateInterval(
            start=datetime.fromisoformat("2026-10-25T01:30:00+01:00"),
            end=datetime.fromisoformat("2026-10-25T01:30:00+00:00"),
            export_price=Decimal("0.15"), source="e", tariff="E", retrieved_at=NOW,
            source_day="current", source_event="e", source_revision_at=NOW,
            retrieval_source_entity_id=EXPORT_SOURCE,
        ),
    )
    result = evaluate_cheap_windows(
        import_rates=import_rates, export_rates=export_rates, start=start, end=end, now=NOW,
        import_source=RateSourceObservation(NOW, IMPORT_SOURCE),
        export_source=RateSourceObservation(NOW, EXPORT_SOURCE),
    )
    assert result.coverage_status is CoverageStatus.GAPPED


def test_exact_zero_margin_is_not_actionable() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    charge = Decimal("0.95")
    discharge = Decimal("0.95")
    zero_export = (Decimal("0.07") / charge + Decimal("0.0165")) / discharge
    result = _result(
        rates,
        _export((str(zero_export), str(zero_export))),
        charge_efficiency=charge,
        discharge_efficiency=discharge,
    )
    assert result.coverage_status is CoverageStatus.TRUSTED_EMPTY
    assert result.diagnostic_components[0].margin_per_stored_kwh == 0


def test_exact_retrieval_source_and_timestamp_are_bound_to_observation() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    mismatched_import = tuple(
        replace(item, retrieval_source_entity_id="sensor.wrong_import_source")
        for item in rates
    )
    assert _result(mismatched_import).coverage_status is CoverageStatus.INVALID

    exports = tuple(
        replace(item, retrieved_at=NOW - timedelta(hours=27)) for item in _export()
    )
    stale = _result(rates, exports)
    assert stale.coverage_status is CoverageStatus.INVALID
    assert not stale.windows


def test_bonus_dispatch_source_must_match_exact_configured_entity() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.BONUS_DISPATCH, True),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    result = _result(
        rates,
        dispatch_source=DispatchSourceObservation(NOW, "sensor.wrong_dispatch_source"),
    )
    assert result.coverage_status is CoverageStatus.INVALID
    assert not result.windows


def test_empty_current_only_and_export_gap_are_not_trusted_empty() -> None:
    assert _result(()).coverage_status is CoverageStatus.GAPPED
    current_only = _import_rates(
        ("0.07", CheapClassification.NOT_CHEAP, False), source="current-day",
    )
    assert _result(current_only).coverage_status is CoverageStatus.GAPPED
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    assert _result(rates, _export()[:1]).coverage_status is CoverageStatus.GAPPED


def test_overlap_duplicate_and_unordered_input_are_invalid() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    exports = _export()
    overlap = (exports[0], replace(exports[1], start=NOW + timedelta(minutes=15)))
    duplicate = (exports[0], replace(exports[1], start=exports[0].start, end=exports[0].end))
    assert _result(rates, overlap).coverage_status is CoverageStatus.INVALID
    assert _result(rates, duplicate).coverage_status is CoverageStatus.INVALID
    assert _result(tuple(reversed(rates))).coverage_status is CoverageStatus.INVALID


def test_forged_direct_intervals_and_wrong_concrete_types_fail_closed() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    forged = (
        replace(
            rates[0],
            classification=CheapClassification.BONUS_DISPATCH,
            is_intelligent_adjusted=False,
        ),
        rates[1],
    )
    assert _result(forged).coverage_status is CoverageStatus.INVALID
    wrong_type = _result((object(),))
    assert wrong_type.coverage_status is CoverageStatus.INVALID
    assert not wrong_type.windows


def test_fused_export_parser_rejects_missing_revision_and_unordered_records() -> None:
    records = [
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "start_fold": item.start.fold,
                "end_fold": item.end.fold,
            "value_inc_vat": str(item.export_price),
            "unit": item.unit,
            "source": item.source,
            "source_event": item.source_event,
            "source_day": item.source_day,
            "tariff": item.tariff,
            "retrieved_at": item.retrieved_at.isoformat(),
            "source_revision_at": item.source_revision_at.isoformat(),
            "retrieval_source_entity_id": item.retrieval_source_entity_id,
        }
        for item in _export()
    ]
    missing = dict(records[0])
    missing.pop("source_revision_at")
    with pytest.raises(ValueError, match="mandatory fields"):
        parse_fused_export_rates([missing])
    with pytest.raises(ValueError, match="ordered"):
        parse_fused_export_rates(list(reversed(records)))


def test_trusted_import_view_is_complete_without_export_and_bonus_is_independent() -> None:
    standard = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    result = evaluate_trusted_import_rates(
        import_rates=standard, start=NOW, end=NOW + timedelta(hours=1), now=NOW,
        import_source=RateSourceObservation(NOW, IMPORT_SOURCE),
    )
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.intervals == standard

    bonus = _import_rates(
        ("0.07", CheapClassification.BONUS_DISPATCH, True),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="current-day",
    )
    missing = evaluate_trusted_import_rates(
        import_rates=bonus, start=NOW, end=NOW + timedelta(hours=1), now=NOW,
        import_source=RateSourceObservation(NOW, IMPORT_SOURCE),
    )
    assert missing.coverage_status is CoverageStatus.UNAVAILABLE
    assert not missing.intervals
    fresh = evaluate_trusted_import_rates(
        import_rates=bonus, start=NOW, end=NOW + timedelta(hours=1), now=NOW,
        import_source=RateSourceObservation(NOW, IMPORT_SOURCE),
        dispatch_source=DispatchSourceObservation(NOW, DISPATCH_SOURCE),
    )
    assert fresh.coverage_status is CoverageStatus.COMPLETE


def test_complete_coverage_across_both_dst_folds_uses_utc_instants() -> None:
    fold_now = datetime(2026, 10, 25, 0, tzinfo=UTC)
    imports = (
        AdjustedRateInterval(
            start=datetime.fromisoformat("2026-10-25T01:00:00+01:00"),
            end=datetime.fromisoformat("2026-10-25T01:00:00+00:00"),
            import_price=Decimal("0.07"), classification=CheapClassification.STANDARD_CHEAP,
            source="fold-event", tariff="T", source_day="current", source_event="fold-event",
            source_revision_at=fold_now, retrieval_source_entity_id=IMPORT_SOURCE,
            dispatch_source_entity_id=DISPATCH_SOURCE, event_minimum=Decimal("0.07"),
            event_unique_price_count=2, is_intelligent_adjusted=False,
        ),
        AdjustedRateInterval(
            start=datetime.fromisoformat("2026-10-25T01:00:00+00:00"),
            end=datetime.fromisoformat("2026-10-25T02:00:00+00:00"),
            import_price=Decimal("0.30"), classification=CheapClassification.NOT_CHEAP,
            source="fold-event", tariff="T", source_day="current", source_event="fold-event",
            source_revision_at=fold_now, retrieval_source_entity_id=IMPORT_SOURCE,
            dispatch_source_entity_id=DISPATCH_SOURCE, event_minimum=Decimal("0.07"),
            event_unique_price_count=2, is_intelligent_adjusted=False,
        ),
    )
    exports = (
        ExportRateInterval(
            start=datetime.fromisoformat("2026-10-25T01:00:00+01:00"),
            end=datetime.fromisoformat("2026-10-25T01:00:00+00:00"),
            export_price=Decimal("0.15"), source="fold-export", tariff="E", retrieved_at=fold_now,
            source_day="current", source_event="fold-export", source_revision_at=fold_now,
            retrieval_source_entity_id=EXPORT_SOURCE,
        ),
        ExportRateInterval(
            start=datetime.fromisoformat("2026-10-25T01:00:00+00:00"),
            end=datetime.fromisoformat("2026-10-25T02:00:00+00:00"),
            export_price=Decimal("0.15"), source="fold-export", tariff="E", retrieved_at=fold_now,
            source_day="current", source_event="fold-export", source_revision_at=fold_now,
            retrieval_source_entity_id=EXPORT_SOURCE,
        ),
    )
    result = evaluate_cheap_windows(
        import_rates=imports, export_rates=exports,
        start=datetime(2026, 10, 25, 0, tzinfo=UTC),
        end=datetime(2026, 10, 25, 2, tzinfo=UTC), now=fold_now,
        import_source=RateSourceObservation(fold_now, IMPORT_SOURCE),
        export_source=RateSourceObservation(fold_now, EXPORT_SOURCE),
    )
    assert result.coverage_status is CoverageStatus.COMPLETE
