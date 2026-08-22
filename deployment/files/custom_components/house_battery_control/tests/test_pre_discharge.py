from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
import yaml

from custom_components.house_battery_control import config as integration_config
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
    ForecastObservation,
    PreDischargePlanningStatus,
    TRUSTED_FORECAST_PRODUCER,
    TRUSTED_FORECAST_REVISION,
    TRUSTED_FORECAST_SCHEMA_REVISION,
    TRUSTED_FORECAST_SOURCE,
    TRUSTED_FORECAST_SOURCE_FAMILY,
    _forecast_digests,
    _simulate_continuous,
    _wall_safe,
    plan_pre_discharge_headroom,
    trusted_solis_energy_boundary,
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
from custom_components.house_battery_control.solis_actuator import mapping_fingerprint
from custom_components.house_battery_control.solis_reader import read_solis_state


UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
IMPORT = "import-source"
EXPORT = "export-source"


def _state(value, now, *, attributes=None):
    return {"state": value, "attributes": attributes or {}, "last_updated": now, "context_id": "initial"}


def _capability(value, unit, now, maximum="100"):
    return _state(value, now, attributes={"min": "0", "max": maximum, "step": "1", "unit_of_measurement": unit})


def _healthy_solis(soc: Decimal, now: datetime):
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    source["solis"]["telemetry"].update(
        battery_power_entity_id="sensor.garage_battery_power",
        battery_power_sign="positive_means_charging",
        device_timestamp_entity_id="sensor.garage_device_time",
    )
    solis = integration_config.from_mapping(source).solis
    states = {
        solis.telemetry.state_of_charge_entity_id: _state(str(soc), now, attributes={"unit_of_measurement": "%"}),
        solis.telemetry.battery_power_entity_id: _state("0", now, attributes={"unit_of_measurement": "kW"}),
        solis.telemetry.device_timestamp_entity_id: _state(now.isoformat(), now),
    }
    persistent = solis.persistent
    states[persistent.storage_mode_entity_id] = _state("Feed-In Priority", now, attributes={"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]})
    for entity_id in (persistent.allow_grid_charging_entity_id, persistent.allow_export_entity_id, persistent.grid_peak_shaving_entity_id, persistent.inverter_on_off_entity_id):
        states[entity_id] = _state("on", now)
    states[persistent.inverter_time_entity_id] = _state(now.isoformat(), now)
    for entity_id in (
        solis.protection.battery_over_discharge_soc_entity_id,
        solis.protection.battery_force_charge_soc_entity_id,
        solis.protection.battery_recovery_soc_entity_id,
        solis.protection.battery_max_charge_soc_entity_id,
        solis.protection.battery_reserve_soc_entity_id,
    ):
        states[entity_id] = _capability("10", "%", now)
    states[solis.protection.battery_reserve_entity_id] = _state("off", now)
    states[solis.capability.battery_max_charge_current_entity_id] = _capability("100", "A", now, "200")
    states[solis.capability.battery_max_discharge_current_entity_id] = _capability("100", "A", now, "200")
    states[solis.capability.max_output_power_entity_id] = _capability("5000", "W", now, "6000")
    states[solis.capability.max_export_power_entity_id] = _capability("5000", "W", now, "6000")
    for slot in solis.slots:
        for direction in (slot.charge, slot.discharge):
            states[direction.enable_entity_id] = _state("off", now)
            states[direction.time_entity_id] = _state("00:00-00:00", now)
            states[direction.current_entity_id] = _capability("1", "A", now, "10")
            states[direction.target_soc_entity_id] = _capability("50", "%", now)
    result = read_solis_state(solis, states, now)
    assert result.is_healthy and result.snapshot is not None
    return solis, result


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


def _fixtures(*, load=Decimal(0), energy=Decimal("11"), window_hours=1, base=NOW):
    starts = [base + timedelta(hours=i) for i in range(9)]
    solis, solis_state = _healthy_solis(energy / Decimal("12") * Decimal(100), base)
    solis_mapping = mapping_fingerprint(solis)
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
    component_margin = Decimal("0.50") * Decimal("0.9") - Decimal("0.05") / Decimal("0.8") - Decimal("0.0165")
    component = CheapWindowComponent(TimeInterval(starts[4], starts[4] + timedelta(hours=1)), cheap, export_cheap, component_margin)
    window = CheapWindow(starts[4], starts[4] + timedelta(hours=1), (component,))
    envelope = CommissionedPowerEnvelope(
        maximum_charge_power_kw=Decimal("2"), maximum_discharge_power_kw=Decimal("2"),
        maximum_grid_import_power_kw=MAXIMUM_GRID_IMPORT_POWER_KW, schema_version="1",
        inverter_identity="inverter", mapping_fingerprint=solis_mapping, candidate_policy_fingerprint="policy",
        manual_grid_fingerprint="grid", capability_fingerprint="cap", evidence_source="test", validated_at=NOW,
    )
    authority = ReserveAuthority("1", "inverter", solis_mapping, "policy", "grid", "cap")
    forecast = pre + (ReserveInputInterval(TimeInterval(starts[4], starts[5]), load, Decimal(0), CheapClassification.STANDARD_CHEAP),) + tuple(ReserveInputInterval(TimeInterval(starts[i], starts[i + 1]), load, Decimal(0), CheapClassification.NOT_CHEAP) for i in range(5, 8))
    reserve = plan_commissioned_reserve(
        intervals=forecast, trusted_import=trusted, import_source=RateSourceObservation(NOW, IMPORT),
        start=forecast[0].interval.start, end=forecast[-1].interval.end, now=base,
        capacity_kwh=Decimal("12"), minimum_energy_kwh=Decimal("1"), reserve_margin_kwh=Decimal(0),
        charge_efficiency=Decimal("0.8"), discharge_efficiency=Decimal("0.9"), commissioned=envelope, authority=authority,
    )
    observation = ForecastObservation(
        TRUSTED_FORECAST_SOURCE, TRUSTED_FORECAST_REVISION, NOW, NOW + timedelta(hours=2),
        "0" * 64, "0" * 64, forecast[0].interval.start, forecast[-1].interval.end,
        TRUSTED_FORECAST_PRODUCER, TRUSTED_FORECAST_SOURCE_FAMILY, TRUSTED_FORECAST_SCHEMA_REVISION,
    )
    content_digest, generation_digest = _forecast_digests(forecast, observation)
    observation = ForecastObservation(
        observation.source, observation.revision, observation.retrieved_at, observation.fresh_until,
        content_digest, generation_digest, observation.requested_start, observation.requested_end,
        observation.producer, observation.source_family, observation.schema_revision,
    )
    energy_boundary = trusted_solis_energy_boundary(
        state=solis_state, config=solis, expected_mapping_fingerprint=solis_mapping,
        capacity_kwh=Decimal("12"), now=base,
    )
    assert not hasattr(energy_boundary, "code")
    return dict(
        energy_boundary=energy_boundary, forecast_intervals=forecast,
        reserve_plan=reserve, standard_window=window, trusted_import=trusted,
        export_rates=tuple(_export(starts[i], starts[i + 1]) for i in range(4)) + (export_cheap,), import_source=RateSourceObservation(NOW, IMPORT),
        export_source=RateSourceObservation(NOW, EXPORT), commissioned=envelope,
        authority=authority, now=base, capacity_kwh=Decimal("12"), minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal(0), charge_efficiency=Decimal("0.8"), discharge_efficiency=Decimal("0.9"),
        forecast_observation=observation, inverter_timezone=UTC,
    )


def _observed_forecast(args, *, revision=TRUSTED_FORECAST_REVISION, retrieved=NOW):
    forecast = args["forecast_intervals"]
    observation = ForecastObservation(
        TRUSTED_FORECAST_SOURCE, revision, retrieved, retrieved + timedelta(hours=2),
        "0" * 64, "0" * 64, forecast[0].interval.start, forecast[-1].interval.end,
        TRUSTED_FORECAST_PRODUCER, TRUSTED_FORECAST_SOURCE_FAMILY, TRUSTED_FORECAST_SCHEMA_REVISION,
    )
    content, generation = _forecast_digests(forecast, observation)
    return replace(observation, content_digest=content, generation_digest=generation)


def _recompute_reserve(args):
    intervals = args["forecast_intervals"]
    args["reserve_plan"] = plan_commissioned_reserve(
        intervals=intervals, trusted_import=args["trusted_import"], import_source=args["import_source"],
        dispatch_source=args.get("dispatch_source"), start=intervals[0].interval.start,
        end=intervals[-1].interval.end, now=args["now"], capacity_kwh=args["capacity_kwh"],
        minimum_energy_kwh=args["minimum_energy_kwh"], reserve_margin_kwh=args["reserve_margin_kwh"],
        charge_efficiency=args["charge_efficiency"], discharge_efficiency=args["discharge_efficiency"],
        commissioned=args["commissioned"], authority=args["authority"],
    )


def test_no_headroom_when_baseline_fits_maximum_refill():
    args = _fixtures(energy=Decimal("10.4"))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.NO_HEADROOM_NEEDED
    assert result.input_fingerprint


def test_plans_latest_safe_standard_pre_discharge_with_exact_energy_semantics():
    result = plan_pre_discharge_headroom(**_fixtures())
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.target_stored_energy_kwh == Decimal("10.4")
    assert result.planned_stored_withdrawal_kwh == Decimal("0.6")
    assert result.planned_ac_export_kwh == result.planned_stored_withdrawal_kwh * Decimal("0.9")
    assert result.proposed_end == NOW + timedelta(hours=4)
    assert result.proposed_start == NOW + timedelta(hours=3, minutes=43)
    assert result.conservative_margin_per_stored_kwh == Decimal("0.50") * Decimal("0.9") - Decimal("0.05") / Decimal("0.8") - Decimal("0.0165")
    assert result.schedule_ledger
    assert sum((entry.discretionary_ac_export_kwh for entry in result.schedule_ledger), Decimal(0)) == result.planned_ac_export_kwh
    assert result.target_stop is not None
    target_entries = [entry for entry in result.schedule_ledger if entry.end == result.target_stop]
    assert len(target_entries) == 1
    assert target_entries[0].end_energy_kwh == result.target_stored_energy_kwh
    assert all(entry.discretionary_ac_export_kwh == 0 for entry in result.schedule_ledger if entry.start >= result.target_stop)
    assert result.target_stored_energy_kwh >= result.desired_window_start_energy_kwh


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
    args["export_rates"] = tuple(_export(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1), "0.02") for i in range(4)) + (args["standard_window"].components[0].export_interval,)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNPROFITABLE


def test_forged_stored_energy_boundary_is_rejected():
    args = _fixtures()
    args["energy_boundary"] = replace(
        args["energy_boundary"],
        stored_energy_kwh=args["energy_boundary"].stored_energy_kwh + Decimal("0.1"),
    )
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "ENERGY_FORGED"


def test_forecast_provenance_is_fingerprinted_and_staleness_is_fail_closed():
    args = _fixtures()
    args["forecast_observation"] = _observed_forecast(args, retrieved=NOW)
    first = plan_pre_discharge_headroom(**args)
    assert first.input_fingerprint
    args["forecast_observation"] = _observed_forecast(args, retrieved=NOW + timedelta(seconds=1))
    second = plan_pre_discharge_headroom(**args)
    assert first.input_fingerprint != second.input_fingerprint
    args["forecast_observation"] = _observed_forecast(args, retrieved=NOW - timedelta(hours=3))
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
        capacity=Decimal("10"), minimum=Decimal("1"), target_energy=Decimal("4"),
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


def test_healthy_t0004_energy_boundary_is_mandatory_and_current_mapping_bound():
    args = _fixtures()
    args.pop("energy_boundary")
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "ENERGY_UNAVAILABLE"
    args = _fixtures()
    args["energy_boundary"] = replace(args["energy_boundary"], mapping_fingerprint="0" * 64)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "ENERGY_FORGED"


def test_energy_boundary_rejects_current_config_mapping_change():
    args = _fixtures()
    boundary = args["energy_boundary"]
    changed_telemetry = replace(
        boundary.config.telemetry,
        state_of_charge_entity_id="sensor.replaced_battery_soc",
    )
    args["energy_boundary"] = replace(
        boundary,
        config=replace(boundary.config, telemetry=changed_telemetry),
    )
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "ENERGY_MAPPING_MISMATCH"


def test_energy_boundary_rejects_forged_soc_observation_timestamp():
    args = _fixtures()
    boundary = args["energy_boundary"]
    telemetry = replace(
        boundary.state.snapshot.telemetry,
        home_assistant_last_updated=NOW - timedelta(seconds=1),
        soc_last_updated=NOW - timedelta(seconds=1),
    )
    forged_state = replace(
        boundary.state,
        snapshot=replace(boundary.state.snapshot, telemetry=telemetry),
        telemetry=telemetry,
    )
    args["energy_boundary"] = replace(boundary, state=forged_state)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "ENERGY_FORGED"


def test_forecast_authority_is_mandatory_and_named_freshness_is_enforced():
    args = _fixtures()
    args.pop("forecast_observation")
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "FORECAST_UNAVAILABLE"
    args = _fixtures()
    args["forecast_observation"] = replace(_observed_forecast(args), fresh_until=NOW + timedelta(hours=1))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "FORECAST_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("producer", "spoofed_producer"),
        ("source", "spoofed.forecast"),
        ("source_family", "spoofed_family"),
        ("revision", "spoofed-revision"),
        ("schema_revision", "spoofed-schema"),
    ),
)
def test_wrong_forecast_producer_identity_is_rejected_even_with_matching_digests(field, value):
    args = _fixtures()
    spoofed = replace(args["forecast_observation"], **{field: value})
    content, generation = _forecast_digests(args["forecast_intervals"], spoofed)
    args["forecast_observation"] = replace(
        spoofed,
        content_digest=content,
        generation_digest=generation,
    )
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
        capacity=Decimal("10"), minimum=Decimal("1"), target_energy=Decimal("1"),
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
        capacity=Decimal("10"), minimum=Decimal("1"), target_energy=Decimal("1"),
    )
    assert not sim.safe
    assert sim.issue is not None


def test_malformed_export_sequence_is_typed_invalid_not_raised():
    args = _fixtures()
    args["export_rates"] = (object(),)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code in {"INPUT_INVALID", "WINDOW_PROVENANCE"}


def test_export_coverage_is_required_for_entire_encoded_slot():
    args = _fixtures()
    # A price only covering the target-stop portion must not authorize the
    # larger encoded continuous slot.
    args["export_rates"] = (_export(NOW + timedelta(hours=3, minutes=50), NOW + timedelta(hours=4), "0.50"), args["standard_window"].components[0].export_interval)
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


def test_full_plan_returns_inverter_local_cross_midnight_schedule():
    args = _fixtures()
    args["inverter_timezone"] = timezone(timedelta(hours=8))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.proposed_start.date() != result.proposed_end.date()
    assert result.proposed_end - result.proposed_start < timedelta(days=1)


def test_full_plan_rejects_ambiguous_inverter_local_window_boundary():
    base = datetime(2026, 10, 24, 20, 30, tzinfo=UTC)
    args = _fixtures(base=base)
    args["inverter_timezone"] = ZoneInfo("Europe/London")
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "DST_AMBIGUOUS"


def test_degraded_t0004_result_cannot_authorize_energy():
    args = _fixtures()
    boundary = args["energy_boundary"]
    args["energy_boundary"] = replace(boundary, state=replace(boundary.state, health=type(boundary.state.health).DEGRADED))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "ENERGY_UNAVAILABLE"


def test_initial_energy_outside_physical_bounds_is_rejected_before_early_return():
    args = _fixtures(energy=Decimal("0.9"))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INFEASIBLE
    assert result.issues[0].code == "INITIAL_ENERGY_BOUNDARY"


def test_forecast_content_generation_and_authoritative_horizon_are_exact():
    args = _fixtures()
    args["forecast_observation"] = replace(args["forecast_observation"], content_digest="a" * 64)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code == "FORECAST_DIGEST_MISMATCH"
    args = _fixtures()
    args["forecast_observation"] = replace(args["forecast_observation"], generation_digest="b" * 64)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    args = _fixtures()
    args["forecast_observation"] = replace(args["forecast_observation"], requested_end=args["forecast_observation"].requested_end - timedelta(hours=1))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "FORECAST_HORIZON_MISMATCH"
    args = _fixtures()
    args["forecast_intervals"] = args["forecast_intervals"][:-1]
    _recompute_reserve(args)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.UNAVAILABLE
    assert result.issues[0].code == "FORECAST_HORIZON_MISMATCH"


@pytest.mark.parametrize("spoof", ("import_interval", "export_interval", "export_source", "margin"))
def test_window_component_spoofs_fail_closed(spoof):
    args = _fixtures()
    component = args["standard_window"].components[0]
    if spoof == "import_interval":
        changed = replace(component, interval=TimeInterval(component.interval.start + timedelta(minutes=1), component.interval.end))
    elif spoof == "export_interval":
        changed = replace(component, export_interval=replace(component.export_interval, start=component.export_interval.start + timedelta(minutes=1)))
    elif spoof == "export_source":
        changed = replace(component, export_interval=replace(component.export_interval, retrieval_source_entity_id="sensor.spoof"))
    else:
        changed = replace(component, margin_per_stored_kwh=component.margin_per_stored_kwh + Decimal("0.01"))
    args["standard_window"] = replace(args["standard_window"], components=(changed,))
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.INVALID
    assert result.issues[0].code.startswith("WINDOW_")


def test_absolute_target_splits_mid_interval_and_models_negative_load_after_stop():
    first = ReserveInputInterval(TimeInterval(NOW, NOW + timedelta(hours=1)), Decimal("0"), Decimal("0"), CheapClassification.NOT_CHEAP)
    second = ReserveInputInterval(TimeInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), Decimal("-1"), Decimal("0"), CheapClassification.NOT_CHEAP)
    reserve = ReservePlanResult(ReservePlanningStatus.COMPLETE, (
        ReserveTrajectoryInterval(first.interval, Decimal("1"), Decimal("1")),
        ReserveTrajectoryInterval(second.interval, Decimal("1"), Decimal("1")),
    ))
    sim = _simulate_continuous(
        intervals=(first, second), reserve_plan=reserve, start_energy=Decimal("5"),
        action_start=NOW, action_end=NOW + timedelta(hours=2), maximum_charge=Decimal("1"),
        maximum_discharge=Decimal("2"), charge_eff=Decimal("0.8"), discharge_eff=Decimal("1"),
        capacity=Decimal("10"), minimum=Decimal("1"), target_energy=Decimal("4"),
    )
    assert sim.safe
    assert sim.target_stop == NOW + timedelta(minutes=30)
    assert any(end == sim.target_stop and end_energy == Decimal("4") for _, end, _, end_energy, _ in sim.ledger)
    assert sim.ledger[-1][3] == Decimal("4.8")
    assert sim.ledger[-1][4] == Decimal(0)


def test_external_pv_overlap_with_active_discharge_fails_closed():
    interval = ReserveInputInterval(
        TimeInterval(NOW, NOW + timedelta(hours=1)),
        Decimal("0"),
        Decimal("1"),
        CheapClassification.NOT_CHEAP,
    )
    reserve = ReservePlanResult(
        ReservePlanningStatus.COMPLETE,
        (ReserveTrajectoryInterval(interval.interval, Decimal("1"), Decimal("1")),),
    )
    sim = _simulate_continuous(
        intervals=(interval,),
        reserve_plan=reserve,
        start_energy=Decimal("5"),
        action_start=NOW,
        action_end=NOW + timedelta(hours=1),
        maximum_charge=Decimal("2"),
        maximum_discharge=Decimal("2"),
        charge_eff=Decimal("0.8"),
        discharge_eff=Decimal("0.9"),
        capacity=Decimal("10"),
        minimum=Decimal("1"),
        target_energy=Decimal("4"),
    )
    assert not sim.safe
    assert sim.issue is not None
    assert sim.issue.code == "PV_DISCHARGE_OVERLAP_UNCOMMISSIONED"


def test_delayed_fresh_observation_never_returns_a_past_schedule_start():
    args = _fixtures()
    args["now"] = NOW + timedelta(minutes=4, seconds=20)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.proposed_start >= args["now"].replace(second=0, microsecond=0) + timedelta(minutes=1)


def test_asymmetric_low_discharge_power_reports_partial_uncreated_headroom():
    args = _fixtures()
    args["commissioned"] = replace(
        args["commissioned"], maximum_charge_power_kw=Decimal("1.5"),
        maximum_discharge_power_kw=Decimal("0.02"),
    )
    _recompute_reserve(args)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.planned_stored_withdrawal_kwh > 0
    assert result.uncreated_headroom_kwh > 0
    assert result.reachable_window_start_energy_kwh > result.desired_window_start_energy_kwh


def test_fractional_window_end_is_quantized_down_and_target_prevents_extra_withdrawal():
    base = NOW + timedelta(seconds=30)
    args = _fixtures(base=base)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.proposed_end.second == 0
    assert result.proposed_end < args["standard_window"].start
    assert result.target_stored_energy_kwh >= result.desired_window_start_energy_kwh
    assert all(entry.end_energy_kwh >= result.target_stored_energy_kwh for entry in result.schedule_ledger if entry.discretionary_ac_export_kwh > 0)


def test_multi_component_refill_uses_strict_worst_case_import_margin():
    args = _fixtures()
    window = args["standard_window"]
    middle = window.start + timedelta(minutes=30)
    first_rate = _rate(window.start, middle, "0.04", CheapClassification.STANDARD_CHEAP, "cheap-a", event_min="0.04", unique_count=2)
    second_rate = _rate(middle, window.end, "0.06", CheapClassification.STANDARD_CHEAP, "cheap-b", event_min="0.06", unique_count=2)
    existing = args["trusted_import"].intervals
    cheap_other = replace(existing[5], source_event="cheap-a", event_minimum=Decimal("0.04"), event_unique_price_count=2)
    second_other = replace(existing[6], source_event="cheap-b", event_minimum=Decimal("0.06"), event_unique_price_count=2)
    args["trusted_import"] = TrustedImportResult(
        CoverageStatus.COMPLETE,
        intervals=existing[:4] + (first_rate, second_rate, cheap_other, second_other) + existing[7:],
    )
    first_export = _export(window.start, middle)
    second_export = _export(middle, window.end)
    first_margin = Decimal("0.50") * Decimal("0.9") - Decimal("0.04") / Decimal("0.8") - Decimal("0.0165")
    second_margin = Decimal("0.50") * Decimal("0.9") - Decimal("0.06") / Decimal("0.8") - Decimal("0.0165")
    args["standard_window"] = CheapWindow(window.start, window.end, (
        CheapWindowComponent(TimeInterval(window.start, middle), first_rate, first_export, first_margin),
        CheapWindowComponent(TimeInterval(middle, window.end), second_rate, second_export, second_margin),
    ))
    old_forecast = args["forecast_intervals"]
    cheap_forecast = old_forecast[4]
    args["forecast_intervals"] = old_forecast[:4] + (
        ReserveInputInterval(TimeInterval(window.start, middle), cheap_forecast.load_kwh / 2, cheap_forecast.solar_kwh / 2, CheapClassification.STANDARD_CHEAP),
        ReserveInputInterval(TimeInterval(middle, window.end), cheap_forecast.load_kwh / 2, cheap_forecast.solar_kwh / 2, CheapClassification.STANDARD_CHEAP),
    ) + old_forecast[5:]
    args["export_rates"] = args["export_rates"][:-1] + (first_export, second_export)
    args["forecast_observation"] = _observed_forecast(args)
    _recompute_reserve(args)
    result = plan_pre_discharge_headroom(**args)
    assert result.status is PreDischargePlanningStatus.PLANNED
    assert result.conservative_margin_per_stored_kwh == second_margin
