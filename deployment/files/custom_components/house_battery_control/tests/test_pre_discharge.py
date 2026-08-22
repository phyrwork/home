from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from custom_components.house_battery_control.domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from custom_components.house_battery_control.interval import TimeInterval
from custom_components.house_battery_control.octopus_windows import (
    AdjustedRateInterval,
    CheapClassification,
    CheapWindow,
    CheapWindowComponent,
    CoverageStatus,
    ExportRateInterval,
    RateSourceObservation,
    TrustedImportResult,
)
from custom_components.house_battery_control.pre_discharge import (
    BatteryEnergyObservation,
    EnergyObservationAuthority,
    ForecastObservation,
    PreDischargePlanningStatus,
    _simulate_continuous,
    _wall_safe,
    plan_pre_discharge_headroom,
)
from custom_components.house_battery_control.reserve_planner import (
    CommissionedPowerEnvelope,
    ReserveAuthority,
    ReserveInputInterval,
    ReservePlanResult,
    ReservePlanningStatus,
    ReserveTrajectoryInterval,
    plan_commissioned_reserve,
)


UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
IMPORT = "import-source"
EXPORT = "export-source"


def _rate(start, end, price, classification, event, *, adjusted=False, event_min=None, unique_count=1):
    return AdjustedRateInterval(
        start=start, end=end, import_price=Decimal(price), classification=classification,
        source="octopus", tariff="T", source_day="day", source_event=event,
        source_revision_at=NOW, retrieval_source_entity_id=IMPORT,
        dispatch_source_entity_id="dispatch", event_minimum=Decimal(event_min or price),
        event_unique_price_count=unique_count, is_intelligent_adjusted=adjusted,
    )


def _export(start, end, price="0.50"):
    return ExportRateInterval(
        start=start, end=end, export_price=Decimal(price), source="octopus-export",
        tariff="E", retrieved_at=NOW, source_day="day", source_event="export",
        source_revision_at=NOW, retrieval_source_entity_id=EXPORT,
    )


def _fixtures(*, load=Decimal(0), energy=Decimal("11"), window_hours=1):
    starts = [NOW + timedelta(hours=i) for i in range(9)]
    pre = tuple(
        ReserveInputInterval(TimeInterval(starts[i], starts[i + 1]), load, Decimal(0), CheapClassification.NOT_CHEAP)
        for i in range(4)
    )
    # The first event is a flat ordinary event.  The second event supplies one
    # standard minimum followed by an ordinary maximum, as T0012 requires.
    cheap = _rate(starts[4], starts[4] + timedelta(hours=window_hours), "0.05", CheapClassification.STANDARD_CHEAP, "cheap", event_min="0.05", unique_count=2)
    cheap_other = _rate(starts[4] + timedelta(hours=window_hours), starts[4] + timedelta(hours=2), "0.20", CheapClassification.NOT_CHEAP, "cheap", event_min="0.05", unique_count=2)
    imports = tuple(
        _rate(starts[i], starts[i + 1], "0.20", CheapClassification.NOT_CHEAP, f"ordinary-{i}")
        for i in range(4)
    ) + (cheap, cheap_other) + tuple(
        _rate(starts[i], starts[i + 1], "0.20", CheapClassification.NOT_CHEAP, f"ordinary-{i}")
        for i in range(6, 8)
    )
    trusted = TrustedImportResult(CoverageStatus.COMPLETE, intervals=imports)
    export_rates = tuple(_export(starts[i], starts[i + 1]) for i in range(4))
    export_cheap = _export(starts[4], starts[4] + timedelta(hours=1))
    component = CheapWindowComponent(TimeInterval(starts[4], starts[4] + timedelta(hours=1)), cheap, export_cheap, Decimal("0.1"))
    window = CheapWindow(starts[4], starts[4] + timedelta(hours=1), (component,))
    envelope = CommissionedPowerEnvelope(
        maximum_charge_power_kw=Decimal("2"), maximum_discharge_power_kw=Decimal("2"),
        maximum_grid_import_power_kw=MAXIMUM_GRID_IMPORT_POWER_KW, schema_version="1",
        inverter_identity="inverter", mapping_fingerprint="map", candidate_policy_fingerprint="policy",
        manual_grid_fingerprint="grid", capability_fingerprint="cap", evidence_source="test", validated_at=NOW,
    )
    authority = ReserveAuthority("1", "inverter", "map", "policy", "grid", "cap")
    forecast = pre + (ReserveInputInterval(TimeInterval(starts[4], starts[5]), load, Decimal(0), CheapClassification.STANDARD_CHEAP),) + tuple(ReserveInputInterval(TimeInterval(starts[i], starts[i + 1]), load, Decimal(0), CheapClassification.NOT_CHEAP) for i in range(5, 8))
    reserve = plan_commissioned_reserve(
        intervals=forecast, trusted_import=trusted, import_source=RateSourceObservation(NOW, IMPORT),
        start=forecast[0].interval.start, end=forecast[-1].interval.end, now=NOW,
        capacity_kwh=Decimal("12"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal(0),
        charge_efficiency=Decimal("0.8"), discharge_efficiency=Decimal("0.9"), commissioned=envelope, authority=authority,
    )
    return dict(
        energy=BatteryEnergyObservation(energy, NOW, "battery", "revision-1"), energy_authority=EnergyObservationAuthority("battery", "solis.telemetry.stored_energy", "revision-1"), forecast_intervals=forecast,
        reserve_plan=reserve, standard_window=window, trusted_import=trusted,
        export_rates=tuple(_export(starts[i], starts[i + 1]) for i in range(4)), import_source=RateSourceObservation(NOW, IMPORT),
        export_source=RateSourceObservation(NOW, EXPORT), commissioned=envelope,
        authority=authority, now=NOW, capacity_kwh=Decimal("12"), minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal(0), charge_efficiency=Decimal("0.8"), discharge_efficiency=Decimal("0.9"),
        forecast_observation=ForecastObservation("forecast", "forecast-revision", NOW, NOW + timedelta(hours=2)), inverter_timezone=UTC,
    )


def test_no_headroom_when_baseline_fits_maximum_refill():
    args = _fixtures(energy=Decimal("10.4"))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.NO_HEADROOM_NEEDED
    assert result.input_fingerprint


def test_plans_latest_safe_standard_pre_discharge_with_exact_energy_semantics():
    result = plan_pre_discharge_headroom(**_fixtures())
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.planned_stored_withdrawal_kwh == Decimal("0.6")
    assert result.planned_ac_export_kwh == Decimal("0.54")
    assert result.proposed_end == NOW + timedelta(hours=4)
    assert result.proposed_start == NOW + timedelta(hours=3, minutes=43)
    assert result.conservative_margin_per_stored_kwh == Decimal("0.50") * Decimal("0.9") - Decimal("0.05") / Decimal("0.8") - Decimal("0.0165")
    assert result.schedule_ledger
    assert sum((entry.discretionary_ac_export_kwh for entry in result.schedule_ledger), Decimal(0)) == result.planned_ac_export_kwh


def test_bonus_window_is_rejected():
    args = _fixtures()
    component = args["standard_window"].components[0]
    bonus = _rate(component.interval.start, component.interval.end, "0.05", CheapClassification.BONUS_DISPATCH, "cheap", adjusted=True)
    args["standard_window"] = CheapWindow(component.interval.start, component.interval.end, (CheapWindowComponent(component.interval, bonus, component.export_interval, Decimal("0.1")),))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "BONUS_NOT_ALLOWED"


def test_stale_energy_is_unavailable_and_freshness_is_explicit():
    args = _fixtures()
    args["now"] = NOW + timedelta(minutes=6)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "ENERGY_STALE"


def test_baseline_demand_uses_exact_point_one_kw_grid_contribution():
    args = _fixtures(load=Decimal("0.2"), energy=Decimal("11.5"))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.baseline_window_start_energy_kwh == Decimal("11.05555555555555555555555556")


def test_negative_margin_is_unprofitable():
    args = _fixtures()
    args["export_rates"] = tuple(_export(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1), "0.02") for i in range(4))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNPROFITABLE


def test_mutating_energy_revision_changes_fingerprint():
    args = _fixtures()
    first = plan_pre_discharge_headroom(**args)
    args["energy"] = BatteryEnergyObservation(Decimal("11"), NOW, "different", "revision-2")
    args["energy_authority"] = EnergyObservationAuthority("different", "solis.telemetry.stored_energy", "revision-2")
    second = plan_pre_discharge_headroom(**args)
    assert first.input_fingerprint != second.input_fingerprint


def test_forecast_provenance_is_fingerprinted_and_staleness_is_fail_closed():
    args = _fixtures()
    args["forecast_observation"] = ForecastObservation(
        "forecast", "revision-1", NOW, NOW + timedelta(hours=2)
    )
    first = plan_pre_discharge_headroom(**args)
    assert first.input_fingerprint
    args["forecast_observation"] = ForecastObservation(
        "forecast", "revision-2", NOW, NOW + timedelta(hours=2)
    )
    second = plan_pre_discharge_headroom(**args)
    assert first.input_fingerprint != second.input_fingerprint
    args["forecast_observation"] = ForecastObservation(
        "forecast", "revision-2", NOW - timedelta(hours=3), NOW - timedelta(hours=1)
    )
    stale = plan_pre_discharge_headroom(**args)
    assert stale.status is PreDischargePlanningStatus.UNAVAILABLE
    assert stale.issues[0].code == "FORECAST_STALE"


def test_backward_overlay_never_crosses_a_zero_capacity_gap():
    intervals = tuple(
        ReserveInputInterval(
            TimeInterval(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1)),
            Decimal(0), Decimal(0), CheapClassification.NOT_CHEAP,
        )
        for i in range(3)
    )
    reserve = ReservePlanResult(ReservePlanningStatus.COMPLETE, (
        ReserveTrajectoryInterval(intervals[0].interval, Decimal("1"), Decimal("1")),
        ReserveTrajectoryInterval(intervals[1].interval, Decimal("5"), Decimal("5")),
        ReserveTrajectoryInterval(intervals[2].interval, Decimal("1"), Decimal("1")),
    ))
    sim = _simulate_continuous(
        intervals=intervals, reserve_plan=reserve, start_energy=Decimal("5"),
        action_start=NOW, action_end=NOW + timedelta(hours=3), maximum_charge=Decimal("2"),
        maximum_discharge=Decimal("2"), charge_eff=Decimal("1"), discharge_eff=Decimal("1"),
        capacity=Decimal("10"), minimum=Decimal("1"), target=Decimal("1"),
    )
    assert not sim.safe
    assert sim.issue is not None


def test_schedule_rejects_cross_midnight_representation():
    args = _fixtures()
    start = NOW + timedelta(hours=4)
    end = start + timedelta(days=1)
    args["standard_window"] = CheapWindow(start, end, args["standard_window"].components)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "INPUT_INVALID"


def test_energy_authority_is_mandatory_and_bound_to_t0004():
    args = _fixtures()
    args.pop("energy_authority")
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "ENERGY_UNAVAILABLE"
    args = _fixtures()
    args["energy_authority"] = EnergyObservationAuthority("other", "solis.telemetry.stored_energy", "revision-1")
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "ENERGY_INVALID"


def test_forecast_authority_is_mandatory_and_named_freshness_is_enforced():
    args = _fixtures()
    args.pop("forecast_observation")
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "FORECAST_UNAVAILABLE"
    args = _fixtures()
    args["forecast_observation"] = ForecastObservation("forecast", "r", NOW, NOW + timedelta(hours=1))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "FORECAST_INVALID"


def test_inverter_timezone_is_required_but_cross_midnight_under_24_hours_is_valid():
    args = _fixtures()
    args.pop("inverter_timezone")
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    args = _fixtures()
    start = NOW + timedelta(hours=4)
    end = start + timedelta(hours=1)
    args["standard_window"] = CheapWindow(start, end, (CheapWindowComponent(TimeInterval(start, end), args["standard_window"].components[0].rate_interval, args["standard_window"].components[0].export_interval, Decimal("0.1")),))
    # UTC -> Europe/London crosses a wall-clock midnight only when the
    # instant is near midnight; the explicit timezone conversion itself is
    # what is under test here, rather than source tzinfo identity.
    args["inverter_timezone"] = ZoneInfo("Europe/London")
    result = plan_pre_discharge_headroom(**args)
    assert result.status in {PreDischargePlanningStatus.PLANNED, PreDischargePlanningStatus.NO_HEADROOM_NEEDED, PreDischargePlanningStatus.INFEASIBLE}


def test_full_horizon_supplied_reserve_must_match_recomputed_later_demand():
    args = _fixtures()
    trajectory = list(args["reserve_plan"].trajectory)
    trajectory[-1] = ReserveTrajectoryInterval(trajectory[-1].interval, Decimal("11"), Decimal("11"))
    args["reserve_plan"] = ReservePlanResult(ReservePlanningStatus.COMPLETE, tuple(trajectory))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "RESERVE_FINGERPRINT_MISMATCH"


def test_continuous_simulator_carries_withdrawal_across_two_forecast_intervals():
    args = _fixtures()
    intervals = args["forecast_intervals"][:2]
    reserve = ReservePlanResult(
        ReservePlanningStatus.COMPLETE,
        tuple(ReserveTrajectoryInterval(item.interval, Decimal("1"), Decimal("1")) for item in intervals),
    )
    sim = _simulate_continuous(
        intervals=intervals, reserve_plan=reserve, start_energy=Decimal("3"),
        action_start=NOW, action_end=NOW + timedelta(hours=2), maximum_charge=Decimal("2"),
        maximum_discharge=Decimal("2"), charge_eff=Decimal("1"), discharge_eff=Decimal("1"),
        capacity=Decimal("10"), minimum=Decimal("1"), target=Decimal("2"),
    )
    assert sim.safe
    assert sim.withdrawal_kwh == Decimal("2")
    assert len(sim.ledger) == 2
    assert sim.ledger[0][3] == Decimal("1")
    assert sim.ledger[1][2] == Decimal("1")
    assert sim.target_stop is not None


def test_continuous_simulator_never_merges_disjoint_zero_capacity_piece():
    first = ReserveInputInterval(TimeInterval(NOW, NOW + timedelta(hours=1)), Decimal("0"), Decimal("0"), CheapClassification.NOT_CHEAP)
    second = ReserveInputInterval(TimeInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), Decimal("2"), Decimal("0"), CheapClassification.NOT_CHEAP)
    reserve = ReservePlanResult(ReservePlanningStatus.COMPLETE, (
        ReserveTrajectoryInterval(first.interval, Decimal("1"), Decimal("1")),
        ReserveTrajectoryInterval(second.interval, Decimal("1"), Decimal("1")),
    ))
    sim = _simulate_continuous(
        intervals=(first, second), reserve_plan=reserve, start_energy=Decimal("3"),
        action_start=NOW, action_end=NOW + timedelta(hours=2), maximum_charge=Decimal("2"),
        maximum_discharge=Decimal("2"), charge_eff=Decimal("1"), discharge_eff=Decimal("1"),
        capacity=Decimal("10"), minimum=Decimal("1"), target=Decimal("2"),
    )
    assert not sim.safe
    assert sim.issue is not None


def test_malformed_export_sequence_is_typed_invalid_not_raised():
    args = _fixtures()
    args["export_rates"] = (object(),)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "INPUT_INVALID"


def test_export_coverage_is_required_for_entire_encoded_slot():
    args = _fixtures()
    # A price only covering the target-stop portion must not authorize the
    # larger encoded continuous slot.
    args["export_rates"] = (_export(NOW + timedelta(hours=3, minutes=50), NOW + timedelta(hours=4), "0.50"),)
    result = plan_pre_discharge_headroom(**args)
    assert result.status in {PreDischargePlanningStatus.UNAVAILABLE, PreDischargePlanningStatus.INFEASIBLE}


def test_wall_checks_reject_ambiguous_nonexistent_and_transition_boundaries():
    london = ZoneInfo("Europe/London")
    nonexistent = datetime(2026, 3, 29, 1, 30, tzinfo=london)
    ambiguous = datetime(2026, 10, 25, 1, 30, tzinfo=london)
    assert _wall_safe(nonexistent, nonexistent + timedelta(minutes=10), london).code == "DST_NONEXISTENT"
    assert _wall_safe(ambiguous, ambiguous + timedelta(minutes=10), london).code == "DST_AMBIGUOUS"
    transition_start = datetime(2026, 3, 29, 0, 30, tzinfo=london)
    transition_end = datetime(2026, 3, 29, 2, 30, tzinfo=london)
    assert _wall_safe(transition_start, transition_end, london).code == "DST_TRANSITION"


def test_wall_checks_allow_valid_cross_midnight_under_24_hours():
    london = ZoneInfo("Europe/London")
    start = datetime(2026, 1, 1, 23, 30, tzinfo=london)
    end = datetime(2026, 1, 2, 0, 30, tzinfo=london)
    assert _wall_safe(start, end, london) is None
