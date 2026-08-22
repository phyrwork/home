"""Offline lifecycle tests for the T0008 observation coordinator."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import battery, config, energy, planner, tariff
from custom_components.house_battery_control import coordinator as coordinator_module
from custom_components.house_battery_control.contracts import ControllerHealth
from custom_components.house_battery_control.coordinator import (
    FAIL_SAFE_ATTEMPT_BUDGET,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_STALE_AFTER,
    OBSERVATION_ONLY_LEGACY_POWER_LIMIT,
    Coordinator,
)
from custom_components.house_battery_control.interval import TimeInterval
from custom_components.house_battery_control.solis_policy import PolicyActuationResult, PolicyActuationStatus
from custom_components.house_battery_control.solis_actuator import SolisSlotActuator
from custom_components.house_battery_control.solis_policy import SolisPolicyActuator
from custom_components.house_battery_control.dependencies import stub_inverter
from custom_components.house_battery_control.solis_state import SolisStateReadResult, SolisTelemetry

NOW = datetime(2026, 7, 4, 10, tzinfo=UTC)
END = NOW + timedelta(hours=1)


def integration_config() -> config.Config:
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    return config.from_mapping(source)


def planning_source() -> planner.Input:
    interval = TimeInterval(NOW, END)
    return planner.Input(
        now=NOW,
        battery_spec=battery.Spec(Decimal("32.1536"), Decimal("3.21536"), Decimal("6"), Decimal("6"), Decimal("0.95"), Decimal("0.95")),
        battery_state=battery.State(Decimal("16.0768")),
        tariff_forecast=(tariff.TariffInterval(interval, tariff.Tariff(Decimal("0.3"), Decimal("0.15"), False)),),
        load_forecast=(energy.EnergyInterval(interval, Decimal("1")),),
        solar_forecast=(energy.EnergyInterval(interval, Decimal("0")),),
    )


def healthy_solis() -> SolisStateReadResult:
    telemetry = SolisTelemetry(Decimal("50"), Decimal("0"), NOW, NOW, NOW, NOW)
    return SolisStateReadResult(ControllerHealth.HEALTHY, object(), telemetry, None, (), ())  # type: ignore[arg-type]


def set_safe_proof(hass: HomeAssistant, configured: config.Config) -> None:
    assert configured.solis is not None
    for slot in configured.solis.slots:
        for direction in (slot.charge, slot.discharge):
            hass.states.async_set(direction.enable_entity_id, "off")
    hass.states.async_set(configured.solis.persistent.storage_mode_entity_id, "Self-Use")
    hass.states.async_set(configured.solis.persistent.grid_peak_shaving_entity_id, "on")
    hass.states.async_set(configured.solis.protection.battery_reserve_entity_id, "off")


def set_planner_helpers(hass: HomeAssistant, configured: config.Config) -> None:
    hass.states.async_set(configured.policy.reserve_margin_entity_id, "2")
    hass.states.async_set(configured.policy.export_hysteresis_entity_id, "1")


async def refresh(coordinator: Coordinator, hass: HomeAssistant) -> None:
    with (
        patch.object(coordinator_module.dt_util, "now", return_value=NOW),
        patch.object(coordinator_module, "read_solis_state", return_value=healthy_solis()),
        patch.object(coordinator_module.inputs, "async_read_input", AsyncMock(return_value=planning_source())),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()


def test_named_heartbeat_limits_and_periodic_coordinator(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, integration_config())
    assert HEARTBEAT_INTERVAL == timedelta(minutes=1)
    assert HEARTBEAT_STALE_AFTER == timedelta(minutes=3)
    assert coordinator.update_interval == HEARTBEAT_INTERVAL
    assert coordinator._fail_safe_obligation


@pytest.mark.parametrize(
    ("state", "expected_state", "quality"),
    (("on", "on", "valid"), ("off", "off", "valid"), ("unknown", "unknown", "invalid"), ("unavailable", "unavailable", "invalid"), ("invalid", "invalid", "invalid"), (None, None, "invalid")),
)
def test_guard_is_fail_closed(hass: HomeAssistant, state: str | None, expected_state: str | None, quality: str) -> None:
    configured = integration_config()
    if state is not None:
        hass.states.async_set(configured.control_disable_guard_entity_id, state)
    coordinator = Coordinator(hass, configured)
    assert coordinator._read_guard() == (expected_state, quality)


async def test_healthy_guard_off_window_is_observation_only(hass: HomeAssistant) -> None:
    configured = integration_config()
    set_safe_proof(hass, configured)
    set_planner_helpers(hass, configured)
    hass.states.async_set(configured.control_disable_guard_entity_id, "off")
    coordinator = Coordinator(hass, configured)

    await refresh(coordinator, hass)

    snapshot = coordinator.data
    assert snapshot is not None
    assert snapshot.health is ControllerHealth.HEALTHY
    assert not snapshot.fail_safe_obligation
    assert not snapshot.fail_safe_pending
    assert snapshot.fail_safe_proof is not None and snapshot.fail_safe_proof.ha_safe
    assert snapshot.fail_safe_proof.device_reconciliation_pending
    assert snapshot.diagnostic_energy_kwh == Decimal("16.0768")
    assert snapshot.recommendation is not None
    assert OBSERVATION_ONLY_LEGACY_POWER_LIMIT in snapshot.source_quality
    assert snapshot.planning_horizon_end == END


@pytest.mark.parametrize("guard", ("on", "unknown", "unavailable"))
async def test_asserted_or_invalid_guard_retains_obligation_without_redundant_write(hass: HomeAssistant, guard: str) -> None:
    configured = integration_config()
    set_safe_proof(hass, configured)
    set_planner_helpers(hass, configured)
    hass.states.async_set(configured.control_disable_guard_entity_id, guard)
    coordinator = Coordinator(hass, configured)

    await refresh(coordinator, hass)

    assert coordinator.data is not None
    assert coordinator.data.health is ControllerHealth.FAIL_SAFE
    assert coordinator.data.fail_safe_obligation
    assert not coordinator.data.fail_safe_pending


async def test_unexpected_reader_error_advances_heartbeat_and_starts_one_attempt(hass: HomeAssistant) -> None:
    configured = integration_config()
    hass.states.async_set(configured.control_disable_guard_entity_id, "off")
    coordinator = Coordinator(hass, configured)
    release = asyncio.Event()

    async def attempt(*, deadline):
        await release.wait()
        return PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE)

    coordinator._policy = AsyncMock()
    coordinator._policy.async_apply_fail_safe.side_effect = attempt
    with (
        patch.object(coordinator_module.dt_util, "now", return_value=NOW),
        patch.object(coordinator_module, "read_solis_state", side_effect=RuntimeError("boom")),
    ):
        await coordinator.async_refresh()
        first_task = coordinator._fail_safe_task
        await coordinator.async_refresh()
        await asyncio.sleep(0)

    assert coordinator.data is not None
    assert coordinator.data.heartbeat_at == NOW
    assert coordinator.data.unexpected_error == "RuntimeError: boom"
    assert coordinator.data.fail_safe_pending
    assert coordinator._fail_safe_task is first_task
    assert coordinator._policy.async_apply_fail_safe.await_count == 1
    coordinator._stopping = True
    release.set()
    assert first_task is not None
    await first_task


async def test_cancelled_cycle_propagates(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, integration_config())
    with patch.object(coordinator, "_async_observe", AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await coordinator._async_update_data()


async def test_event_refresh_uses_coordinator_coalescing_and_stops_cleanly(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, integration_config())
    coordinator.async_request_refresh = AsyncMock()
    await coordinator._async_source_changed(object())  # type: ignore[arg-type]
    coordinator._stopping = True
    await coordinator._async_source_changed(object())  # type: ignore[arg-type]
    coordinator.async_request_refresh.assert_awaited_once_with()


async def test_concurrent_data_coordinator_refreshes_are_coalesced(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, integration_config())
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    active = 0
    maximum_active = 0

    async def update():
        nonlocal calls, active, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        started.set()
        await release.wait()
        active -= 1
        return coordinator_module.Snapshot(heartbeat_at=NOW)

    coordinator._async_update_data = update  # type: ignore[method-assign]
    first = asyncio.create_task(coordinator.async_request_refresh())
    await started.wait()
    followers = [asyncio.create_task(coordinator.async_request_refresh()) for _ in range(4)]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, *followers)
    assert maximum_active == 1
    assert calls <= 2


def test_subscriptions_exclude_all_stub_control_and_soc_helpers(hass: HomeAssistant) -> None:
    configured = integration_config()
    ids = Coordinator(hass, configured)._source_entity_ids()
    assert configured.battery.state_of_charge_entity_id not in ids
    assert configured.inverter.operating_mode_entity_id not in ids
    assert configured.inverter.state_of_charge_target_entity_id not in ids
    assert configured.battery.power_limit_entity_id in ids
    assert configured.control_disable_guard_entity_id in ids


async def test_planner_receives_real_solis_result_and_never_reads_stub_soc(hass: HomeAssistant) -> None:
    configured = integration_config()
    set_safe_proof(hass, configured)
    set_planner_helpers(hass, configured)
    hass.states.async_set(configured.control_disable_guard_entity_id, "off")
    read = AsyncMock(return_value=planning_source())
    coordinator = Coordinator(hass, configured)
    observed = healthy_solis()
    with (
        patch.object(coordinator_module.dt_util, "now", return_value=NOW),
        patch.object(coordinator_module, "read_solis_state", return_value=observed),
        patch.object(coordinator_module.inputs, "async_read_input", read),
    ):
        await coordinator.async_refresh()
    assert read.await_args.kwargs["solis_result"] is observed


async def test_observation_never_calls_stub_candidate_or_slot_actuation(hass: HomeAssistant) -> None:
    configured = integration_config()
    set_safe_proof(hass, configured)
    set_planner_helpers(hass, configured)
    hass.states.async_set(configured.control_disable_guard_entity_id, "off")
    coordinator = Coordinator(hass, configured)
    with (
        patch.object(stub_inverter, "async_apply", AsyncMock()) as stub_apply,
        patch.object(SolisPolicyActuator, "async_apply_candidate", AsyncMock()) as candidate_apply,
        patch.object(SolisSlotActuator, "async_apply_intent", AsyncMock()) as slot_apply,
        patch.object(coordinator_module.dt_util, "now", return_value=NOW),
        patch.object(coordinator_module, "read_solis_state", return_value=healthy_solis()),
        patch.object(coordinator_module.inputs, "async_read_input", AsyncMock(return_value=planning_source())),
    ):
        await coordinator.async_refresh()
    stub_apply.assert_not_awaited()
    candidate_apply.assert_not_awaited()
    slot_apply.assert_not_awaited()


async def test_incomplete_common_horizon_is_degraded_and_removes_recommendation(hass: HomeAssistant) -> None:
    configured = integration_config()
    set_safe_proof(hass, configured)
    hass.states.async_set(configured.control_disable_guard_entity_id, "off")
    no_future = planning_source()
    no_future = planner.Input(no_future.now, no_future.battery_spec, no_future.battery_state, no_future.tariff_forecast, (), no_future.solar_forecast)
    coordinator = Coordinator(hass, configured)
    with (
        patch.object(coordinator_module.dt_util, "now", return_value=NOW),
        patch.object(coordinator_module, "read_solis_state", return_value=healthy_solis()),
        patch.object(coordinator_module.inputs, "async_read_input", AsyncMock(return_value=no_future)),
    ):
        await coordinator.async_refresh()
    assert coordinator.data is not None
    assert coordinator.data.recommendation is None
    assert coordinator.data.planning_horizon_end is None
    assert coordinator.data.health is ControllerHealth.FAIL_SAFE


def test_fresh_proof_contains_all_revision_bearing_states(hass: HomeAssistant) -> None:
    configured = integration_config()
    set_safe_proof(hass, configured)
    proof = Coordinator(hass, configured)._fresh_fail_safe_proof(NOW)
    assert proof.complete and proof.ha_safe
    assert len(proof.states) == 15
    assert all(item.last_updated is not None and item.revision for item in proof.states)


async def test_shutdown_reproves_and_retries_after_existing_unsafe_attempt(hass: HomeAssistant) -> None:
    configured = integration_config()
    hass.states.async_set(configured.control_disable_guard_entity_id, "on")
    coordinator = Coordinator(hass, configured)
    coordinator._policy = AsyncMock()
    coordinator._policy.async_apply_fail_safe.return_value = PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE)
    await coordinator._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
    existing = coordinator._fail_safe_task
    assert existing is not None
    await existing
    await coordinator.async_stop()
    assert coordinator._policy.async_apply_fail_safe.await_count >= 2
    assert coordinator._stopping


async def test_pending_fail_safe_survives_guard_transitions(hass: HomeAssistant) -> None:
    configured = integration_config()
    coordinator = Coordinator(hass, configured)
    release = asyncio.Event()

    async def attempt(*, deadline):
        await release.wait()
        return PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE)

    coordinator._policy = AsyncMock()
    coordinator._policy.async_apply_fail_safe.side_effect = attempt
    await coordinator._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
    task = coordinator._fail_safe_task
    assert task is not None
    for state in ("off", "on", "off"):
        hass.states.async_set(configured.control_disable_guard_entity_id, state)
        await refresh(coordinator, hass)
        assert coordinator._fail_safe_task is task
        assert not task.cancelled()
    coordinator._stopping = True
    release.set()
    await task


@pytest.mark.parametrize(
    "outcome",
    (
        PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION),
        RuntimeError("cloud failure"),
    ),
)
async def test_attempt_completion_uses_fresh_proof_and_requests_retry_refresh(hass: HomeAssistant, outcome: object) -> None:
    configured = integration_config()
    coordinator = Coordinator(hass, configured)
    coordinator.async_request_refresh = AsyncMock()
    coordinator._policy = AsyncMock()
    if isinstance(outcome, Exception):
        coordinator._policy.async_apply_fail_safe.side_effect = outcome
    else:
        coordinator._policy.async_apply_fail_safe.return_value = outcome
    await coordinator._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
    task = coordinator._fail_safe_task
    assert task is not None
    await task
    await asyncio.sleep(0)
    assert coordinator._last_fail_safe_attempt is not None
    assert coordinator._last_fail_safe_attempt.status in {"unsafe", "failed"}
    assert coordinator._last_fail_safe_attempt.proof is not None
    assert not coordinator._last_fail_safe_attempt.proof.ha_safe
    assert coordinator._fail_safe_obligation
    coordinator.async_request_refresh.assert_awaited_once_with()
    previous_id = coordinator._last_fail_safe_attempt.attempt_id
    release = asyncio.Event()

    async def retry(*, deadline):
        await release.wait()
        return PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE)

    coordinator._policy.async_apply_fail_safe.side_effect = retry
    with (
        patch.object(coordinator_module, "read_solis_state", return_value=healthy_solis()),
        patch.object(coordinator_module.inputs, "async_read_input", AsyncMock(return_value=planning_source())),
    ):
        await coordinator._async_observe(NOW)
    retry_task = coordinator._fail_safe_task
    assert retry_task is not None
    assert coordinator._last_fail_safe_attempt.attempt_id > previous_id
    coordinator._stopping = True
    release.set()
    await retry_task


async def test_done_before_callback_cannot_overwrite_new_attempt(hass: HomeAssistant) -> None:
    coordinator = Coordinator(hass, integration_config())
    coordinator._stopping = True
    coordinator._policy = AsyncMock()
    coordinator._policy.async_apply_fail_safe.return_value = PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE)
    await coordinator._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
    old = coordinator._fail_safe_task
    assert old is not None
    old.remove_done_callback(coordinator._fail_safe_done)
    await old
    old_id = coordinator._last_fail_safe_attempt.attempt_id

    await coordinator._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
    new = coordinator._fail_safe_task
    assert new is not None and new is not old
    new_id = coordinator._last_fail_safe_attempt.attempt_id
    coordinator._fail_safe_done(old)

    assert coordinator._fail_safe_task is new
    assert coordinator._last_fail_safe_attempt.attempt_id == new_id
    assert coordinator._last_fail_safe_attempt.status == "pending"
    assert coordinator._stale_fail_safe_attempts[-1].attempt_id == old_id
    await new


async def test_shutdown_unsubscribes_cancels_to_deadline_and_schedules_no_refresh(hass: HomeAssistant) -> None:
    configured = integration_config()
    coordinator = Coordinator(hass, configured)
    unsubscribe = MagicMock()
    coordinator._unsub_sources = unsubscribe
    coordinator.async_request_refresh = AsyncMock()
    never = asyncio.Event()

    async def blocked(*, deadline):
        await never.wait()

    coordinator._policy = AsyncMock()
    coordinator._policy.async_apply_fail_safe.side_effect = blocked
    await coordinator._start_fail_safe(timedelta(milliseconds=5))
    first = coordinator._fail_safe_task
    with patch.object(coordinator_module, "SHUTDOWN_FAIL_SAFE_BUDGET", timedelta(milliseconds=5)):
        await coordinator.async_stop()
    await asyncio.sleep(0)
    unsubscribe.assert_called_once_with()
    assert first is not None and first.done()
    assert coordinator._fail_safe_task is None or coordinator._fail_safe_task.done()
    coordinator.async_request_refresh.assert_not_awaited()
    assert not coordinator._attempts_by_task
