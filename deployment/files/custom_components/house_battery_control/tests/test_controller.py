"""Behavior tests for the single event-driven battery controller."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
import pytest
from homeassistant.core import HomeAssistant

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.controller import (
    BACKSTOP_INTERVAL,
    Controller,
    IMPORTANT_STOP_FAILSAFE_TIMEOUT,
    START_RETRY_DELAYS,
    WRITE_DEADLINE,
    StartRetry,
    _stop_retry_delay,
    _bonus_fingerprint,
    _preserve_standard_cheap_slot,
)
from custom_components.house_battery_control.model import (
    ControllerHealth,
    CycleState,
    LogicalIntent,
    SlotDirection,
    SlotIntent,
    SlotOwner,
    StrategyAction,
)
from custom_components.house_battery_control.planner import CheapClassification, Plan
from custom_components.house_battery_control.solis import (
    SlotKey,
    SolisState,
    WriteOutcome,
    WriteResult,
    read_state,
)
from custom_components.house_battery_control.tests.test_solis import fixture as solis_fixture

NOW = datetime(2026, 8, 22, 12, 0, 10, tzinfo=UTC)


def test_write_deadline_covers_pinned_service_retry_and_readback() -> None:
    assert WRITE_DEADLINE >= timedelta(seconds=75)
    assert WRITE_DEADLINE < timedelta(minutes=3)


def test_bonus_fingerprint_binds_dispatch_source_identity() -> None:
    component = SimpleNamespace(
        interval=SimpleNamespace(start=NOW, end=NOW + timedelta(minutes=15)),
        rate_interval=SimpleNamespace(
            classification=CheapClassification.BONUS_DISPATCH,
            import_price=Decimal("0.07"), source="import", source_event="event",
            dispatch_source_entity_id="binary_sensor.a", source_revision_at=NOW,
        ),
        export_interval=SimpleNamespace(
            start=NOW, end=NOW + timedelta(minutes=15), export_price=Decimal("0.15")
        ),
    )
    first = replace(plan(), current_cheap_window=SimpleNamespace(components=(component,)))
    second_rate = SimpleNamespace(**vars(component.rate_interval))
    second_rate.dispatch_source_entity_id = "binary_sensor.b"
    second_component = SimpleNamespace(
        interval=component.interval,
        rate_interval=second_rate,
        export_interval=component.export_interval,
    )
    second = replace(
        first, current_cheap_window=SimpleNamespace(components=(second_component,))
    )
    assert _bonus_fingerprint(first, NOW) != _bonus_fingerprint(second, NOW)


def test_standard_cheap_plan_explicitly_allows_rollover_slot_preservation() -> None:
    component = SimpleNamespace(
        interval=SimpleNamespace(start=NOW - timedelta(minutes=30), end=NOW + timedelta(minutes=30)),
        rate_interval=SimpleNamespace(classification=CheapClassification.STANDARD_CHEAP),
    )
    standard_plan = replace(
        plan(),
        action=StrategyAction.CHEAP_CHARGE,
        intent=LogicalIntent((SlotIntent(
            SlotOwner.CHEAP_CHARGING,
            SlotDirection.CHARGE,
            NOW - timedelta(minutes=30),
            NOW + timedelta(minutes=30),
            Decimal("100"), Decimal("100"), NOW + timedelta(minutes=30),
        ),)),
        current_cheap_window=SimpleNamespace(components=(component,)),
    )

    assert _preserve_standard_cheap_slot(standard_plan, NOW)
    assert not _preserve_standard_cheap_slot(
        replace(standard_plan, action=StrategyAction.CYCLE_DISCHARGE), NOW
    )


def config() -> integration_config.Config:
    source = yaml.safe_load(
        (Path(__file__).parents[3] / "house_battery_control.yaml").read_text()
    )
    return integration_config.from_mapping(source)


def observation(
    *,
    soc: str = "55",
    enabled: SlotKey | None = None,
    enabled_state: str = "on",
    mode: str = "Feed-In Priority",
    target_state: str | None = None,
    time_state: str | None = None,
    peak_state: str = "on",
) -> SolisState:
    parsed, states = solis_fixture()
    states[parsed.telemetry.state_of_charge_entity_id]["state"] = soc
    states[parsed.persistent.storage_mode_entity_id]["state"] = mode
    states[parsed.persistent.grid_peak_shaving_entity_id]["state"] = peak_state
    if enabled is not None:
        states[parsed.direction(enabled).enable_entity_id]["state"] = enabled_state
        if target_state is not None:
            states[parsed.direction(enabled).target_soc_entity_id]["state"] = target_state
        if time_state is not None:
            states[parsed.direction(enabled).time_entity_id]["state"] = time_state
    return read_state(states, parsed, now=NOW)


def plan(
    *,
    issue: str | None = None,
    state: CycleState = CycleState.IDLE,
    deadline: datetime | None = None,
) -> Plan:
    return Plan(
        action=StrategyAction.IDLE,
        intent=None,
        next_cycle_state=state,
        cycle_deadline=deadline,
        reserve_soc_percent=None if issue else Decimal("20"),
        reserve_energy_kwh=None if issue else Decimal("6.4"),
        battery_energy_kwh=Decimal("17.6"),
        reserve_balance_kwh=None if issue else Decimal("11.2"),
        maximum_charge_power_kw=None if issue else Decimal("5.12"),
        maximum_discharge_power_kw=None if issue else Decimal("5.12"),
        issue=issue,
    )


def adapter(controller: Controller, *, reconciled: bool = True) -> MagicMock:
    result = MagicMock()
    result.conflicting_enabled_keys.return_value = ()
    result.next_start_change.return_value = None
    result.intent_matches.return_value = reconciled
    result.next_housekeeping_change.return_value = None
    result.apply = AsyncMock(
        return_value=WriteResult("text.slot", WriteOutcome.APPLIED, "applied")
    )
    result.stop = AsyncMock(
        return_value=WriteResult("switch.slot", WriteOutcome.APPLIED, "stopped")
    )
    result.set_mode = AsyncMock(
        return_value=WriteResult("select.mode", WriteOutcome.APPLIED, "Self-Use")
    )
    result.set_peak_shaving = AsyncMock(
        return_value=WriteResult("switch.peak", WriteOutcome.APPLIED, "on")
    )
    controller.solis = result
    return result


async def test_dirty_worker_coalesces_event_during_write_without_losing_it(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    change = SimpleNamespace(entity_id="text.slot", target="value")
    solis.next_start_change.side_effect = (change, None)

    async def apply(_change, *, deadline):
        del deadline
        controller.trigger()
        return WriteResult("text.slot", WriteOutcome.APPLIED, "applied")

    solis.apply.side_effect = apply
    controller._schedule_wakeup = MagicMock()
    with (
        patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=observation(),
        ),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(side_effect=(plan(), plan())),
        ) as build,
    ):
        await asyncio.wait_for(controller.async_reconcile_now(), timeout=1)

    assert build.await_count == 2
    assert solis.apply.await_count == 1
    assert solis.intent_matches.call_count == 1
    assert controller.data.health is ControllerHealth.HEALTHY


async def test_start_retries_at_exact_generation_offsets_then_suppresses(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    stable_standard = replace(
        plan(),
        action=StrategyAction.CHEAP_CHARGE,
        intent=LogicalIntent((SlotIntent(
            SlotOwner.CHEAP_CHARGING,
            SlotDirection.CHARGE,
            NOW - timedelta(minutes=30),
            NOW + timedelta(hours=4),
            Decimal("100"),
            Decimal("100"),
            NOW + timedelta(hours=4),
        ),)),
        current_cheap_window=SimpleNamespace(components=(SimpleNamespace(
            interval=SimpleNamespace(start=NOW - timedelta(minutes=30), end=NOW + timedelta(hours=4)),
            rate_interval=SimpleNamespace(classification=CheapClassification.STANDARD_CHEAP),
        ),)),
    )
    solis.next_start_change.return_value = SimpleNamespace(
        entity_id="text.slot", target="12:00-16:00"
    )
    solis.apply.return_value = WriteResult(
        "text.slot", WriteOutcome.SERVICE_ERROR, "cloud failed"
    )
    clock = [0.0]
    with (
        patch.object(Controller, "_now", side_effect=lambda: NOW + timedelta(seconds=clock[0])),
        patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]),
        patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=observation(),
        ),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=stable_standard),
        ),
    ):
        generation = None
        for instant, calls in ((0, 1), (10, 1), (15, 2), (59, 2), (60, 3), (120, 3)):
            clock[0] = float(instant)
            await controller._reconcile()
            assert solis.apply.await_count == calls
            assert controller._start_retry is not None
            if generation is None:
                generation = controller._start_retry.generation
            else:
                assert controller._start_retry.generation == generation

    assert START_RETRY_DELAYS == (
        timedelta(0),
        timedelta(seconds=15),
        timedelta(seconds=60),
    )
    assert controller._start_retry is not None
    assert controller._start_retry.suppressed
    same_end = SimpleNamespace(entity_id="text.slot", target="13:00-16:00")
    assert controller._start_generation(
        stable_standard,
        same_end,
        preserve_standard_cheap_slot=True,
    ) == generation
    changed_end = SimpleNamespace(entity_id="text.slot", target="13:00-17:00")
    assert controller._start_generation(
        stable_standard,
        changed_end,
        preserve_standard_cheap_slot=True,
    ) != generation
    bonus_component = SimpleNamespace(
        interval=stable_standard.current_cheap_window.components[0].interval,
        rate_interval=SimpleNamespace(classification=CheapClassification.BONUS_DISPATCH),
    )
    bonus_plan = replace(
        stable_standard,
        current_cheap_window=SimpleNamespace(components=(bonus_component,)),
    )
    assert controller._start_generation(
        bonus_plan,
        same_end,
        preserve_standard_cheap_slot=False,
    ) != generation


async def test_peak_off_failure_uses_bounded_start_retry_without_slot_cleanup(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    forced = replace(
        plan(),
        action=StrategyAction.RESERVE_DISCHARGE,
        intent=LogicalIntent((SlotIntent(
            SlotOwner.RESERVE_EXPORT,
            SlotDirection.DISCHARGE,
            NOW,
            NOW + timedelta(minutes=15),
            Decimal("50"),
            Decimal("20"),
            NOW + timedelta(minutes=15),
        ),)),
        reserve_soc_percent=Decimal("20"),
    )
    change = SimpleNamespace(
        entity_id=controller.config.solis.persistent.grid_peak_shaving_entity_id,
        target=False,
    )
    solis.next_start_change.return_value = change
    solis.apply.return_value = WriteResult(
        change.entity_id, WriteOutcome.SERVICE_ERROR, "Peak Shaving write failed"
    )
    current = observation(
        soc="55", enabled=key, target_state="20", time_state="11:00-13:00", peak_state="off"
    )
    assert current.health is ControllerHealth.HEALTHY, current.issues
    with (
        patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=current,
        ),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=forced),
        ),
    ):
        await controller._reconcile()

    assert solis.apply.await_count == 1
    solis.stop.assert_not_awaited()
    solis.next_housekeeping_change.assert_not_called()
    assert not controller._stop_debts
    assert key not in controller._used_slots
    assert key not in controller._owned_expiry
    assert controller._start_retry is not None
    assert controller._start_retry.attempt == 1
    assert not controller._start_retry.suppressed


async def test_start_generation_ignores_unrelated_refresh_but_resets_for_pending_or_intent_change(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    planned = plan()
    change = SimpleNamespace(entity_id="text.slot", target="10:00-11:00")
    generation = controller._start_generation(planned, change)
    controller._start_retry = StartRetry(
        generation,
        0,
        3,
        60,
        NOW + timedelta(seconds=60),
        True,
    )
    base = observation()
    inverter_time_entity_id = controller.config.solis.persistent.inverter_time_entity_id
    revisions = dict(base.revisions)
    revisions[inverter_time_entity_id] = replace(
        revisions[inverter_time_entity_id],
        last_updated=revisions[inverter_time_entity_id].last_updated
        + timedelta(minutes=1),
        context_id="telemetry-poll",
    )
    refreshed = replace(
        base,
        observed_at=NOW + timedelta(minutes=1),
        revisions=MappingProxyType(revisions),
        telemetry=replace(
            base.telemetry,
            battery_power_kw=Decimal("0.7"),
        ),
    )
    await controller._attempt_start(
        change,
        controller._start_generation(planned, change),
        planned,
        refreshed,
        NOW,
        120,
    )
    solis.apply.assert_not_awaited()

    changed = SimpleNamespace(entity_id="text.slot", target="10:00-11:01")
    assert controller._start_generation(planned, changed) != generation
    segment = SlotIntent(
        SlotOwner.CHEAP_CHARGING,
        SlotDirection.CHARGE,
        NOW,
        NOW + timedelta(minutes=30),
        Decimal("50"),
        Decimal("100"),
        NOW + timedelta(minutes=30),
    )
    changed_plan = replace(
        planned,
        action=StrategyAction.CHEAP_CHARGE,
        intent=LogicalIntent((segment,)),
    )
    assert controller._start_generation(changed_plan, change) != generation


async def test_ambiguous_stop_debt_survives_optimistic_off_and_forces_proof(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    solis.stop.side_effect = (
        WriteResult("switch.slot", WriteOutcome.SERVICE_TIMEOUT, "ambiguous"),
        WriteResult("switch.slot", WriteOutcome.APPLIED, "proved off"),
    )
    clock = [0.0]
    with patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]):
        controller._add_stop(key, NOW, clock[0])
        await controller._attempt_stop(
            controller._stop_debts[key], observation(enabled=key), NOW, clock[0]
        )
        assert controller._stop_debts[key].ambiguous
        clock[0] = 5.0
        optimistic_off = observation(enabled=key, enabled_state="off")
        controller._retire_proven_stops(optimistic_off)
        assert key in controller._stop_debts
        await controller._attempt_stop(
            controller._stop_debts[key], optimistic_off, NOW + timedelta(seconds=5), clock[0]
        )

    assert key not in controller._stop_debts
    assert solis.stop.await_args_list[1].kwargs["force"] is True


async def test_peak_handover_is_one_attempt_then_important_stop(hass: HomeAssistant) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    off = replace(observation(enabled=key), grid_peak_shaving=False)
    controller._add_stop(key, NOW, 0)
    debt = controller._stop_debts[key]

    await controller._attempt_stop(debt, off, NOW, 0)
    assert solis.set_peak_shaving.await_count == 1
    solis.stop.assert_not_awaited()
    assert controller._stop_debts[key].peak_shaving_handover_attempted

    await controller._attempt_stop(controller._stop_debts[key], off, NOW, 0)
    solis.stop.assert_awaited_once()


@pytest.mark.parametrize("peak_state", (None, False))
async def test_unavailable_peak_handover_skips_write_but_stops_on_next_due_pass(
    hass: HomeAssistant, peak_state: bool | None,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    unknown_observation = observation(enabled=key)
    revisions = dict(unknown_observation.revisions)
    revisions.pop(controller.config.solis.persistent.grid_peak_shaving_entity_id)
    unknown = replace(
        unknown_observation,
        grid_peak_shaving=peak_state,
        revisions=MappingProxyType(revisions),
    )
    controller._add_stop(key, NOW, 0)

    await controller._attempt_stop(controller._stop_debts[key], unknown, NOW, 0)
    solis.set_peak_shaving.assert_not_awaited()
    solis.stop.assert_not_awaited()
    await controller._attempt_stop(controller._stop_debts[key], unknown, NOW, 0)
    solis.stop.assert_awaited_once()


def test_minimum_soc_bypass_upgrades_existing_stop_debt(hass: HomeAssistant) -> None:
    controller = Controller(hass, config())
    key = SlotKey(2, SlotDirection.DISCHARGE)
    controller._add_stop(key, NOW, 0)
    assert not controller._stop_debts[key].peak_shaving_handover_attempted
    controller._add_stop(key, NOW, 0, bypass_peak_handover=True)
    assert controller._stop_debts[key].peak_shaving_handover_attempted


def test_recurring_native_schedule_does_not_infer_restart_expiry(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    key = SlotKey(1, SlotDirection.CHARGE)
    base = observation(
        soc="40", enabled=key, target_state="100", time_state="10:00-11:00"
    )
    invalid_policy = replace(base, persistent=None)

    controller._discover_unconditional_stops(invalid_policy, NOW, 0)

    assert key not in controller._stop_debts


async def test_stop_publishes_degraded_context_before_blocking_service(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    controller._add_stop(key, NOW, 0)
    debt = controller._stop_debts[key]

    async def stop(*_args, **_kwargs):
        snapshot = controller.data
        assert snapshot is not None
        assert snapshot.health is ControllerHealth.DEGRADED
        assert snapshot.heartbeat_at == NOW
        assert snapshot.pending_operation == "stop slot 2 discharge"
        assert snapshot.attempt == 0
        assert snapshot.next_retry_at == NOW
        return WriteResult("switch.slot", WriteOutcome.APPLIED, "proved off")

    solis.stop.side_effect = stop
    await controller._attempt_stop(debt, observation(enabled=key), NOW, 0)

    assert key in controller._stop_debts
    solis.stop.assert_awaited_once()


async def test_stop_retries_remain_unbounded_and_capped_and_cancellation_is_ambiguous(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    solis.stop.return_value = WriteResult(
        "switch.slot", WriteOutcome.SERVICE_ERROR, "failed"
    )
    clock = [0.0]
    with patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]):
        controller._add_stop(key, NOW, 0)
        for attempt in range(7):
            debt = controller._stop_debts[key]
            clock[0] = debt.next_attempt
            await controller._attempt_stop(debt, observation(enabled=key), NOW, clock[0])
            assert controller._stop_debts[key].attempt == attempt + 1
            assert _stop_retry_delay(attempt) <= timedelta(seconds=60)
        assert key in controller._stop_debts

        solis.stop.side_effect = asyncio.CancelledError
        clock[0] = controller._stop_debts[key].next_attempt
        with pytest.raises(asyncio.CancelledError):
            await controller._attempt_stop(
                controller._stop_debts[key], observation(enabled=key), NOW, clock[0]
            )
        assert controller._stop_debts[key].ambiguous
        solis.stop.side_effect = None
        solis.stop.return_value = WriteResult(
            "switch.slot", WriteOutcome.APPLIED, "forced proof"
        )
        await controller._attempt_stop(
            controller._stop_debts[key],
            observation(enabled=key, enabled_state="off"),
            NOW,
            clock[0],
        )
    assert key not in controller._stop_debts
    assert solis.stop.await_args.kwargs["force"] is True


async def test_later_on_drift_recreates_previously_proved_stop_debt(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    solis.conflicting_enabled_keys.return_value = (key,)
    current = observation(enabled=key)
    with (
        patch(
            "custom_components.house_battery_control.controller.read_state",
            side_effect=(
                current,
                observation(enabled=key, enabled_state="off"),
                current,
            ),
        ),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=plan()),
        ),
    ):
        await controller._reconcile()
        assert key in controller._stop_debts
        await controller._reconcile()
        assert key not in controller._stop_debts
        await controller._reconcile()
    assert solis.stop.await_count == 2


async def test_minimum_soc_discharge_stops_before_unavailable_planning(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    build = AsyncMock(return_value=plan(issue="tariff unavailable"))
    with (
        patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=observation(
                soc="10",
                enabled=key,
                target_state="unavailable",
            ),
        ),
        patch("custom_components.house_battery_control.controller.build_plan", build),
    ):
        await controller._reconcile()

    solis.stop.assert_awaited_once()
    build.assert_not_awaited()


@pytest.mark.parametrize("reason", ("target", "expiry"))
async def test_target_complete_or_owned_expiry_creates_stop_before_planning(
    hass: HomeAssistant,
    reason: str,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    current = observation(soc="100" if reason == "target" else "55", enabled=key)
    if reason == "expiry":
        controller._owned_expiry[key] = NOW - timedelta(seconds=1)
    build = AsyncMock(return_value=plan())
    with (
        patch("custom_components.house_battery_control.controller.read_state", return_value=current),
        patch("custom_components.house_battery_control.controller.build_plan", build),
    ):
        await controller._reconcile()
    solis.stop.assert_awaited_once()
    build.assert_not_awaited()


async def test_bonus_lease_stop_waits_for_authoritative_off_before_releasing_lease(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    controller._bonus_charge_keys.add(key)
    controller._charge_lease_deadline = NOW + timedelta(minutes=15)
    controller._add_stop(key, NOW, 0)

    await controller._attempt_stop(
        controller._stop_debts[key], observation(enabled=key), NOW, 0
    )
    assert key in controller._awaiting_off_proof
    assert key in controller._bonus_charge_keys
    assert key in controller._stop_debts
    await controller._attempt_stop(
        controller._stop_debts[key], observation(enabled=key), NOW, 0
    )
    assert solis.stop.await_count == 1
    debt = controller._stop_debts[key]
    await controller._attempt_stop(debt, observation(enabled=key), NOW, debt.next_attempt)
    assert solis.stop.await_count == 2

    controller._retire_proven_stops(observation(enabled=key, enabled_state="off"))
    assert key not in controller._awaiting_off_proof
    assert key not in controller._bonus_charge_keys
    assert controller._charge_lease_deadline is None


async def test_expired_bonus_lease_stops_then_renews_only_after_off_proof(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    active = observation(enabled=key)
    off = observation(enabled=key, enabled_state="off")
    component = SimpleNamespace(
        interval=SimpleNamespace(start=NOW, end=NOW + timedelta(minutes=30)),
        rate_interval=SimpleNamespace(
            classification=CheapClassification.BONUS_DISPATCH,
            import_price=Decimal("0.07"), source="import", source_event="event",
            dispatch_source_entity_id="binary_sensor.dispatch", source_revision_at=NOW,
        ),
        export_interval=SimpleNamespace(
            start=NOW, end=NOW + timedelta(minutes=30), export_price=Decimal("0.15")
        ),
    )
    segment = SlotIntent(
        SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE, NOW,
        NOW + timedelta(minutes=15), Decimal("50"), Decimal("100"),
        NOW + timedelta(minutes=15),
    )
    bonus_plan = replace(
        plan(), action=StrategyAction.CHEAP_CHARGE,
        intent=LogicalIntent((segment,)),
        current_cheap_window=SimpleNamespace(components=(component,)),
        charge_lease_deadline=NOW + timedelta(minutes=15),
    )
    assert _bonus_fingerprint(bonus_plan, NOW) is not None
    controller._bonus_charge_keys.add(key)
    controller._charge_lease_deadline = NOW + timedelta(minutes=15)
    controller._owned_expiry[key] = NOW - timedelta(seconds=1)
    change = SimpleNamespace(
        entity_id=controller.config.solis.direction(key).enable_entity_id,
        target=True,
    )
    solis.next_start_change.side_effect = (change, None)
    with (
        patch(
            "custom_components.house_battery_control.controller.read_state",
            side_effect=(active, off, off),
        ),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=bonus_plan),
        ),
        patch.object(Controller, "_now", return_value=NOW),
    ):
        await controller._reconcile()
        assert solis.stop.await_count == 1
        assert solis.apply.await_count == 0
        await controller._reconcile()
        assert solis.apply.await_count == 1
        assert key in controller._bonus_charge_keys
        await controller._reconcile()

    assert controller._charge_lease_deadline == NOW + timedelta(minutes=15)


async def test_fresh_controller_reconstructs_ephemeral_bonus_lease_and_expires_it(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    component = SimpleNamespace(
        interval=SimpleNamespace(start=NOW, end=NOW + timedelta(minutes=30)),
        rate_interval=SimpleNamespace(
            classification=CheapClassification.BONUS_DISPATCH,
            import_price=Decimal("0.07"), source="import", source_event="event",
            dispatch_source_entity_id="binary_sensor.dispatch", source_revision_at=NOW,
        ),
        export_interval=SimpleNamespace(
            start=NOW, end=NOW + timedelta(minutes=30), export_price=Decimal("0.15")
        ),
    )
    segment = SlotIntent(
        SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE, NOW,
        NOW + timedelta(minutes=15), Decimal("50"), Decimal("100"),
        NOW + timedelta(minutes=15),
    )
    bonus_plan = replace(
        plan(), action=StrategyAction.CHEAP_CHARGE,
        intent=LogicalIntent((segment,)),
        current_cheap_window=SimpleNamespace(components=(component,)),
        charge_lease_deadline=NOW + timedelta(minutes=15),
    )
    current = observation(
        enabled=key, target_state="100", time_state="12:00-12:15"
    )
    assert _bonus_fingerprint(bonus_plan, NOW) is not None
    hass.states.async_set(
        controller.config.tariff.import_rates_entity_id,
        "available",
        {"dispatch_source_entity_id": "binary_sensor.dispatch"},
    )
    hass.states.async_set("binary_sensor.dispatch", "on")
    with (
        patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=current,
        ),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=bonus_plan),
        ),
        patch.object(Controller, "_now", return_value=NOW),
        ):
            await controller._reconcile()
    solis.intent_matches.assert_called_once()
    assert key in controller._bonus_charge_keys
    assert controller._owned_expiry[key] == NOW + timedelta(minutes=15)
    assert controller._charge_lease_deadline == NOW + timedelta(minutes=15)

    controller._owned_expiry[key] = NOW - timedelta(seconds=1)
    with patch(
        "custom_components.house_battery_control.controller.read_state",
        return_value=current,
    ):
        await controller._reconcile()
    assert key in controller._stop_debts
    solis.stop.assert_awaited_once()


async def test_explicit_dispatch_off_is_withdrawal_but_zero_ev_power_is_not(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    controller._bonus_charge_keys.add(key)
    hass.states.async_set(
        controller.config.tariff.import_rates_entity_id,
        "available",
        {"dispatch_source_entity_id": "binary_sensor.dispatch"},
    )
    hass.states.async_set("binary_sensor.dispatch", "off")
    assert controller._bonus_dispatch_is_off()
    with patch(
        "custom_components.house_battery_control.controller.read_state",
        return_value=observation(enabled=key),
    ):
        await controller._reconcile()
    solis.stop.assert_awaited_once()
    assert key in controller._stop_debts


async def test_plan_issue_preserves_cycle_and_makes_no_start_call(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    deadline = NOW + timedelta(minutes=8)
    controller._cycle_state = CycleState.CYCLE_DISCHARGING
    controller._cycle_deadline = deadline
    with (
        patch("custom_components.house_battery_control.controller.read_state", return_value=observation()),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=plan(issue="tariff unavailable", state=CycleState.CYCLE_DISCHARGING, deadline=deadline)),
        ),
    ):
        await controller._reconcile()

    assert controller.data.health is ControllerHealth.DEGRADED
    assert controller._cycle_state is CycleState.CYCLE_DISCHARGING
    assert controller._cycle_deadline == deadline
    solis.next_start_change.assert_not_called()


async def test_conflicting_direction_is_stopped_before_start(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(1, SlotDirection.CHARGE)
    solis.conflicting_enabled_keys.return_value = (key,)
    with (
        patch("custom_components.house_battery_control.controller.read_state", return_value=observation(enabled=key)),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=plan()),
        ),
    ):
        await controller._reconcile()

    solis.stop.assert_awaited_once()
    solis.next_start_change.assert_not_called()


async def test_prolonged_planning_degradation_recovers_without_fail_safe(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    clock = [0.0]
    issue = plan(issue="telemetry unavailable")
    with (
        patch.object(Controller, "_now", side_effect=lambda: NOW + timedelta(seconds=clock[0])),
        patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]),
        patch("custom_components.house_battery_control.controller.read_state", return_value=observation()),
        patch(
            "custom_components.house_battery_control.controller.build_plan",
            AsyncMock(return_value=issue),
        ) as build,
    ):
        await controller._reconcile()
        clock[0] = IMPORTANT_STOP_FAILSAFE_TIMEOUT.total_seconds() + 1
        await controller._reconcile()
        assert not controller._fail_safe_latched
        assert controller.data.health is ControllerHealth.DEGRADED
        build.side_effect = None
        build.return_value = plan()
        with patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=observation(),
        ):
            await controller._reconcile()

    assert not controller._fail_safe_latched
    assert controller.data.health is ControllerHealth.HEALTHY
    solis.set_mode.assert_not_awaited()
    assert build.await_count == 3
    assert not Controller(hass, config())._fail_safe_latched


async def test_stop_deadline_is_not_renewed_by_retries_or_events(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    solis.stop.return_value = WriteResult("switch.slot", WriteOutcome.SERVICE_ERROR, "failed")
    clock = [0.0]
    with patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]):
        controller._add_stop(key, NOW, clock[0])
        debt = controller._stop_debts[key]
        assert debt.first_seen == 0
        assert debt.fail_safe_deadline == IMPORTANT_STOP_FAILSAFE_TIMEOUT.total_seconds()
        for _ in range(3):
            controller.trigger()
            clock[0] = controller._stop_debts[key].next_attempt
            await controller._attempt_stop(controller._stop_debts[key], observation(enabled=key), NOW, clock[0])
        assert controller._stop_debts[key].first_seen == debt.first_seen
        assert controller._stop_debts[key].fail_safe_deadline == debt.fail_safe_deadline


async def test_stop_proved_before_deadline_clears_without_latching(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    key = SlotKey(2, SlotDirection.DISCHARGE)
    clock = [0.0]
    with patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]):
        controller._add_stop(key, NOW, clock[0])
        clock[0] = IMPORTANT_STOP_FAILSAFE_TIMEOUT.total_seconds() - 1
        controller._retire_proven_stops(observation(enabled=key, enabled_state="off"))
    assert key not in controller._stop_debts
    assert not controller._fail_safe_latched


async def test_unproved_stop_deadline_latches_and_keeps_retrying(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    clock = [0.0]
    solis.stop.side_effect = (
        WriteResult("switch.slot", WriteOutcome.SERVICE_ERROR, "failed"),
        WriteResult("switch.slot", WriteOutcome.APPLIED, "provisional"),
    )
    with (
        patch.object(Controller, "_now", side_effect=lambda: NOW + timedelta(seconds=clock[0])),
        patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]),
        patch("custom_components.house_battery_control.controller.read_state", return_value=observation(enabled=key)),
    ):
        controller._add_stop(key, NOW, clock[0])
        await controller._reconcile()
        clock[0] = IMPORTANT_STOP_FAILSAFE_TIMEOUT.total_seconds()
        await controller._reconcile()
        assert controller._fail_safe_latched
        assert controller.data.health is ControllerHealth.FAIL_SAFE
        assert key in controller._stop_debts
        clock[0] += 1
        await controller._reconcile()
    assert solis.stop.await_count == 2
    assert solis.set_mode.await_count == 1


def test_distinct_stop_debt_receives_a_new_deadline(hass: HomeAssistant) -> None:
    controller = Controller(hass, config())
    first = SlotKey(1, SlotDirection.CHARGE)
    second = SlotKey(2, SlotDirection.DISCHARGE)
    controller._add_stop(first, NOW, 0)
    controller._retire_proven_stops(observation(enabled=first, enabled_state="off"))
    controller._add_stop(second, NOW + timedelta(seconds=100), 100)
    assert controller._stop_debts[second].first_seen == 100
    assert controller._stop_debts[second].fail_safe_deadline == 100 + IMPORTANT_STOP_FAILSAFE_TIMEOUT.total_seconds()


async def test_fail_safe_retries_only_mode_then_continues_known_stop(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    controller._fail_safe_latched = True
    controller._fail_safe_since = NOW
    key = SlotKey(2, SlotDirection.DISCHARGE)
    controller._add_stop(key, NOW, 0)
    clock = [0.0]
    solis.set_mode.side_effect = (
        WriteResult("select.mode", WriteOutcome.SERVICE_ERROR, "failed"),
        WriteResult("select.mode", WriteOutcome.APPLIED, "Self-Use"),
    )
    with (
        patch.object(Controller, "_now", side_effect=lambda: NOW + timedelta(seconds=clock[0])),
        patch.object(Controller, "_monotonic", side_effect=lambda: clock[0]),
        patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=observation(enabled=key),
        ),
    ):
        await controller._reconcile()
        assert solis.stop.await_count == 1
        assert solis.set_mode.await_count == 0
        clock[0] = 1
        await controller._reconcile()
        clock[0] = 5
        await controller._reconcile()
        clock[0] = 6
        await controller._reconcile()
        with patch(
            "custom_components.house_battery_control.controller.read_state",
            return_value=observation(mode="Self-Use", enabled=key),
        ):
            await controller._reconcile()

    assert solis.set_mode.await_count == 2
    assert solis.stop.await_count == 2
    solis.apply.assert_not_awaited()
    assert controller.data.health is ControllerHealth.FAIL_SAFE


def test_source_subscription_covers_every_solis_and_planning_entity(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    ids = set(controller.source_entity_ids())
    solis = controller.config.solis
    assert {
        controller.config.tariff.import_rates_entity_id,
        controller.config.tariff.export_rates_entity_id,
        controller.config.cycle_discharge_duration_entity_id,
        solis.telemetry.state_of_charge_entity_id,
        solis.persistent.storage_mode_entity_id,
    }.issubset(ids)
    for slot in solis.slots:
        for direction in (slot.charge, slot.discharge):
            assert {
                direction.enable_entity_id,
                direction.time_entity_id,
                direction.current_entity_id,
                direction.target_soc_entity_id,
            }.issubset(ids)


def test_dispatch_change_wakes_through_fused_import_entity_without_extra_poller(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    with patch.object(controller, "trigger") as trigger:
        controller._source_changed(SimpleNamespace())
    trigger.assert_called_once()
    assert controller.config.tariff.import_rates_entity_id in controller.source_entity_ids()


async def test_exact_boundary_precedes_one_minute_backstop(hass: HomeAssistant) -> None:
    controller = Controller(hass, config())
    boundary = NOW + timedelta(seconds=20)
    controller._last_plan = replace(
        plan(),
        next_cheap_window=SimpleNamespace(
            start=boundary,
            end=boundary + timedelta(minutes=30),
        ),
    )
    remove = MagicMock()
    with (
        patch.object(Controller, "_now", return_value=NOW),
        patch(
            "custom_components.house_battery_control.controller.async_track_point_in_utc_time",
            return_value=remove,
        ) as track,
    ):
        controller._schedule_wakeup()
    assert track.call_args.args[2] == boundary
    assert BACKSTOP_INTERVAL == timedelta(minutes=1)


async def test_retry_deadline_precedes_boundary_and_backstop(hass: HomeAssistant) -> None:
    controller = Controller(hass, config())
    key = SlotKey(2, SlotDirection.DISCHARGE)
    controller._stop_debts[key] = SimpleNamespace(
        next_attempt=5.0,
        next_retry_at=NOW + timedelta(seconds=5),
        fail_safe_deadline=900.0,
    )
    remove = MagicMock()
    with (
        patch.object(Controller, "_now", return_value=NOW),
        patch.object(Controller, "_monotonic", return_value=0.0),
        patch(
            "custom_components.house_battery_control.controller.async_track_point_in_utc_time",
            return_value=remove,
        ) as track,
    ):
        controller._schedule_wakeup()
    assert track.call_args.args[2] == NOW + timedelta(seconds=5)


async def test_shutdown_confirms_self_use_before_stopping_only_observed_on(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    feed = observation(mode="Feed-In Priority")
    self_use_on = observation(mode="Self-Use", enabled=key)
    self_use_off = observation(mode="Self-Use", enabled=key, enabled_state="off")
    order: list[str] = []

    async def set_mode(*args, **kwargs):
        del args, kwargs
        order.append("mode")
        return WriteResult("select.mode", WriteOutcome.APPLIED, "Self-Use")

    async def stop(*args, **kwargs):
        del args, kwargs
        order.append("stop")
        return WriteResult("switch.slot", WriteOutcome.APPLIED, "off")

    solis.set_mode.side_effect = set_mode
    solis.stop.side_effect = stop
    with patch(
        "custom_components.house_battery_control.controller.read_state",
        side_effect=(feed, self_use_on, self_use_on, self_use_off),
    ):
        await controller._shutdown_controls()

    assert order == ["mode", "stop"]
    assert solis.stop.await_args.args[0] == key


async def test_shutdown_rereads_unknown_without_speculative_write(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    key = SlotKey(2, SlotDirection.DISCHARGE)
    unknown = observation(mode="Self-Use", enabled=key, enabled_state="unavailable")
    all_off = observation(mode="Self-Use")
    with (
        patch(
            "custom_components.house_battery_control.controller.read_state",
            side_effect=(unknown, unknown, all_off),
        ),
        patch("custom_components.house_battery_control.controller.asyncio.sleep", AsyncMock()),
    ):
        await controller._shutdown_controls()

    solis.stop.assert_not_awaited()


async def test_prolonged_shutdown_refreshes_heartbeat_on_every_retry_and_reread(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    solis = adapter(controller)
    solis.set_mode.return_value = WriteResult(
        "select.mode", WriteOutcome.SERVICE_ERROR, "cloud unavailable"
    )
    feed = observation(mode="Feed-In Priority")
    self_use = observation(mode="Self-Use")
    reads = 0

    def read(*args, **kwargs):
        nonlocal reads
        del args, kwargs
        reads += 1
        return feed if reads <= 4 else self_use

    tick = 0

    def now() -> datetime:
        nonlocal tick
        value = NOW + timedelta(seconds=31 * tick)
        tick += 1
        return value

    heartbeats: list[datetime] = []
    controller.async_add_listener(
        lambda: heartbeats.append(controller.data.heartbeat_at)
    )
    with (
        patch.object(Controller, "_now", side_effect=now),
        patch("custom_components.house_battery_control.controller.read_state", side_effect=read),
        patch("custom_components.house_battery_control.controller.asyncio.sleep", AsyncMock()),
    ):
        await controller._shutdown_controls()

    assert solis.set_mode.await_count == 4
    assert heartbeats[-1] - heartbeats[0] > timedelta(minutes=3)
    assert max(
        right - left for left, right in zip(heartbeats, heartbeats[1:])
    ) < timedelta(minutes=3)


async def test_teardown_is_idempotent_and_removes_listener_timer_and_worker(
    hass: HomeAssistant,
) -> None:
    controller = Controller(hass, config())
    source_remove = MagicMock()
    timer_remove = MagicMock()
    controller._unsub_sources = source_remove
    controller._unsub_wakeup = timer_remove
    with patch.object(controller, "_shutdown_controls", AsyncMock()) as shutdown:
        await asyncio.gather(controller.async_stop(), controller.async_stop())
    shutdown.assert_awaited_once()
    source_remove.assert_called_once()
    timer_remove.assert_called_once()
    assert controller._stop_task is not None and controller._stop_task.done()
