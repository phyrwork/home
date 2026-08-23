from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.house_battery_control.octopus_windows import (
    AdjustedRateInterval,
    CheapClassification,
    CoverageStatus,
    DispatchSourceObservation,
    RateSourceObservation,
    evaluate_trusted_import_rates,
)
from custom_components.house_battery_control.runtime_inputs import (
    RuntimeUnavailable,
    _cycle_duration,
    _runtime_powers,
    _slot_start,
    _solis_failure_is_transient,
    async_read_runtime_inputs,
)
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


def test_unavailable_solis_entity_is_a_transient_runtime_failure() -> None:
    parsed, states = fixture()
    entity_id = parsed.telemetry.state_of_charge_entity_id
    states[entity_id]["state"] = "unavailable"
    result = read_solis_state(parsed, states, NOW)
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda requested: SimpleNamespace(state=states[requested]["state"])
            if requested in states
            else None
        )
    )

    assert _solis_failure_is_transient(hass, result)


def test_invalid_available_solis_value_is_a_runtime_invariant() -> None:
    parsed, states = fixture()
    entity_id = parsed.telemetry.state_of_charge_entity_id
    states[entity_id]["state"] = "101"
    result = read_solis_state(parsed, states, NOW)
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda requested: SimpleNamespace(state=states[requested]["state"])
            if requested in states
            else None
        )
    )

    assert not _solis_failure_is_transient(hass, result)


def test_future_solis_device_timestamp_is_a_runtime_invariant() -> None:
    parsed, states = fixture()
    entity_id = parsed.telemetry.device_timestamp_entity_id
    states[entity_id]["state"] = (NOW + timedelta(minutes=5)).isoformat()
    result = read_solis_state(parsed, states, NOW)
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda requested: SimpleNamespace(state=states[requested]["state"])
            if requested in states
            else None
        )
    )

    assert any(issue.code == "device_timestamp_future" for issue in result.issues)
    assert not _solis_failure_is_transient(hass, result)


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


@pytest.mark.asyncio
async def test_stale_dispatch_window_is_a_recoverable_runtime_failure(hass) -> None:
    from pathlib import Path
    import yaml

    from custom_components.house_battery_control.config import from_mapping
    from custom_components.house_battery_control.strategy import CycleState

    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    source["dynamic_control_enabled"] = True
    config = from_mapping(source)
    hass.states.async_set(config.control_disable_guard_entity_id, "off")

    state = SimpleNamespace(state="10", attributes={})
    import_rates = (SimpleNamespace(end=NOW + timedelta(hours=1)),)
    export_rates = (SimpleNamespace(end=NOW + timedelta(hours=1)),)
    solis = SimpleNamespace(health=__import__(
        "custom_components.house_battery_control.contracts", fromlist=["ControllerHealth"]
    ).ControllerHealth.HEALTHY, snapshot=SimpleNamespace())

    with (
        patch("custom_components.house_battery_control.runtime_inputs.read_solis_state", return_value=solis),
        patch("custom_components.house_battery_control.runtime_inputs._state", return_value=state),
        patch("custom_components.house_battery_control.runtime_inputs._attribute", side_effect=(import_rates, export_rates)),
        patch("custom_components.house_battery_control.runtime_inputs.parse_fused_import_rates", return_value=import_rates),
        patch("custom_components.house_battery_control.runtime_inputs.parse_fused_export_rates", return_value=export_rates),
        patch("custom_components.house_battery_control.runtime_inputs._rate_source", return_value=SimpleNamespace()),
        patch("custom_components.house_battery_control.runtime_inputs._dispatch_source", return_value=SimpleNamespace()),
        patch("custom_components.house_battery_control.runtime_inputs.evaluate_cheap_windows", return_value=SimpleNamespace(
            coverage_status=CoverageStatus.UNAVAILABLE,
            issues=("dispatch source observation is stale",),
            windows=(),
        )),
    ):
        with pytest.raises(RuntimeUnavailable, match="dispatch source observation is stale"):
            await async_read_runtime_inputs(
                hass,
                config,
                now=NOW,
                cycle_state=CycleState.IDLE,
            )
