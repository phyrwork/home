"""Focused lifecycle and safety tests for the MVP coordinator."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.const import DOMAIN
from custom_components.house_battery_control.contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from custom_components.house_battery_control.coordinator import (
    HEARTBEAT_INTERVAL,
    Coordinator,
)
from custom_components.house_battery_control.octopus_windows import CoverageStatus
from custom_components.house_battery_control.solis_policy import PolicyActuationResult
from custom_components.house_battery_control.runtime_inputs import RuntimeUnavailable
from custom_components.house_battery_control.strategy import CycleState, StrategyAction, StrategyResult


NOW = datetime(2026, 8, 22, 10, tzinfo=UTC)


def config() -> integration_config.Config:
    from pathlib import Path

    path = Path(__file__).parents[3] / "house_battery_control.yaml"
    source = yaml.safe_load(path.read_text())
    source["dynamic_control_enabled"] = False
    return integration_config.from_mapping(source)


def policy(coordinator: Coordinator, *, safe: bool = True) -> MagicMock:
    instance = MagicMock()
    instance.async_apply_safe_baseline = AsyncMock(
        return_value=PolicyActuationResult(safe, safe, "safe" if safe else "unsafe")
    )
    instance.async_apply_healthy = AsyncMock(
        return_value=PolicyActuationResult(True, False, "healthy")
    )
    coordinator.policy_actuator = instance
    return instance


def observation(
    health: ControllerHealth = ControllerHealth.HEALTHY,
    *,
    storage_mode: str = "Self-Use",
    battery_reserve: bool = False,
    slot_enabled: bool = False,
) -> SimpleNamespace:
    telemetry = SimpleNamespace(
        state_of_charge_percent=Decimal("55"),
        battery_power_kw=Decimal("0"),
    )
    persistent = SimpleNamespace(
        storage_mode=storage_mode,
        battery_reserve=battery_reserve,
    )
    slots = tuple(
        SimpleNamespace(
            charge=SimpleNamespace(enabled=slot_enabled),
            discharge=SimpleNamespace(enabled=False),
        )
        for _ in range(6)
    )
    return SimpleNamespace(
        health=health,
        snapshot=SimpleNamespace(persistent=persistent, slots=slots, telemetry=telemetry),
        telemetry=telemetry,
        issues=(),
    )


async def test_disabled_controller_applies_safe_state_once(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "on")
    actuator = policy(coordinator)
    with patch(
        "custom_components.house_battery_control.coordinator.read_solis_state",
        return_value=observation(),
    ):
        first = await coordinator._async_update_data()
        second = await coordinator._async_update_data()

    assert first.action is StrategyAction.FAIL_SAFE
    assert second.action is StrategyAction.STOP
    assert second.reason == "dynamic control is disabled"
    assert second.health is ControllerHealth.HEALTHY
    actuator.async_apply_safe_baseline.assert_awaited_once_with()


async def test_disabled_healthy_snapshot_includes_exact_reserve_diagnostics(
    hass: HomeAssistant,
) -> None:
    coordinator = Coordinator(hass, config())
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "on")
    actuator = policy(coordinator)
    runtime = SimpleNamespace(
        strategy=SimpleNamespace(reserve_soc_percent=Decimal("38")),
        reserve=SimpleNamespace(reserve_energy_kwh=Decimal("12.345678")),
    )
    with (
        patch(
            "custom_components.house_battery_control.coordinator.read_solis_state",
            return_value=observation(),
        ),
        patch(
            "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
            AsyncMock(return_value=runtime),
        ),
    ):
        await coordinator._async_update_data()
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.HEALTHY
    assert result.reserve_soc_percent == Decimal("38")
    assert result.battery_energy_kwh == Decimal("17.68448")
    assert result.reserve_target_energy_kwh == Decimal("12.345678")
    assert result.reserve_balance_kwh == Decimal("5.338802")
    actuator.async_apply_safe_baseline.assert_awaited_once_with()
    actuator.async_apply_healthy.assert_not_awaited()


async def test_disabled_diagnostic_failure_does_not_degrade_proven_safe_state(
    hass: HomeAssistant,
) -> None:
    coordinator = Coordinator(hass, config())
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "on")
    actuator = policy(coordinator)
    with (
        patch(
            "custom_components.house_battery_control.coordinator.read_solis_state",
            return_value=observation(),
        ),
        patch(
            "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
            AsyncMock(side_effect=RuntimeError("forecast unavailable")),
        ),
    ):
        await coordinator._async_update_data()
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.HEALTHY
    assert result.battery_energy_kwh == Decimal("17.68448")
    assert result.reserve_target_energy_kwh is None
    assert result.reserve_balance_kwh is None
    actuator.async_apply_safe_baseline.assert_awaited_once_with()


async def test_disabled_controller_fault_reports_fail_safe_without_write_loop(
    hass: HomeAssistant,
) -> None:
    coordinator = Coordinator(hass, config())
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "on")
    actuator = policy(coordinator)
    with patch(
        "custom_components.house_battery_control.coordinator.read_solis_state",
        side_effect=(observation(), observation(ControllerHealth.DEGRADED), observation(ControllerHealth.DEGRADED)),
    ):
        await coordinator._async_update_data()
        await coordinator._async_update_data()
        failed = await coordinator._async_update_data()
        repeated = await coordinator._async_update_data()

    assert failed.health is ControllerHealth.FAIL_SAFE
    assert repeated.health is ControllerHealth.FAIL_SAFE
    assert actuator.async_apply_safe_baseline.await_count == 3


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("storage_mode", "Feed-In Priority"),
        ("battery_reserve", True),
        ("slot_enabled", True),
    ),
)
async def test_disabled_controller_reapplies_safe_state_after_external_drift(
    hass: HomeAssistant,
    field: str,
    value: object,
) -> None:
    coordinator = Coordinator(hass, config())
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "on")
    actuator = policy(coordinator)
    safe = observation()
    drifted = observation(**{field: value})
    with patch(
        "custom_components.house_battery_control.coordinator.read_solis_state",
        side_effect=(safe, drifted, safe),
    ):
        first = await coordinator._async_update_data()
        healthy = await coordinator._async_update_data()
        drift = await coordinator._async_update_data()
        recovered = await coordinator._async_update_data()

    assert first.health is ControllerHealth.FAIL_SAFE
    assert healthy.health is ControllerHealth.HEALTHY
    assert drift.health is ControllerHealth.FAIL_SAFE
    assert recovered.health is ControllerHealth.HEALTHY
    assert actuator.async_apply_safe_baseline.await_count == 2


async def test_enabled_controller_recovers_from_fresh_degraded_telemetry(
    hass: HomeAssistant,
) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    degraded = SimpleNamespace(
        solis=observation(ControllerHealth.DEGRADED),
        strategy=SimpleNamespace(reserve_soc_percent=Decimal("20")),
        reserve=None,
        current_window=None,
        next_window=None,
    )
    healthy = SimpleNamespace(
        solis=observation(),
        strategy=SimpleNamespace(reserve_soc_percent=Decimal("20")),
        reserve=None,
        current_window=None,
        next_window=None,
    )
    decision = StrategyResult(
        StrategyAction.IDLE,
        None,
        CycleState.IDLE,
        "normal operation",
    )
    with (
        patch(
            "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
            AsyncMock(side_effect=(degraded, degraded, healthy)),
        ),
        patch(
            "custom_components.house_battery_control.coordinator.select_strategy",
            return_value=decision,
        ),
    ):
        first = await coordinator._async_update_data()
        repeated = await coordinator._async_update_data()
        recovered = await coordinator._async_update_data()

    assert first.health is ControllerHealth.DEGRADED
    assert first.action is StrategyAction.STOP
    assert repeated.health is ControllerHealth.DEGRADED
    assert recovered.health is ControllerHealth.HEALTHY
    assert hass.states.get(coordinator.config.control_disable_guard_entity_id).state == "off"
    actuator.async_apply_safe_baseline.assert_awaited_once_with()
    actuator.async_apply_healthy.assert_awaited_once()


async def test_degraded_baseline_failure_escalates_to_fail_safe(
    hass: HomeAssistant,
) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator, safe=False)
    degraded = SimpleNamespace(
        solis=observation(ControllerHealth.DEGRADED),
        strategy=SimpleNamespace(reserve_soc_percent=Decimal("20")),
        reserve=None,
        current_window=None,
        next_window=None,
    )
    with patch(
        "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
        AsyncMock(return_value=degraded),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.FAIL_SAFE
    assert result.action is StrategyAction.FAIL_SAFE
    assert "could not be proven" in result.reason
    assert actuator.async_apply_safe_baseline.await_count == 2


async def test_degraded_baseline_retries_once_before_escalating(
    hass: HomeAssistant,
) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    actuator.async_apply_safe_baseline = AsyncMock(
        side_effect=(
            PolicyActuationResult(False, False, "first readback incomplete"),
            PolicyActuationResult(True, True, "retry proven"),
        )
    )
    degraded = SimpleNamespace(
        solis=observation(ControllerHealth.DEGRADED),
        strategy=SimpleNamespace(reserve_soc_percent=Decimal("20")),
        reserve=None,
        current_window=None,
        next_window=None,
    )
    with patch(
        "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
        AsyncMock(return_value=degraded),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.DEGRADED
    assert result.action is StrategyAction.STOP
    assert result.actuation_message == "retry proven"
    assert actuator.async_apply_safe_baseline.await_count == 2


async def test_runtime_unavailable_exception_is_recoverable_degraded(
    hass: HomeAssistant,
) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    with patch(
        "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
        AsyncMock(side_effect=RuntimeUnavailable("Solis telemetry is unavailable")),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.DEGRADED
    assert result.action is StrategyAction.STOP
    assert result.last_error == "Solis telemetry is unavailable"
    actuator.async_apply_safe_baseline.assert_awaited_once_with()


@pytest.mark.parametrize(
    "issue",
    (
        "import rate source revision is in the future",
        "export rate retrieval is in the future",
        "export rate interval provenance is stale",
    ),
)
async def test_future_or_interval_provenance_fault_fails_safe(
    hass: HomeAssistant,
    issue: str,
) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    rates = (SimpleNamespace(end=NOW + timedelta(hours=1)),)
    state = SimpleNamespace(state="10", attributes={})

    with (
        patch.object(Coordinator, "_now", return_value=NOW),
        patch(
            "custom_components.house_battery_control.runtime_inputs.read_solis_state",
            return_value=observation(),
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs._state",
            return_value=state,
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs._attribute",
            side_effect=(rates, rates),
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs.parse_fused_import_rates",
            return_value=rates,
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs.parse_fused_export_rates",
            return_value=rates,
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs._rate_source",
            return_value=SimpleNamespace(),
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs._dispatch_source",
            return_value=SimpleNamespace(),
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs.evaluate_cheap_windows",
            return_value=SimpleNamespace(
                coverage_status=CoverageStatus.UNAVAILABLE,
                issues=(issue,),
                windows=(),
            ),
        ),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.FAIL_SAFE
    assert result.action is StrategyAction.FAIL_SAFE
    assert issue in (result.last_error or "")
    actuator.async_apply_safe_baseline.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("attribute", "value"),
    (
        ("rates", None),
        ("rates", "unavailable"),
        ("dispatch_source_last_retrieved", None),
        ("dispatch_source_last_retrieved", "unknown"),
    ),
)
async def test_missing_or_unavailable_tariff_attribute_is_degraded(
    hass: HomeAssistant,
    attribute: str,
    value: object,
) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    rates = (SimpleNamespace(end=NOW + timedelta(hours=1)),)
    import_attributes = {
        "rates": (),
        "rate_source_last_retrieved": NOW.isoformat(),
        "rate_source_entity_id": "sensor.import",
        "dispatch_source_last_retrieved": NOW.isoformat(),
        "dispatch_source_entity_id": "sensor.dispatch",
    }
    import_attributes[attribute] = value
    export_attributes = {
        "rates": (),
        "rate_source_last_retrieved": NOW.isoformat(),
        "rate_source_entity_id": "sensor.export",
    }
    states = (
        SimpleNamespace(state="off", entity_id=coordinator.config.control_disable_guard_entity_id, attributes={}),
        SimpleNamespace(
            state="available",
            entity_id=coordinator.config.tariff.import_rates_entity_id,
            attributes=import_attributes,
        ),
        SimpleNamespace(
            state="available",
            entity_id=coordinator.config.tariff.export_rates_entity_id,
            attributes=export_attributes,
        ),
        SimpleNamespace(
            state="10",
            entity_id=coordinator.config.cycle_discharge_duration_entity_id,
            attributes={},
        ),
    )

    with (
        patch.object(Coordinator, "_now", return_value=NOW),
        patch(
            "custom_components.house_battery_control.runtime_inputs.read_solis_state",
            return_value=observation(),
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs._state",
            side_effect=states,
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs.parse_fused_import_rates",
            return_value=rates,
        ),
        patch(
            "custom_components.house_battery_control.runtime_inputs.parse_fused_export_rates",
            return_value=rates,
        ),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.DEGRADED
    assert result.action is StrategyAction.STOP
    assert "has no usable" in (result.last_error or "")
    actuator.async_apply_safe_baseline.assert_awaited_once_with()


async def test_hard_fault_reconciles_again_after_degraded_baseline(
    hass: HomeAssistant,
) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    with patch(
        "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
        AsyncMock(
            side_effect=(
                RuntimeUnavailable("Solis telemetry is unavailable"),
                RuntimeError("planner invariant failed"),
            )
        ),
    ):
        degraded = await coordinator._async_update_data()
        failed = await coordinator._async_update_data()

    assert degraded.health is ControllerHealth.DEGRADED
    assert failed.health is ControllerHealth.FAIL_SAFE
    assert actuator.async_apply_safe_baseline.await_count == 2


async def test_enabled_unexpected_failure_fails_safe(hass: HomeAssistant) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    with patch(
        "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
        AsyncMock(side_effect=RuntimeError("input failed")),
    ):
        result = await coordinator._async_update_data()

    assert result.health is ControllerHealth.FAIL_SAFE
    assert result.action is StrategyAction.FAIL_SAFE
    assert "critical input" in result.reason
    actuator.async_apply_safe_baseline.assert_awaited_once_with()


async def test_cycle_deadline_is_stored_and_reused_across_heartbeats(hass: HomeAssistant) -> None:
    source = yaml.safe_load(
        (__import__("pathlib").Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    source["dynamic_control_enabled"] = True
    coordinator = Coordinator(hass, integration_config.from_mapping(source))
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    actuator = policy(coordinator)
    deadline = NOW + timedelta(minutes=10)
    intent = SlotIntent(
        SlotOwner.FULL_SOC_CYCLING,
        1,
        SlotDirection.DISCHARGE,
        NOW,
        deadline,
        Decimal("5"),
        Decimal("20"),
        deadline,
    )
    runtime = SimpleNamespace(
        strategy=SimpleNamespace(reserve_soc_percent=Decimal("10")),
        solis=SimpleNamespace(
            health=ControllerHealth.HEALTHY,
            snapshot=SimpleNamespace(telemetry=SimpleNamespace(
                state_of_charge_percent=Decimal("100"), battery_power_kw=Decimal("0")
            )),
        ),
        reserve=SimpleNamespace(reserve_energy_kwh=Decimal("3")),
        current_window=None,
        next_window=None,
    )
    decision = StrategyResult(
        StrategyAction.CYCLE_DISCHARGE,
        intent,
        CycleState.DISCHARGING,
        "test cycle",
    )
    with (
        patch(
            "custom_components.house_battery_control.coordinator.async_read_runtime_inputs",
            AsyncMock(side_effect=(runtime, runtime)),
        ) as read_runtime,
        patch(
            "custom_components.house_battery_control.coordinator.select_strategy",
            return_value=decision,
        ),
    ):
        await coordinator._async_update_data()
        await coordinator._async_update_data()

    assert coordinator._cycle_deadline == deadline
    assert read_runtime.await_args_list[0].kwargs["cycle_deadline"] is None
    assert read_runtime.await_args_list[1].kwargs["cycle_deadline"] == deadline
    actuator.async_apply_healthy.assert_awaited()


async def test_source_refresh_is_ignored_after_stop(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    coordinator._stopping = True
    coordinator.async_request_refresh = AsyncMock()
    await coordinator._async_source_changed(object())  # type: ignore[arg-type]
    coordinator.async_request_refresh.assert_not_awaited()


async def test_stop_is_idempotent_and_unsubscribes(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, config())
    policy_instance = policy(coordinator)
    unsub = MagicMock()
    coordinator._unsub_sources = unsub
    await coordinator.async_stop()
    await coordinator.async_stop()

    assert coordinator._stopping
    unsub.assert_called_once_with()
    policy_instance.async_apply_safe_baseline.assert_awaited_once_with()


async def test_shutdown_applies_baseline_without_latching_guard(
    hass: HomeAssistant,
) -> None:
    coordinator = Coordinator(hass, config())
    hass.states.async_set(coordinator.config.control_disable_guard_entity_id, "off")
    policy(coordinator)

    await coordinator.async_stop()

    assert coordinator._safe_state_applied
    assert hass.states.get(coordinator.config.control_disable_guard_entity_id).state == "off"


def test_heartbeat_interval_is_one_minute() -> None:
    assert HEARTBEAT_INTERVAL.total_seconds() == 60
