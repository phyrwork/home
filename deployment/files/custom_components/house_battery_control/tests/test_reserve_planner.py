from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from custom_components.house_battery_control.domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from custom_components.house_battery_control.octopus_windows import (
    AdjustedRateInterval,
    CheapClassification,
    CoverageStatus,
    CheapWindowResult,
    DispatchSourceObservation,
    RateSourceObservation,
    TrustedImportResult,
)
from custom_components.house_battery_control.reserve_planner import (
    CommissionedPowerEnvelope,
    ReserveAuthority,
    ReserveInputInterval,
    ReservePlanningStatus,
    commissioned_reserve,
)
from custom_components.house_battery_control.interval import TimeInterval


UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
IMPORT_SOURCE = "sensor.import_rates_data_last_retrieved"
DISPATCH_SOURCE = "sensor.dispatches_data_last_retrieved"


def _envelope(**changes):
    values = dict(
        maximum_charge_power_kw=Decimal("2"),
        maximum_discharge_power_kw=Decimal("2"),
        maximum_grid_import_power_kw=MAXIMUM_GRID_IMPORT_POWER_KW,
        schema_version="1",
        inverter_identity="garage-inverter",
        mapping_fingerprint="map-1",
        candidate_policy_fingerprint="policy-1",
        manual_grid_fingerprint="grid-1",
        capability_fingerprint="cap-1",
        evidence_source="commissioning-session-1",
        validated_at=NOW,
    )
    values.update(changes)
    return CommissionedPowerEnvelope(**values)


def _authority(**changes):
    values = dict(
        schema_version="1",
        inverter_identity="garage-inverter",
        mapping_fingerprint="map-1",
        candidate_policy_fingerprint="policy-1",
        manual_grid_fingerprint="grid-1",
        capability_fingerprint="cap-1",
    )
    values.update(changes)
    return ReserveAuthority(**values)


def _intervals(*classes, load=Decimal("0"), solar=Decimal("0")):
    result = []
    for index, classification in enumerate(classes):
        start = NOW + timedelta(hours=index)
        result.append(
            ReserveInputInterval(
                TimeInterval(start, start + timedelta(hours=1)),
                load,
                solar,
                classification,
            )
        )
    return tuple(result)


def _trusted(intervals):
    records = list(intervals)
    prices = [
        Decimal("0.05")
        if item.classification is not CheapClassification.NOT_CHEAP
        else Decimal("0.20")
        for item in records
    ]
    if any(item.classification is CheapClassification.STANDARD_CHEAP for item in records) and len(set(prices)) == 1:
        extra_start = records[-1].interval.end
        records.append(
            ReserveInputInterval(
                TimeInterval(extra_start, extra_start + timedelta(hours=1)),
                Decimal(0),
                Decimal(0),
                CheapClassification.NOT_CHEAP,
            )
        )
        prices.append(Decimal("0.20"))
    unique_count = len(set(prices))
    event_minimum = min(prices)
    rates = tuple(
        AdjustedRateInterval(
            start=item.interval.start,
            end=item.interval.end,
            import_price=price,
            classification=item.classification,
            source="event-source",
            tariff="T-1",
            source_day="2026-01-01",
            source_event="event-1",
            source_revision_at=NOW,
            retrieval_source_entity_id=IMPORT_SOURCE,
            dispatch_source_entity_id=DISPATCH_SOURCE,
            event_minimum=event_minimum,
            event_unique_price_count=unique_count,
            is_intelligent_adjusted=item.classification is CheapClassification.BONUS_DISPATCH,
        )
        for item, price in zip(records, prices, strict=True)
    )
    return TrustedImportResult(CoverageStatus.COMPLETE, intervals=rates)


_DEFAULT = object()


def _plan(
    intervals,
    *,
    commissioned=_DEFAULT,
    authority=_DEFAULT,
    trusted_import=_DEFAULT,
    import_source=_DEFAULT,
    dispatch_source=_DEFAULT,
    **changes,
):
    values = dict(
        intervals=intervals,
        trusted_import=_trusted(intervals) if trusted_import is _DEFAULT else trusted_import,
        import_source=(
            RateSourceObservation(NOW, IMPORT_SOURCE)
            if import_source is _DEFAULT
            else import_source
        ),
        dispatch_source=(
            DispatchSourceObservation(NOW, DISPATCH_SOURCE)
            if any(item.classification is CheapClassification.BONUS_DISPATCH for item in intervals)
            and dispatch_source is _DEFAULT
            else None
            if dispatch_source is _DEFAULT
            else dispatch_source
        ),
        start=intervals[0].interval.start,
        end=intervals[-1].interval.end,
        now=NOW,
        capacity_kwh=Decimal("10"),
        minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal("1"),
        charge_efficiency=Decimal("0.8"),
        discharge_efficiency=Decimal("0.5"),
        commissioned=(
            _envelope()
            if commissioned is _DEFAULT
            else commissioned
        ),
        authority=_authority() if authority is _DEFAULT else authority,
    )
    values.update(changes)
    return commissioned_reserve(**values)


def test_exact_grid_contribution_and_delivered_discharge_semantics():
    result = _plan(_intervals(CheapClassification.NOT_CHEAP, load=Decimal("0.2")))
    assert result.status is ReservePlanningStatus.COMPLETE
    # 0.2 kWh load less 0.1 kW x 1h grid contribution is 0.1 kWh AC;
    # discharge efficiency is applied once at the battery boundary.
    assert result.trajectory[0].start_energy_kwh == Decimal("2.2")
    assert result.trajectory[0].end_energy_kwh == Decimal("2")


def test_cheap_import_is_independent_of_export_profitability():
    result = _plan(_intervals(CheapClassification.STANDARD_CHEAP))
    assert result.status is ReservePlanningStatus.COMPLETE
    assert result.trajectory[0].start_energy_kwh == Decimal("2")


def test_bonus_and_standard_are_both_recharge_classes():
    result = _plan(_intervals(CheapClassification.BONUS_DISPATCH))
    assert result.status is ReservePlanningStatus.COMPLETE


def test_external_pv_uses_shared_charge_cap_and_efficiency():
    result = _plan(_intervals(CheapClassification.NOT_CHEAP, load=Decimal("-10"), solar=Decimal("0")))
    assert result.trajectory[0].start_energy_kwh == Decimal("2")
    # 2 kW AC for one hour at 0.8 stores 1.6 kWh, but the floor wins.
    assert result.trajectory[0].end_energy_kwh == Decimal("2")


def test_charge_power_is_before_efficiency_and_bounded():
    result = _plan(_intervals(CheapClassification.STANDARD_CHEAP))
    assert result.trajectory[0].start_energy_kwh == Decimal("2")


def test_discharge_power_is_infeasible_not_clamped():
    result = _plan(_intervals(CheapClassification.NOT_CHEAP, load=Decimal("3")))
    assert result.status is ReservePlanningStatus.INFEASIBLE
    assert result.issues[0].code == "DISCHARGE_POWER_EXCEEDED"


def test_floor_above_capacity_is_infeasible():
    result = _plan(_intervals(CheapClassification.NOT_CHEAP),)
    result = commissioned_reserve(
        intervals=_intervals(CheapClassification.NOT_CHEAP), trusted_import=_trusted(_intervals(CheapClassification.NOT_CHEAP)), import_source=RateSourceObservation(NOW, IMPORT_SOURCE),
        start=NOW, end=NOW + timedelta(hours=1), now=NOW, capacity_kwh=Decimal("1"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal("1"),
        charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"), commissioned=_envelope(), authority=_authority(),
    )
    assert result.status is ReservePlanningStatus.INFEASIBLE


def test_absent_or_mismatched_commissioning_is_unavailable():
    intervals = _intervals(CheapClassification.NOT_CHEAP)
    missing = commissioned_reserve(
        intervals=intervals, trusted_import=_trusted(intervals), import_source=RateSourceObservation(NOW, IMPORT_SOURCE), start=NOW, end=NOW + timedelta(hours=1), now=NOW,
        capacity_kwh=Decimal("10"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal("0"), charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"), commissioned=None, authority=_authority(),
    )
    assert missing.status is ReservePlanningStatus.UNAVAILABLE
    mismatch = _plan(intervals, commissioned=_envelope(capability_fingerprint="different"))
    assert mismatch.status is ReservePlanningStatus.UNAVAILABLE


def test_grid_value_is_named_constant_not_substitute():
    result = _plan(
        _intervals(CheapClassification.NOT_CHEAP),
        commissioned=_envelope(maximum_grid_import_power_kw=Decimal("0.2")),
    )
    assert result.status is ReservePlanningStatus.UNAVAILABLE


def test_import_trust_gaps_and_diagnostics_are_unavailable_or_invalid():
    intervals = _intervals(CheapClassification.NOT_CHEAP)
    unavailable = commissioned_reserve(
        intervals=intervals, trusted_import=TrustedImportResult(CoverageStatus.GAPPED), import_source=RateSourceObservation(NOW, IMPORT_SOURCE), start=NOW, end=NOW + timedelta(hours=1), now=NOW,
        capacity_kwh=Decimal("10"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal("0"), charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"), commissioned=_envelope(), authority=_authority(),
    )
    assert unavailable.status is ReservePlanningStatus.UNAVAILABLE
    invalid = commissioned_reserve(
        intervals=intervals, trusted_import=TrustedImportResult(CoverageStatus.INVALID), import_source=RateSourceObservation(NOW, IMPORT_SOURCE), start=NOW, end=NOW + timedelta(hours=1), now=NOW,
        capacity_kwh=Decimal("10"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal("0"), charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"), commissioned=_envelope(), authority=_authority(),
    )
    assert invalid.status is ReservePlanningStatus.INVALID


def test_invalid_numeric_inputs_are_result_not_exception():
    intervals = _intervals(CheapClassification.NOT_CHEAP)
    result = commissioned_reserve(
        intervals=intervals, trusted_import=_trusted(intervals), import_source=RateSourceObservation(NOW, IMPORT_SOURCE), start=NOW, end=NOW + timedelta(hours=1), now=NOW,
        capacity_kwh=Decimal("NaN"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal("0"), charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"), commissioned=_envelope(), authority=_authority(),
    )
    assert result.status is ReservePlanningStatus.INVALID


def test_cross_midnight_utc_ordering_is_supported():
    start = datetime(2026, 1, 1, 23, tzinfo=UTC)
    intervals = tuple(
        ReserveInputInterval(TimeInterval(start + timedelta(hours=i), start + timedelta(hours=i + 1)), Decimal("0"), Decimal("0"), CheapClassification.NOT_CHEAP)
        for i in range(2)
    )
    rates = _trusted(intervals)
    result = commissioned_reserve(
        intervals=intervals, trusted_import=rates, import_source=RateSourceObservation(NOW, IMPORT_SOURCE), start=start, end=start + timedelta(hours=2), now=NOW,
        capacity_kwh=Decimal("10"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal("0"), charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"), commissioned=_envelope(), authority=_authority(),
    )
    assert result.status is ReservePlanningStatus.COMPLETE
    assert result.trajectory[0].start_energy_kwh == result.trajectory[0].end_energy_kwh == Decimal("1")


def test_cheap_recharge_reduces_energy_needed_before_later_deficit():
    intervals = (
        ReserveInputInterval(TimeInterval(NOW, NOW + timedelta(hours=1)), Decimal(0), Decimal(0), CheapClassification.STANDARD_CHEAP),
        ReserveInputInterval(TimeInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), Decimal(1), Decimal(0), CheapClassification.NOT_CHEAP),
    )
    result = _plan(intervals)
    assert result.status is ReservePlanningStatus.COMPLETE
    assert tuple((item.start_energy_kwh, item.end_energy_kwh) for item in result.trajectory) == (
        (Decimal("2.2"), Decimal("3.8")),
        (Decimal("3.8"), Decimal("2")),
    )


def test_negative_load_surplus_is_capped_by_same_ac_charge_envelope():
    intervals = (
        ReserveInputInterval(TimeInterval(NOW, NOW + timedelta(hours=1)), Decimal("-10"), Decimal(0), CheapClassification.NOT_CHEAP),
        ReserveInputInterval(TimeInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), Decimal(1), Decimal(0), CheapClassification.NOT_CHEAP),
    )
    result = _plan(intervals)
    assert tuple((item.start_energy_kwh, item.end_energy_kwh) for item in result.trajectory) == (
        (Decimal("2.2"), Decimal("3.8")),
        (Decimal("3.8"), Decimal("2")),
    )


def test_asymmetric_ac_power_boundaries_are_used_independently():
    intervals = (
        ReserveInputInterval(TimeInterval(NOW, NOW + timedelta(hours=1)), Decimal(0), Decimal(0), CheapClassification.STANDARD_CHEAP),
        ReserveInputInterval(TimeInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), Decimal(1), Decimal(0), CheapClassification.NOT_CHEAP),
    )
    result = _plan(
        intervals,
        commissioned=_envelope(
            maximum_charge_power_kw=Decimal("0.5"),
            maximum_discharge_power_kw=Decimal("1"),
        ),
    )
    assert result.status is ReservePlanningStatus.COMPLETE
    assert result.trajectory[0].start_energy_kwh == Decimal("3.4")
    assert result.trajectory[1].start_energy_kwh == Decimal("3.8")


@pytest.mark.parametrize(
    ("load", "expected"),
    ((Decimal("0.05"), Decimal("2")), (Decimal("0.1"), Decimal("2")), (Decimal("0.2"), Decimal("2.2"))),
)
def test_deficits_below_equal_and_above_exact_grid_allowance(load, expected):
    result = _plan(_intervals(CheapClassification.NOT_CHEAP, load=load))
    assert result.status is ReservePlanningStatus.COMPLETE
    assert result.trajectory[0].start_energy_kwh == expected


def test_boundary_equal_to_capacity_is_complete_and_above_is_infeasible():
    equal = _plan(
        _intervals(CheapClassification.NOT_CHEAP, load=Decimal("1.1")),
        capacity_kwh=Decimal("4"),
    )
    assert equal.status is ReservePlanningStatus.COMPLETE
    assert equal.trajectory[0].start_energy_kwh == Decimal("4")
    above = _plan(
        _intervals(CheapClassification.NOT_CHEAP, load=Decimal("1.2")),
        capacity_kwh=Decimal("4"),
    )
    assert above.status is ReservePlanningStatus.INFEASIBLE
    assert above.issues[0].code == "CAPACITY_EXCEEDED"


@pytest.mark.parametrize(
    "field",
    (
        "schema_version",
        "inverter_identity",
        "mapping_fingerprint",
        "candidate_policy_fingerprint",
        "manual_grid_fingerprint",
        "capability_fingerprint",
    ),
)
def test_every_authority_mismatch_makes_commissioning_unavailable(field):
    intervals = _intervals(CheapClassification.NOT_CHEAP)
    result = _plan(intervals, authority=_authority(**{field: "changed"}))
    assert result.status is ReservePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "AUTHORITY_MISMATCH"


@pytest.mark.parametrize(
    "field",
    ("maximum_charge_power_kw", "maximum_discharge_power_kw", "maximum_grid_import_power_kw"),
)
def test_missing_verified_ac_power_is_unavailable(field):
    result = _plan(
        _intervals(CheapClassification.NOT_CHEAP),
        commissioned=_envelope(**{field: None}),
    )
    assert result.status is ReservePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "POWER_EVIDENCE_MISSING"


@pytest.mark.parametrize("bad", (Decimal("NaN"), Decimal("Infinity"), Decimal("-1")))
@pytest.mark.parametrize("field", ("maximum_charge_power_kw", "maximum_discharge_power_kw"))
def test_malformed_or_negative_present_ac_power_is_invalid(field, bad):
    result = _plan(
        _intervals(CheapClassification.NOT_CHEAP),
        commissioned=_envelope(**{field: bad}),
    )
    assert result.status is ReservePlanningStatus.INVALID


@pytest.mark.parametrize(
    ("field", "bad"),
    (
        ("capacity_kwh", Decimal("NaN")),
        ("capacity_kwh", Decimal("-1")),
        ("minimum_energy_kwh", Decimal("-1")),
        ("minimum_energy_kwh", Decimal("11")),
        ("reserve_margin_kwh", Decimal("-1")),
        ("charge_efficiency", Decimal("0")),
        ("charge_efficiency", Decimal("1.01")),
        ("discharge_efficiency", Decimal("0")),
        ("discharge_efficiency", Decimal("Infinity")),
    ),
)
def test_every_invalid_battery_numeric_returns_invalid(field, bad):
    result = _plan(_intervals(CheapClassification.NOT_CHEAP), **{field: bad})
    assert result.status is ReservePlanningStatus.INVALID


@pytest.mark.parametrize(
    ("load", "solar"),
    ((Decimal("NaN"), Decimal(0)), (Decimal(0), Decimal("Infinity"))),
)
def test_nonfinite_forecast_energy_is_invalid(load, solar):
    result = _plan(_intervals(CheapClassification.NOT_CHEAP, load=load, solar=solar))
    assert result.status is ReservePlanningStatus.INVALID


@pytest.mark.parametrize(
    "bad",
    (Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1")),
)
def test_invalid_commissioned_grid_power_is_invalid(bad):
    result = _plan(
        _intervals(CheapClassification.NOT_CHEAP),
        commissioned=_envelope(maximum_grid_import_power_kw=bad),
    )
    assert result.status is ReservePlanningStatus.INVALID


def test_negative_solar_is_invalid_but_negative_load_is_valid():
    negative_solar = _plan(_intervals(CheapClassification.NOT_CHEAP, solar=Decimal("-1")))
    assert negative_solar.status is ReservePlanningStatus.INVALID
    negative_load = _plan(_intervals(CheapClassification.NOT_CHEAP, load=Decimal("-1")))
    assert negative_load.status is ReservePlanningStatus.COMPLETE


def test_commissioning_validation_time_cannot_be_future():
    result = _plan(
        _intervals(CheapClassification.NOT_CHEAP),
        commissioned=_envelope(validated_at=NOW + timedelta(seconds=1)),
    )
    assert result.status is ReservePlanningStatus.INVALID


@pytest.mark.parametrize(
    ("import_source", "expected"),
    (
        (None, ReservePlanningStatus.UNAVAILABLE),
        (RateSourceObservation(NOW - timedelta(hours=27), IMPORT_SOURCE), ReservePlanningStatus.UNAVAILABLE),
        (RateSourceObservation(NOW + timedelta(minutes=3), IMPORT_SOURCE), ReservePlanningStatus.UNAVAILABLE),
        (RateSourceObservation(NOW, "sensor.wrong"), ReservePlanningStatus.INVALID),
    ),
)
def test_ordinary_source_is_revalidated_at_planner_now(import_source, expected):
    result = _plan(_intervals(CheapClassification.NOT_CHEAP), import_source=import_source)
    assert result.status is expected


@pytest.mark.parametrize(
    ("dispatch_source", "expected"),
    (
        (None, ReservePlanningStatus.UNAVAILABLE),
        (DispatchSourceObservation(NOW - timedelta(minutes=11), DISPATCH_SOURCE), ReservePlanningStatus.UNAVAILABLE),
        (DispatchSourceObservation(NOW + timedelta(minutes=3), DISPATCH_SOURCE), ReservePlanningStatus.UNAVAILABLE),
        (DispatchSourceObservation(NOW, "sensor.wrong"), ReservePlanningStatus.INVALID),
        (DispatchSourceObservation(NOW, DISPATCH_SOURCE), ReservePlanningStatus.COMPLETE),
    ),
)
def test_bonus_requires_independently_fresh_matching_dispatch(dispatch_source, expected):
    result = _plan(
        _intervals(CheapClassification.BONUS_DISPATCH),
        dispatch_source=dispatch_source,
    )
    assert result.status is expected


def test_fabricated_complete_result_does_not_bypass_source_proof():
    intervals = _intervals(CheapClassification.BONUS_DISPATCH)
    fabricated = TrustedImportResult(CoverageStatus.COMPLETE, intervals=_trusted(intervals).intervals)
    result = _plan(
        intervals,
        trusted_import=fabricated,
        import_source=None,
        dispatch_source=None,
    )
    assert result.status is ReservePlanningStatus.UNAVAILABLE


def test_export_trusted_empty_does_not_gate_standard_recharge():
    export_result = CheapWindowResult(CoverageStatus.TRUSTED_EMPTY)
    assert export_result.coverage_status is CoverageStatus.TRUSTED_EMPTY
    result = _plan(_intervals(CheapClassification.STANDARD_CHEAP))
    assert result.status is ReservePlanningStatus.COMPLETE


def test_interval_gap_overlap_and_reverse_order_are_invalid():
    first = ReserveInputInterval(TimeInterval(NOW, NOW + timedelta(hours=1)), Decimal(0), Decimal(0), CheapClassification.NOT_CHEAP)
    for second in (
        ReserveInputInterval(TimeInterval(NOW + timedelta(hours=2), NOW + timedelta(hours=3)), Decimal(0), Decimal(0), CheapClassification.NOT_CHEAP),
        ReserveInputInterval(TimeInterval(NOW + timedelta(minutes=30), NOW + timedelta(hours=2)), Decimal(0), Decimal(0), CheapClassification.NOT_CHEAP),
        ReserveInputInterval(TimeInterval(NOW - timedelta(hours=1), NOW), Decimal(0), Decimal(0), CheapClassification.NOT_CHEAP),
    ):
        result = _plan((first, second), end=second.interval.end)
        assert result.status is ReservePlanningStatus.INVALID


def test_dst_fold_horizon_uses_utc_instants_and_exact_continuity():
    london = ZoneInfo("Europe/London")
    first_start = datetime(2026, 10, 25, 1, 0, tzinfo=london, fold=0)
    fold_boundary = datetime(2026, 10, 25, 1, 0, tzinfo=london, fold=1)
    end = datetime(2026, 10, 25, 2, 0, tzinfo=london, fold=1)
    intervals = (
        ReserveInputInterval(TimeInterval(first_start, fold_boundary), Decimal(0), Decimal(0), CheapClassification.NOT_CHEAP),
        ReserveInputInterval(TimeInterval(fold_boundary, end), Decimal(0), Decimal(0), CheapClassification.NOT_CHEAP),
    )
    planning_now = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    result = _plan(
        intervals,
        now=planning_now,
        import_source=RateSourceObservation(planning_now, IMPORT_SOURCE),
    )
    assert result.status is ReservePlanningStatus.COMPLETE
    assert result.trajectory[0].end_energy_kwh == result.trajectory[1].start_energy_kwh
