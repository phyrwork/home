from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
    PreDischargePlanningStatus,
    plan_pre_discharge_headroom,
)
from custom_components.house_battery_control.reserve_planner import (
    CommissionedPowerEnvelope,
    ReserveAuthority,
    ReserveInputInterval,
    ReservePlanResult,
    ReservePlanningStatus,
    ReserveTrajectoryInterval,
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
    starts = [NOW + timedelta(hours=i) for i in range(5)]
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
    ) + (cheap, cheap_other)
    trusted = TrustedImportResult(CoverageStatus.COMPLETE, intervals=imports)
    export_rates = tuple(_export(starts[i], starts[i + 1]) for i in range(4))
    export_cheap = _export(starts[4], starts[4] + timedelta(hours=1))
    component = CheapWindowComponent(TimeInterval(starts[4], starts[4] + timedelta(hours=1)), cheap, export_cheap, Decimal("0.1"))
    window = CheapWindow(starts[4], starts[4] + timedelta(hours=1), (component,))
    trajectory = tuple(
        ReserveTrajectoryInterval(item.interval, Decimal("1"), Decimal("1"))
        for item in pre
    )
    reserve = ReservePlanResult(ReservePlanningStatus.COMPLETE, trajectory=trajectory)
    envelope = CommissionedPowerEnvelope(
        maximum_charge_power_kw=Decimal("2"), maximum_discharge_power_kw=Decimal("2"),
        maximum_grid_import_power_kw=MAXIMUM_GRID_IMPORT_POWER_KW, schema_version="1",
        inverter_identity="inverter", mapping_fingerprint="map", candidate_policy_fingerprint="policy",
        manual_grid_fingerprint="grid", capability_fingerprint="cap", evidence_source="test", validated_at=NOW,
    )
    authority = ReserveAuthority("1", "inverter", "map", "policy", "grid", "cap")
    return dict(
        energy=BatteryEnergyObservation(energy, NOW, "battery", "revision-1"), forecast_intervals=pre,
        reserve_plan=reserve, standard_window=window, trusted_import=trusted,
        export_rates=export_rates, import_source=RateSourceObservation(NOW, IMPORT),
        export_source=RateSourceObservation(NOW, EXPORT), commissioned=envelope,
        authority=authority, now=NOW, capacity_kwh=Decimal("12"), minimum_energy_kwh=Decimal("1"),
        reserve_margin_kwh=Decimal(0), charge_efficiency=Decimal("0.8"), discharge_efficiency=Decimal("0.9"),
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
    second = plan_pre_discharge_headroom(**args)
    assert first.input_fingerprint != second.input_fingerprint
