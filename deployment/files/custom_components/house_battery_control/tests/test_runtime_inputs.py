from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from custom_components.house_battery_control.octopus_windows import (
    AdjustedRateInterval,
    CheapClassification,
    CoverageStatus,
    DispatchSourceObservation,
    RateSourceObservation,
    evaluate_trusted_import_rates,
)
from custom_components.house_battery_control.runtime_inputs import _cycle_duration, _runtime_powers, _slot_start
from custom_components.house_battery_control.solis_reader import read_solis_state
from custom_components.house_battery_control.tests.test_solis_reader import NOW, fixture


def test_runtime_power_is_derived_from_voltage_and_current_capabilities() -> None:
    parsed, states = fixture()
    result = read_solis_state(parsed, states, NOW)

    assert result.snapshot is not None
    assert _runtime_powers(result.snapshot) == (Decimal("5.12"), Decimal("5.12"))


def test_runtime_power_uses_current_capabilities() -> None:
    parsed, states = fixture()
    result = read_solis_state(parsed, states, NOW)

    assert result.snapshot is not None
    assert _runtime_powers(result.snapshot) == (Decimal("5.12"), Decimal("5.12"))


def test_cycle_duration_is_validated_at_runtime() -> None:
    assert _cycle_duration(SimpleNamespace(state="10")).total_seconds() == 600


def test_cycle_duration_rejects_out_of_range_values() -> None:
    import pytest

    with pytest.raises(ValueError):
        _cycle_duration(SimpleNamespace(state="0"))
    with pytest.raises(ValueError):
        _cycle_duration(SimpleNamespace(state="10.5"))
    with pytest.raises(ValueError):
        _cycle_duration(SimpleNamespace(state="61"))


def test_active_clipped_window_slot_start_is_exact_minute() -> None:
    now = datetime(2026, 8, 23, 19, 39, 9, 445998, tzinfo=timezone.utc)

    assert _slot_start(now, now) == datetime(2026, 8, 23, 19, 39, tzinfo=timezone.utc)


def test_future_window_slot_start_is_not_rounded_earlier() -> None:
    now = datetime(2026, 8, 23, 19, 39, 9, 445998, tzinfo=timezone.utc)
    future_start = datetime(2026, 8, 23, 19, 39, 9, 445999, tzinfo=timezone.utc)

    assert _slot_start(now, future_start) == datetime(2026, 8, 23, 19, 40, tzinfo=timezone.utc)


def test_stale_bonus_dispatch_is_not_usable_for_reserve() -> None:
    rates = (AdjustedRateInterval(
        start=NOW, end=NOW + timedelta(hours=1), import_price=Decimal("0.07"),
        classification=CheapClassification.BONUS_DISPATCH, source="test", tariff="TEST",
        source_day="current", source_event="test", source_revision_at=NOW,
        retrieval_source_entity_id="sensor.import", dispatch_source_entity_id="sensor.dispatch",
        event_minimum=Decimal("0.07"), event_unique_price_count=1, is_intelligent_adjusted=True,
    ),)
    result = evaluate_trusted_import_rates(
        import_rates=rates,
        start=NOW,
        end=NOW + timedelta(hours=1),
        now=NOW,
        import_source=RateSourceObservation(NOW, "sensor.import"),
        dispatch_source=DispatchSourceObservation(NOW - timedelta(hours=1), "sensor.dispatch"),
    )
    assert result.coverage_status is CoverageStatus.UNAVAILABLE
