"""Behavior tests for the consolidated house-battery planner."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.model import (
    ControllerHealth,
    CycleState,
    SlotDirection,
    SlotOwner,
    StrategyAction,
    ObservedCapability,
)
from custom_components.house_battery_control.planner import (
    AdjustedRateInterval,
    BONUS_CHARGE_LEASE_DURATION,
    CheapClassification,
    CheapWindow,
    CheapWindowComponent,
    CoverageStatus,
    DispatchSourceObservation,
    EnergyInterval,
    ExportRateInterval,
    RateSourceObservation,
    ReserveInputInterval,
    ReservePlanResult,
    TimeInterval,
    build_plan,
    evaluate_cheap_windows,
    evaluate_trusted_import_rates,
    forecast_load,
    parse_fused_export_rates,
    parse_fused_import_rates,
    plan_reserve,
    prorated_energy,
    RESERVE_SOC_UNCERTAINTY_PERCENT,
    _common_quantize_target,
    _charge_phase_start,
    _charge_phase_end,
    _recover_standard_phase_start,
)
from custom_components.house_battery_control.solis import read_state
from custom_components.house_battery_control.tests.test_solis import fixture as solis_fixture


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


def test_dispatch_last_reported_age_does_not_withdraw_actionable_bonus() -> None:
    rates = _import_rates(
        ("0.07", CheapClassification.BONUS_DISPATCH, True),
        ("0.30", CheapClassification.NOT_CHEAP, False),
        source="import-rates", source_event="current-day",
    )
    stale_dispatch = DispatchSourceObservation(NOW - timedelta(minutes=11), DISPATCH_SOURCE)
    result = _result(rates, dispatch_source=stale_dispatch)
    assert result.coverage_status is CoverageStatus.COMPLETE
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


def test_load_forecast_preserves_local_profiles_and_exact_proration() -> None:
    london = ZoneInfo("Europe/London")
    weekday = datetime(2026, 7, 6, 8, 30, tzinfo=UTC)
    forecast = forecast_load(now=weekday, horizon_end=weekday + timedelta(hours=1), timezone=london)
    assert [item.energy_kwh for item in forecast] == [Decimal("0.2768"), Decimal("0.2585")]
    weekend = forecast_load(
        now=datetime(2026, 7, 4, 8, tzinfo=UTC),
        horizon_end=datetime(2026, 7, 4, 9, tzinfo=UTC),
        timezone=london,
    )
    assert weekend[0].energy_kwh == Decimal("0.3296")
    source = EnergyInterval(TimeInterval(weekday, weekday + timedelta(hours=1)), Decimal("2"))
    middle = TimeInterval(weekday + timedelta(minutes=15), weekday + timedelta(minutes=45))
    assert prorated_energy(middle, (source,), required=True) == Decimal("1.0")


def test_reverse_reserve_handles_concurrent_pv_without_banking_surplus() -> None:
    interval = TimeInterval(NOW, NOW + timedelta(hours=1))

    def reserve(load: str, solar: str = "0", classification=CheapClassification.NOT_CHEAP):
        return plan_reserve(
            intervals=(ReserveInputInterval(interval, Decimal(load), Decimal(solar), classification),),
            capacity_kwh=Decimal("10"), minimum_energy_kwh=Decimal("1"),
            reserve_margin_kwh=Decimal("0"), charge_efficiency=Decimal("0.95"),
            discharge_efficiency=Decimal("0.95"), maximum_charge_power_kw=Decimal("5"),
            maximum_discharge_power_kw=Decimal("5"),
        )

    assert reserve("1").reserve_energy_kwh == Decimal("2.052631578947368421052631579")
    assert reserve("1", solar="0.5").reserve_energy_kwh == Decimal(
        "1.526315789473684210526315790"
    )
    assert reserve("0", classification=CheapClassification.STANDARD_CHEAP).reserve_energy_kwh == Decimal("1")
    assert reserve("0", solar="2").reserve_energy_kwh == Decimal("1")
    assert reserve("6").issue == "forecast demand exceeds battery power"

    surplus_then_demand = plan_reserve(
        intervals=(
            ReserveInputInterval(
                interval,
                Decimal("0"),
                Decimal("2"),
                CheapClassification.NOT_CHEAP,
            ),
            ReserveInputInterval(
                TimeInterval(interval.end, interval.end + timedelta(hours=1)),
                Decimal("1"),
                Decimal("0"),
                CheapClassification.NOT_CHEAP,
            ),
        ),
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal("0"),
        charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"),
        maximum_charge_power_kw=Decimal("5"),
        maximum_discharge_power_kw=Decimal("5"),
    )
    assert surplus_then_demand.reserve_energy_kwh == Decimal(
        "2.052631578947368421052631579"
    )
    with_margin = plan_reserve(
        intervals=(ReserveInputInterval(
            interval, Decimal("0"), Decimal("0"), CheapClassification.STANDARD_CHEAP,
        ),),
        capacity_kwh=Decimal("10"), minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal("0.5"), charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"), maximum_charge_power_kw=Decimal("5"),
        maximum_discharge_power_kw=Decimal("5"),
    )
    assert with_margin.reserve_energy_kwh == Decimal("1.5")


def _config():
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    return integration_config.from_mapping(source)


def _solis(*, soc: str = "55", cycle_target_step: str | None = None):
    parsed, states = solis_fixture()
    states[parsed.telemetry.state_of_charge_entity_id]["state"] = soc
    if cycle_target_step is not None:
        cycle_key = parsed.allocation(SlotOwner.FULL_SOC_CYCLING)[0]
        cycle_target = parsed.direction(cycle_key).target_soc_entity_id
        states[cycle_target]["attributes"]["step"] = cycle_target_step
    result = read_state(states, parsed, now=datetime(2026, 8, 22, 12, tzinfo=UTC))
    assert result.health is ControllerHealth.HEALTHY
    return result


async def _build(
    hass,
    *,
    cheap: bool,
    soc: str = "55",
    state: CycleState = CycleState.IDLE,
    deadline: datetime | None = None,
    window_minutes: int = 60,
    coverage: CoverageStatus = CoverageStatus.COMPLETE,
    reserve_energy: Decimal = Decimal("5"),
    bonus: bool = False,
    charge_lease_deadline: datetime | None = None,
    cycle_target_step: str | None = None,
    now: datetime = NOW,
    window_override: CheapWindow | None = None,
    import_rates_override: tuple[AdjustedRateInterval, ...] | None = None,
    export_rates_override: tuple[ExportRateInterval, ...] | None = None,
):
    config = _config()
    for entity_id, value in (
        (config.tariff.import_rates_entity_id, "available"),
        (config.tariff.export_rates_entity_id, "available"),
        (config.cycle_discharge_duration_entity_id, "10"),
    ):
        hass.states.async_set(entity_id, value, {"rates": ("placeholder",)})
    imports = import_rates_override or _import_rates(
        ("0.07", CheapClassification.BONUS_DISPATCH if bonus else CheapClassification.STANDARD_CHEAP, bonus),
        ("0.30", CheapClassification.NOT_CHEAP, False),
    )
    if bonus:
        hass.states.async_set(DISPATCH_SOURCE, "on")
    exports = export_rates_override or _export()
    windows = (
        _result(
            imports,
            exports,
            dispatch_source=DispatchSourceObservation(NOW, DISPATCH_SOURCE),
        ).windows
        if cheap
        else ()
    )
    if window_override is not None:
        windows = (window_override,)
    if windows and window_minutes != 60:
        windows = (replace(windows[0], end=NOW + timedelta(minutes=window_minutes)),)
    window_result = SimpleNamespace(
        coverage_status=coverage,
        issues=() if coverage is CoverageStatus.COMPLETE else ("untrusted tariff input",),
        windows=windows,
    )
    trusted = SimpleNamespace(
        coverage_status=CoverageStatus.COMPLETE, issues=(), intervals=imports,
    )
    reserve_interval = ReserveInputInterval(
        TimeInterval(NOW, NOW + timedelta(minutes=30)), Decimal("0.2"), Decimal("0"),
        CheapClassification.STANDARD_CHEAP if cheap else CheapClassification.NOT_CHEAP,
    )
    with (
        patch("custom_components.house_battery_control.planner.parse_fused_import_rates", return_value=imports),
        patch("custom_components.house_battery_control.planner.parse_fused_export_rates", return_value=exports),
        patch("custom_components.house_battery_control.planner._rate_source", return_value=RateSourceObservation(NOW, IMPORT_SOURCE)),
        patch("custom_components.house_battery_control.planner._dispatch_source", return_value=DispatchSourceObservation(NOW, DISPATCH_SOURCE)),
        patch("custom_components.house_battery_control.planner.evaluate_cheap_windows", return_value=window_result),
        patch("custom_components.house_battery_control.planner.evaluate_trusted_import_rates", return_value=trusted),
        patch("custom_components.house_battery_control.planner._forecast_intervals", AsyncMock(return_value=(reserve_interval,))),
        patch("custom_components.house_battery_control.planner.plan_reserve", return_value=ReservePlanResult(reserve_energy)),
    ):
        return await build_plan(
            hass, config, _solis(soc=soc, cycle_target_step=cycle_target_step), now=now, cycle_state=state,
            cycle_deadline=deadline, charge_lease_deadline=charge_lease_deadline,
        )


@pytest.mark.asyncio
async def test_build_plan_selects_all_three_actions_and_reports_reserve(hass) -> None:
    charge = await _build(hass, cheap=True)
    reserve = await _build(hass, cheap=False)
    cycle = await _build(hass, cheap=True, soc="100")

    assert charge.action is StrategyAction.CHEAP_CHARGE
    assert charge.intent.segments[0].direction is SlotDirection.CHARGE  # type: ignore[union-attr]
    assert charge.intent.start == NOW  # type: ignore[union-attr]
    assert charge.intent.end == NOW + timedelta(minutes=30)  # type: ignore[union-attr]
    assert reserve.action is StrategyAction.RESERVE_DISCHARGE
    assert reserve.intent.segments[0].owner is SlotOwner.RESERVE_EXPORT  # type: ignore[union-attr]
    assert cycle.action is StrategyAction.CYCLE_DISCHARGE
    assert cycle.next_cycle_state is CycleState.CYCLE_DISCHARGING
    assert reserve.reserve_energy_kwh == Decimal("5")
    assert reserve.battery_energy_kwh == Decimal("17.68448")
    assert reserve.reserve_balance_kwh == Decimal("12.68448")
    assert reserve.maximum_charge_power_kw == Decimal("5.12")
    assert reserve.maximum_discharge_power_kw == Decimal("5.12")


@pytest.mark.asyncio
async def test_build_plan_recovers_real_half_hour_standard_phase_across_midnight(hass) -> None:
    phase_start = datetime(2026, 8, 22, 23, 30, tzinfo=UTC)
    phase_end = datetime(2026, 8, 23, 5, 30, tzinfo=UTC)
    imports = tuple(
        replace(
            _import_rates(
                ("0.07", CheapClassification.STANDARD_CHEAP, False),
                ("0.30", CheapClassification.NOT_CHEAP, False),
            )[0],
            start=phase_start + timedelta(minutes=30 * index),
            end=phase_start + timedelta(minutes=30 * (index + 1)),
            source_event=f"phase-{index}",
        )
        for index in range(12)
    )
    exports = (replace(_export()[0], start=phase_start, end=phase_end),)
    planned = []
    for now in (
        datetime(2026, 8, 22, 23, 45, tzinfo=UTC),
        datetime(2026, 8, 23, 0, 1, tzinfo=UTC),
        datetime(2026, 8, 23, 0, 31, tzinfo=UTC),
    ):
        current_rate = next(item for item in imports if item.start <= now < item.end)
        current = CheapWindow(
            now,
            phase_end,
            (CheapWindowComponent(TimeInterval(now, phase_end), current_rate, exports[0], Decimal("1")),),
        )
        plan_result = await _build(
            hass,
            cheap=True,
            now=now,
            window_override=current,
            import_rates_override=imports,
            export_rates_override=exports,
        )
        assert plan_result.intent is not None
        planned.append((plan_result.intent.start, plan_result.intent.end))

    assert planned == [(phase_start, phase_end)] * 3


@pytest.mark.asyncio
async def test_cycle_target_capability_is_in_common_reserve_quantization_domain(hass) -> None:
    result = await _build(
        hass,
        cheap=True,
        soc="100",
        reserve_energy=Decimal("5.4649418"),
        cycle_target_step="2",
    )

    assert result.action is StrategyAction.CYCLE_DISCHARGE
    assert result.reserve_soc_percent == Decimal("18")
    assert result.control_reserve_soc_percent == Decimal("18")
    assert result.intent is not None
    assert result.intent.segments[0].target_soc == Decimal("18")


@pytest.mark.asyncio
async def test_bonus_charge_lease_is_clipped_to_fifteen_minutes_and_does_not_extend(
    hass,
) -> None:
    started = await _build(hass, cheap=True, bonus=True)
    assert started.action is StrategyAction.CHEAP_CHARGE
    assert started.charge_lease_deadline == NOW + BONUS_CHARGE_LEASE_DURATION
    assert started.intent is not None
    assert started.intent.end == NOW + BONUS_CHARGE_LEASE_DURATION

    for _ in range(3):
        heartbeat = await _build(
            hass,
            cheap=True,
            bonus=True,
            state=CycleState.CHARGING,
            charge_lease_deadline=started.charge_lease_deadline,
        )
        assert heartbeat.intent is not None
        assert heartbeat.intent.end == started.intent.end
        assert heartbeat.charge_lease_deadline == started.charge_lease_deadline


def test_adjacent_bonus_components_keep_the_first_native_lease_boundary() -> None:
    first = _import_rates(
        ("0.07", CheapClassification.BONUS_DISPATCH, True),
        ("0.30", CheapClassification.NOT_CHEAP, False),
    )[0]
    first = replace(first, end=NOW + timedelta(minutes=5))
    second = replace(first, start=first.end, end=NOW + timedelta(minutes=10), source_event="next")
    export = _export()[0]
    window = CheapWindow(
        NOW,
        second.end,
        (
            CheapWindowComponent(
                TimeInterval(first.start, first.end), first, export, Decimal("1")
            ),
            CheapWindowComponent(
                TimeInterval(second.start, second.end), second, export, Decimal("1")
            ),
        ),
    )
    end, is_bonus = _charge_phase_end(window, NOW)
    assert is_bonus
    assert end == first.end


def test_adjacent_standard_components_share_a_phase_and_retain_past_boundary() -> None:
    first = _import_rates(
        ("0.07", CheapClassification.STANDARD_CHEAP, False),
        ("0.30", CheapClassification.NOT_CHEAP, False),
    )[0]
    first = replace(first, start=NOW - timedelta(minutes=30), end=NOW)
    second = replace(first, start=NOW, end=NOW + timedelta(minutes=30), source_event="next")
    export = _export()[0]
    window = CheapWindow(
        first.start,
        second.end,
        (
            CheapWindowComponent(TimeInterval(first.start, first.end), first, export, Decimal("1")),
            CheapWindowComponent(TimeInterval(second.start, second.end), second, export, Decimal("1")),
        ),
    )

    phase_start, classification = _charge_phase_start(window, NOW)
    assert classification is CheapClassification.STANDARD_CHEAP
    assert phase_start == first.start


def test_standard_phase_recovery_stops_at_adjacent_bonus_component() -> None:
    bonus_start = datetime(2026, 8, 22, 23, 30, tzinfo=UTC)
    standard_start = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    phase_end = datetime(2026, 8, 23, 5, 30, tzinfo=UTC)
    standard = replace(
        _import_rates(
            ("0.07", CheapClassification.STANDARD_CHEAP, False),
            ("0.30", CheapClassification.NOT_CHEAP, False),
        )[0],
        start=standard_start,
        end=standard_start + timedelta(minutes=30),
    )
    bonus = replace(
        standard,
        start=bonus_start,
        end=standard_start,
        classification=CheapClassification.BONUS_DISPATCH,
        is_intelligent_adjusted=True,
        source_event="bonus",
    )
    export = replace(_export()[0], start=bonus_start, end=phase_end)
    now = datetime(2026, 8, 23, 0, 1, tzinfo=UTC)
    current = CheapWindow(
        now,
        phase_end,
        (CheapWindowComponent(TimeInterval(now, phase_end), standard, export, Decimal("1")),),
    )

    assert _recover_standard_phase_start(
        current,
        now,
        import_rates=(bonus, standard),
        export_rates=(export,),
        charge_efficiency=Decimal("0.95"),
        discharge_efficiency=Decimal("0.95"),
    ) == standard_start


def test_bonus_lease_component_boundary_uses_utc_instants_across_dst_fold() -> None:
    london = ZoneInfo("Europe/London")
    start = datetime(2026, 10, 25, 1, 0, tzinfo=london, fold=0)
    end = datetime(2026, 10, 25, 1, 0, tzinfo=london, fold=1)
    rate = _import_rates(
        ("0.07", CheapClassification.BONUS_DISPATCH, True),
        ("0.30", CheapClassification.NOT_CHEAP, False),
    )[0]
    rate = replace(rate, start=start, end=end)
    export = replace(_export()[0], start=start, end=end)
    window = CheapWindow(
        start,
        end,
        (CheapWindowComponent(TimeInterval(start, end), rate, export, Decimal("1")),),
    )
    fold_now = datetime(2026, 10, 25, 1, 30, tzinfo=london, fold=0)
    boundary, is_bonus = _charge_phase_end(window, fold_now)
    assert is_bonus
    assert boundary.astimezone(UTC) == end.astimezone(UTC)


@pytest.mark.asyncio
async def test_build_plan_clamps_reserve_to_absolute_soc_floor(hass) -> None:
    result = await _build(hass, cheap=False, reserve_energy=Decimal("1"))
    assert result.reserve_soc_percent == Decimal("10")
    assert result.intent.segments[0].target_soc == Decimal("10")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reserve_export_uses_one_percent_soc_uncertainty_band(hass) -> None:
    at_boundary = await _build(
        hass, cheap=False, soc="18", reserve_energy=Decimal("5.4649418")
    )
    above_boundary = await _build(
        hass, cheap=False, soc="19", reserve_energy=Decimal("5.4649418")
    )

    assert RESERVE_SOC_UNCERTAINTY_PERCENT == Decimal("1")
    assert at_boundary.reserve_energy_kwh == Decimal("5.4649418")
    assert at_boundary.control_reserve_soc_percent == Decimal("17")
    assert at_boundary.control_reserve_energy_kwh == Decimal("5.466112")
    assert at_boundary.action is StrategyAction.RESERVE_FOLLOW
    assert at_boundary.intent is None
    assert above_boundary.action is StrategyAction.RESERVE_DISCHARGE
    assert above_boundary.intent is not None
    assert above_boundary.intent.segments[0].target_soc == Decimal("17")


def test_common_reserve_quantizer_preserves_decimal_capability_values() -> None:
    capabilities = (
        ObservedCapability(Decimal("10"), Decimal("0"), Decimal("100"), Decimal("0.5"), "%"),
        ObservedCapability(Decimal("10"), Decimal("0"), Decimal("100"), Decimal("0.5"), "%"),
    )
    assert _common_quantize_target(Decimal("17.1"), capabilities) == Decimal("17.5")


def test_common_reserve_quantizer_handles_tiny_compatible_steps() -> None:
    capabilities = (
        ObservedCapability(Decimal("10"), Decimal("0"), Decimal("100"), Decimal("0.0001"), "%"),
        ObservedCapability(Decimal("10"), Decimal("0"), Decimal("100"), Decimal("0.5"), "%"),
    )
    assert _common_quantize_target(Decimal("17.10001"), capabilities) == Decimal("17.5")


def test_common_reserve_quantizer_handles_dense_decimal_steps() -> None:
    capabilities = (
        ObservedCapability(Decimal("0"), Decimal("0"), Decimal("100"), Decimal("0.0001"), "%"),
        ObservedCapability(Decimal("0"), Decimal("0"), Decimal("100"), Decimal("0.000100001"), "%"),
    )
    assert _common_quantize_target(Decimal("17.1234"), capabilities) == Decimal("20.0002")


def test_common_reserve_quantizer_handles_offset_progressions() -> None:
    capabilities = (
        ObservedCapability(Decimal("1"), Decimal("1"), Decimal("20"), Decimal("2"), "%"),
        ObservedCapability(Decimal("0"), Decimal("0"), Decimal("30"), Decimal("3"), "%"),
    )
    assert _common_quantize_target(Decimal("0"), capabilities) == Decimal("15")


def test_common_reserve_quantizer_rejects_incompatible_or_out_of_range() -> None:
    incompatible = (
        ObservedCapability(Decimal("10"), Decimal("0"), Decimal("100"), Decimal("2"), "%"),
        ObservedCapability(Decimal("11"), Decimal("1"), Decimal("100"), Decimal("2"), "%"),
    )
    with pytest.raises(ValueError, match="common"):
        _common_quantize_target(Decimal("17"), incompatible)
    out_of_range = (
        ObservedCapability(Decimal("0"), Decimal("0"), Decimal("10"), Decimal("2"), "%"),
        ObservedCapability(Decimal("0"), Decimal("0"), Decimal("8"), Decimal("3"), "%"),
    )
    with pytest.raises(ValueError, match="common"):
        _common_quantize_target(Decimal("9"), out_of_range)


@pytest.mark.asyncio
async def test_build_plan_cycle_deadline_is_fixed_and_requires_recharge_time(hass) -> None:
    started = await _build(hass, cheap=True, soc="100")
    assert started.cycle_deadline == NOW + timedelta(minutes=10)
    continued = await _build(
        hass, cheap=True, soc="80", state=CycleState.CYCLE_DISCHARGING,
        deadline=started.cycle_deadline,
    )
    assert continued.action is StrategyAction.CYCLE_DISCHARGE
    assert continued.intent.end == started.cycle_deadline  # type: ignore[union-attr]
    too_short = await _build(hass, cheap=True, soc="100", window_minutes=15)
    assert too_short.action is StrategyAction.IDLE
    assert too_short.intent is None


@pytest.mark.asyncio
async def test_build_plan_outputs_aware_utc_logical_intents_without_physical_slots(hass) -> None:
    result = await _build(hass, cheap=False)
    assert result.intent is not None
    assert not hasattr(result.intent, "physical_slot")
    for segment in result.intent.segments:
        assert segment.start.tzinfo is UTC
        assert segment.end.tzinfo is UTC
        assert segment.expiry.tzinfo is UTC
        assert not hasattr(segment, "physical_slot")


@pytest.mark.asyncio
async def test_unavailable_input_returns_one_issue_and_preserves_bounded_cycle(hass) -> None:
    config = _config()
    deadline = NOW + timedelta(minutes=8)
    result = await build_plan(
        hass, config, _solis(), now=NOW,
        cycle_state=CycleState.CYCLE_DISCHARGING, cycle_deadline=deadline,
    )
    assert result.action is StrategyAction.IDLE
    assert result.intent is None
    assert result.issue is not None
    assert result.next_cycle_state is CycleState.CYCLE_DISCHARGING
    assert result.cycle_deadline == deadline


@pytest.mark.parametrize("coverage", (CoverageStatus.UNAVAILABLE, CoverageStatus.GAPPED, CoverageStatus.INVALID))
@pytest.mark.asyncio
async def test_untrusted_or_incomplete_input_returns_no_intent(hass, coverage) -> None:
    result = await _build(hass, cheap=True, coverage=coverage)
    assert result.action is StrategyAction.IDLE
    assert result.intent is None
    assert result.issue is not None
