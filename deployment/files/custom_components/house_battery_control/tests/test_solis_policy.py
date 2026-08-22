"""Offline tests for persistent Solis policy contracts."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from custom_components.house_battery_control.solis_policy import (
    CapabilityResolutionRecord,
    EphemeralAuthorizationStore,
    ManualGridImportVerification,
    PersistentCandidateAuthorization,
    PolicyActuationStatus,
    SolisPolicyActuator,
    bounded_reserve_soc,
    build_candidate_policy,
    build_fail_safe_policy,
    canonical_policy,
    policy_fingerprint,
)
from custom_components.house_battery_control.domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from custom_components.house_battery_control.contracts import StorageMode
from custom_components.house_battery_control.contracts import PreserveCurrentPolicyValue
from custom_components.house_battery_control import config
from custom_components.house_battery_control.ha_writer import HomeAssistantWriter
from custom_components.house_battery_control.solis_actuator import mapping_fingerprint
from custom_components.house_battery_control.solis_reader import read_solis_state


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
MAPPING = "1" * 64
POLICY = policy_fingerprint()


@pytest.mark.parametrize(
    ("value", "expected"),
    (("10", Decimal("10")), ("10.01", Decimal("11")), ("99.1", Decimal("100")), ("2", Decimal("10"))),
)
def test_reserve_rounds_up_and_uses_only_named_physical_bounds(value, expected):
    assert bounded_reserve_soc(value) == expected


def test_candidate_and_fail_safe_builders_use_named_settings():
    candidate = build_candidate_policy("10.01")
    safe = build_fail_safe_policy()
    assert candidate.storage_mode is StorageMode.FEED_IN_PRIORITY
    assert candidate.battery_reserve_enabled is True
    assert candidate.battery_reserve_soc == Decimal("11")
    assert safe.storage_mode is StorageMode.SELF_USE
    assert safe.battery_reserve_enabled is False
    assert safe.peak_shaving_enabled is True
    assert isinstance(safe.over_discharge_soc, PreserveCurrentPolicyValue)
    assert isinstance(safe.grid_charge_allowed, PreserveCurrentPolicyValue)


def test_policy_serialization_is_stable_and_explicitly_decimal():
    first = canonical_policy()
    assert first == canonical_policy()
    assert b"MINIMUM_SOC_PERCENT" in first
    assert b'"expected_value_kw":"0.1"' in first
    assert policy_fingerprint() == policy_fingerprint()


def test_ephemeral_authorization_is_single_use_and_fingerprint_bound():
    store = EphemeralAuthorizationStore()
    token = store.issue(now=NOW, mapping=MAPPING, policy=POLICY)
    assert store.consume(token, now=NOW + timedelta(seconds=1), mapping=MAPPING, policy=POLICY)
    assert not store.consume(token, now=NOW + timedelta(seconds=2), mapping=MAPPING, policy=POLICY)

    other = store.issue(now=NOW, mapping=MAPPING, policy=POLICY)
    assert not store.consume(other, now=NOW, mapping="2" * 64, policy=POLICY)
    assert not store.consume(other, now=NOW + timedelta(minutes=11), mapping=MAPPING, policy=POLICY)


def test_ephemeral_expiry_is_bounded_and_future_attempt_is_rejected_and_consumed():
    store = EphemeralAuthorizationStore()
    token = store.issue(now=NOW, mapping=MAPPING, policy=POLICY, ttl=timedelta(minutes=1))
    assert not store.consume(token, now=NOW - timedelta(seconds=1), mapping=MAPPING, policy=POLICY)
    assert not store.consume(token, now=NOW, mapping=MAPPING, policy=POLICY)
    with pytest.raises(ValueError):
        store.issue(now=NOW, mapping=MAPPING, policy=POLICY, ttl=timedelta(minutes=11))


def test_manual_grid_verification_requires_exact_decimal_and_matching_fingerprints():
    record = ManualGridImportVerification(MAXIMUM_GRID_IMPORT_POWER_KW, NOW, MAPPING, POLICY, True)
    assert record.valid(now=NOW, mapping=MAPPING, policy=POLICY)
    assert not record.valid(now=NOW, mapping="2" * 64, policy=POLICY)
    wrong = ManualGridImportVerification(Decimal("0.10"), NOW, MAPPING, POLICY, True)
    assert wrong.valid(now=NOW, mapping=MAPPING, policy=POLICY)


def test_capability_resolution_is_fingerprint_and_unit_bound():
    record = CapabilityResolutionRecord("number.example", "W", Decimal("5000"), NOW, MAPPING, POLICY, "manual_commissioning")
    assert record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="W")
    assert not record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="A")
    assert not record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="W", entity_id="number.other")


def test_documented_unlimited_requires_exact_writable_target():
    from custom_components.house_battery_control.contracts import DocumentedUnlimitedValue

    with pytest.raises(ValueError):
        CapabilityResolutionRecord("number.example", "W", DocumentedUnlimitedValue(), NOW, MAPPING, POLICY, "manual_commissioning")
    record = CapabilityResolutionRecord("number.example", "W", DocumentedUnlimitedValue(), NOW, MAPPING, POLICY, "manual_commissioning", Decimal("5000"))
    assert record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="W", entity_id="number.example")


class FakeHA:
    def __init__(self, states):
        self.states = states
        self.calls = []
        self.listeners = {}
        self.on_call = None

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def async_listen_state_change(self, entity_id, callback):
        self.listeners.setdefault(entity_id, []).append(callback)

        def remove():
            self.listeners[entity_id].remove(callback)

        return remove

    async def async_call(self, domain, service, data, *, blocking=True):
        self.calls.append((domain, service, data))
        if self.on_call:
            result = self.on_call(domain, service, data)
            if asyncio.iscoroutine(result):
                await result
        entity_id = data["entity_id"]
        state = self.states[entity_id]
        if domain == "switch":
            value = "on" if service == "turn_on" else "off"
        elif domain == "select":
            value = data["option"]
        else:
            value = str(data["value"])
        state.update(state=value, last_updated=state["last_updated"] + timedelta(microseconds=1), context_id="service")
        for callback in tuple(self.listeners.get(entity_id, ())):
            callback(entity_id, None, state)


class StepClock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        self.value += timedelta(milliseconds=1)
        return self.value


def _state(value, *, attributes=None):
    return {"state": value, "attributes": attributes or {}, "last_updated": NOW, "context_id": "initial"}


def _capability(value, unit, maximum="100"):
    return _state(value, attributes={"min": "0", "max": maximum, "step": "1", "unit_of_measurement": unit})


def policy_fixture(*, persistent=False):
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    source["solis"]["telemetry"].update(battery_power_entity_id="sensor.battery_power", battery_power_sign="positive_means_charging", device_timestamp_entity_id="sensor.device_time")
    solis = config.from_mapping(source).solis
    states = {
        solis.telemetry.state_of_charge_entity_id: _state("55", attributes={"unit_of_measurement": "%"}),
        solis.telemetry.battery_power_entity_id: _state("0", attributes={"unit_of_measurement": "kW"}),
        solis.telemetry.device_timestamp_entity_id: _state(NOW.isoformat()),
        solis.persistent.storage_mode_entity_id: _state("Self-Use", attributes={"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]}),
        solis.persistent.inverter_time_entity_id: _state(NOW.isoformat()),
    }
    for entity_id in (solis.persistent.allow_grid_charging_entity_id, solis.persistent.allow_export_entity_id, solis.persistent.grid_peak_shaving_entity_id, solis.persistent.inverter_on_off_entity_id):
        states[entity_id] = _state("on")
    for entity_id in (solis.protection.battery_over_discharge_soc_entity_id, solis.protection.battery_force_charge_soc_entity_id, solis.protection.battery_recovery_soc_entity_id, solis.protection.battery_max_charge_soc_entity_id, solis.protection.battery_reserve_soc_entity_id):
        states[entity_id] = _capability("10", "%")
    states[solis.protection.battery_reserve_entity_id] = _state("off")
    states[solis.capability.battery_max_charge_current_entity_id] = _capability("100", "A", "200")
    states[solis.capability.battery_max_discharge_current_entity_id] = _capability("100", "A", "200")
    states[solis.capability.max_output_power_entity_id] = _capability("5000", "W", "6000")
    states[solis.capability.max_export_power_entity_id] = _capability("5000", "W", "6000")
    for slot in solis.slots:
        for direction in (slot.charge, slot.discharge):
            states[direction.enable_entity_id] = _state("off")
            states[direction.time_entity_id] = _state("00:00-00:00")
            states[direction.current_entity_id] = _capability("1", "A", "10")
            states[direction.target_soc_entity_id] = _capability("50", "%")
    guard = "input_boolean.house_battery_control_disable"
    states[guard] = _state("off")
    ha = FakeHA(states)
    clock = StepClock()

    def refresh(boundary):
        states[solis.telemetry.device_timestamp_entity_id]["state"] = boundary.isoformat()
        states[solis.persistent.inverter_time_entity_id]["state"] = boundary.isoformat()
        return read_solis_state(solis, states, boundary)

    initial = refresh(NOW)
    mapping = mapping_fingerprint(solis)
    policy = policy_fingerprint()
    persistent_record = PersistentCandidateAuthorization(NOW, mapping, policy, True, True) if persistent else None
    actuator = SolisPolicyActuator(solis, HomeAssistantWriter(ha), control_disable_guard_entity_id=guard, inverter_timezone=timezone.utc, observation_refresh=refresh, persistent_authorization=persistent_record, clock=clock)
    token = actuator.ephemeral_authorizations.issue(now=NOW, mapping=mapping, policy=policy)
    manual = ManualGridImportVerification(MAXIMUM_GRID_IMPORT_POWER_KW, NOW, mapping, policy, True)
    return actuator, ha, initial, token, manual, clock, persistent_record


@pytest.mark.asyncio
async def test_fail_safe_absolute_deadline_bounds_every_persistent_primitive():
    actuator, ha, _initial, _token, _manual, clock, _record = policy_fixture()
    persistent = actuator.config.persistent
    ha.states[persistent.storage_mode_entity_id]["state"] = "Feed-In Priority"
    ha.states[persistent.grid_peak_shaving_entity_id]["state"] = "off"
    ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] = "on"

    result = await actuator.async_apply_fail_safe(
        deadline=NOW + timedelta(milliseconds=18)
    )

    assert result.status == PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE
    assert len(ha.calls) == 1
    assert ha.calls[0][2]["entity_id"] == persistent.storage_mode_entity_id
    assert any("deadline exhausted" in item.message for item in result.results)
    assert clock.value >= NOW + timedelta(milliseconds=18)


@pytest.mark.asyncio
async def test_expired_fail_safe_deadline_starts_no_primitive():
    actuator, ha, _initial, _token, _manual, _clock, _record = policy_fixture()
    result = await actuator.async_apply_fail_safe(deadline=NOW)
    assert result.status == PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE
    assert ha.calls == []


@pytest.mark.asyncio
async def test_real_candidate_actuator_applies_ordered_policy_and_preserves_globals():
    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    original_globals = {entity_id: ha.states[entity_id]["state"] for entity_id in (
        actuator.config.capability.battery_max_charge_current_entity_id,
        actuator.config.capability.battery_max_discharge_current_entity_id,
        actuator.config.capability.max_output_power_entity_id,
        actuator.config.capability.max_export_power_entity_id,
    )}
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=Decimal("20"), authorization=token, manual_grid_import_verification=manual)
    assert result.status == PolicyActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION
    assert ha.calls[-1][2]["entity_id"] == actuator.config.persistent.storage_mode_entity_id
    assert ha.states[actuator.config.protection.battery_force_charge_soc_entity_id]["state"] == "7"
    assert ha.states[actuator.config.protection.battery_max_charge_soc_entity_id]["state"] == "100"
    assert {entity_id: ha.states[entity_id]["state"] for entity_id in original_globals} == original_globals


@pytest.mark.asyncio
async def test_forged_ephemeral_nonce_is_consumed_and_original_cannot_be_reused():
    actuator, _ha, initial, token, manual, _clock, _record = policy_fixture()
    forged = replace(token, expires_at=token.expires_at - timedelta(seconds=1))
    first = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=forged, manual_grid_import_verification=manual)
    second = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual)
    assert first.status == second.status == PolicyActuationStatus.BLOCKED


@pytest.mark.asyncio
async def test_persistent_authorization_must_be_configured_exact_instance():
    actuator, _ha, initial, _token, manual, _clock, record = policy_fixture(persistent=True)
    rejected = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=replace(record), manual_grid_import_verification=manual)
    accepted = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=record, manual_grid_import_verification=manual)
    assert rejected.status == PolicyActuationStatus.BLOCKED
    assert accepted.status == PolicyActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION


@pytest.mark.asyncio
async def test_direct_slot_entry_cannot_interleave_with_policy_lock():
    actuator, _ha, _initial, _token, _manual, _clock, _record = policy_fixture()
    await actuator.orchestration_lock.acquire()
    task = asyncio.create_task(actuator.slot_actuator.async_disable_all())
    await asyncio.sleep(0)
    assert not task.done()
    actuator.orchestration_lock.release()
    assert (await task).safe


@pytest.mark.asyncio
async def test_guard_race_invokes_proven_fail_safe():
    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    force_id = actuator.config.protection.battery_force_charge_soc_entity_id

    def assert_guard(_domain, _service, data):
        if data["entity_id"] == force_id:
            ha.states[actuator.control_disable_guard_entity_id].update(state="on", context_id="external")

    ha.on_call = assert_guard
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual)
    assert result.status == PolicyActuationStatus.CANDIDATE_FAILED_FAIL_SAFE_APPLIED
    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == "Self-Use"
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"


@pytest.mark.asyncio
async def test_documented_unlimited_is_written_and_finally_proven():
    from custom_components.house_battery_control.contracts import DocumentedUnlimitedValue

    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    entity_id = actuator.config.capability.max_output_power_entity_id
    record = CapabilityResolutionRecord(entity_id, "W", DocumentedUnlimitedValue(), NOW, actuator.mapping_fingerprint, actuator.policy_fingerprint, "manual_commissioning", Decimal("6000"))
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual, capability_resolutions={entity_id: record})
    assert result.status == PolicyActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION
    assert ha.states[entity_id]["state"] == "6000"


@pytest.mark.asyncio
async def test_cached_post_write_observation_fails_safe():
    actuator, _ha, initial, token, manual, _clock, _record = policy_fixture()
    actuator.observation_refresh = lambda _boundary: initial
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual)
    assert result.status == PolicyActuationStatus.CANDIDATE_FAILED_FAIL_SAFE_APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("over", "force", "recovery", "maximum", "grid", "export", "peak", "reserve_soc", "reserve", "mode"))
async def test_failure_at_every_persistent_candidate_stage_exits_transaction_then_fails_safe(stage):
    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    targets = {
        "over": actuator.config.protection.battery_over_discharge_soc_entity_id,
        "force": actuator.config.protection.battery_force_charge_soc_entity_id,
        "recovery": actuator.config.protection.battery_recovery_soc_entity_id,
        "maximum": actuator.config.protection.battery_max_charge_soc_entity_id,
        "grid": actuator.config.persistent.allow_grid_charging_entity_id,
        "export": actuator.config.persistent.allow_export_entity_id,
        "peak": actuator.config.persistent.grid_peak_shaving_entity_id,
        "reserve_soc": actuator.config.protection.battery_reserve_soc_entity_id,
        "reserve": actuator.config.protection.battery_reserve_entity_id,
        "mode": actuator.config.persistent.storage_mode_entity_id,
    }
    if stage in {"over", "recovery"}:
        ha.states[targets[stage]]["state"] = "11"
    elif stage in {"grid", "export", "peak"}:
        ha.states[targets[stage]]["state"] = "off"
    failed = False

    def fail_once(_domain, _service, data):
        nonlocal failed
        if data["entity_id"] == targets[stage] and not failed:
            failed = True
            raise RuntimeError("injected candidate failure")

    ha.on_call = fail_once
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual)
    assert result.status == PolicyActuationStatus.CANDIDATE_FAILED_FAIL_SAFE_APPLIED
    assert result.candidate_results
    assert ha.states[actuator.config.persistent.storage_mode_entity_id]["state"] == "Self-Use"
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"


@pytest.mark.asyncio
async def test_stale_capability_record_is_preserved_not_applied():
    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    entity_id = actuator.config.capability.max_output_power_entity_id
    stale = CapabilityResolutionRecord(entity_id, "W", Decimal("6000"), NOW - timedelta(days=31), actuator.mapping_fingerprint, actuator.policy_fingerprint, "manual_commissioning")
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual, capability_resolutions={entity_id: stale})
    assert result.status == PolicyActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION
    assert ha.states[entity_id]["state"] == "5000"


@pytest.mark.asyncio
async def test_manual_grid_mismatch_and_clock_skew_block_without_mutation():
    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    invalid = replace(manual, maximum_grid_import_power_kw=Decimal("0.2"))
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=invalid)
    assert result.status == PolicyActuationStatus.BLOCKED
    assert ha.calls == []

    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    stale_clock = replace(initial, snapshot=replace(initial.snapshot, persistent=replace(initial.snapshot.persistent, inverter_time=NOW - timedelta(minutes=2))))
    result = await actuator.async_apply_candidate(stale_clock, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual)
    assert result.status == PolicyActuationStatus.BLOCKED
    assert ha.calls == []


@pytest.mark.asyncio
async def test_explicit_fail_safe_failure_is_reported_unsafe_and_continues():
    actuator, ha, _initial, _token, _manual, _clock, _record = policy_fixture()
    mode = actuator.config.persistent.storage_mode_entity_id
    original = ha.async_call

    async def fail_mode(domain, service, data, *, blocking=True):
        if data["entity_id"] == mode:
            raise RuntimeError("injected")
        await original(domain, service, data, blocking=blocking)

    ha.states[mode]["state"] = "Feed-In Priority"
    ha.async_call = fail_mode
    result = await actuator.async_apply_fail_safe()
    assert result.status == PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("mode", "peak", "reserve"))
async def test_fail_safe_continues_after_each_persistent_failure(stage):
    actuator, ha, _initial, _token, _manual, _clock, _record = policy_fixture()
    targets = {
        "mode": actuator.config.persistent.storage_mode_entity_id,
        "peak": actuator.config.persistent.grid_peak_shaving_entity_id,
        "reserve": actuator.config.protection.battery_reserve_entity_id,
    }
    ha.states[targets["mode"]]["state"] = "Feed-In Priority"
    ha.states[targets["peak"]]["state"] = "off"
    ha.states[targets["reserve"]]["state"] = "on"
    original = ha.async_call

    async def fail_selected(domain, service, data, *, blocking=True):
        if data["entity_id"] == targets[stage]:
            raise RuntimeError("injected fail-safe failure")
        await original(domain, service, data, blocking=blocking)

    ha.async_call = fail_selected
    result = await actuator.async_apply_fail_safe()
    assert result.status == PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE
    for other, entity_id in targets.items():
        if other != stage:
            expected = "Self-Use" if other == "mode" else ("on" if other == "peak" else "off")
            assert ha.states[entity_id]["state"] == expected


@pytest.mark.asyncio
async def test_repeated_cancellation_while_waiting_for_shared_lock_preserves_original_and_cleans():
    actuator, _ha, initial, token, manual, _clock, _record = policy_fixture()
    await actuator.orchestration_lock.acquire()
    task = asyncio.create_task(actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual))
    await asyncio.sleep(0)
    task.cancel("original")
    await asyncio.sleep(0)
    task.cancel("repeated")
    actuator.orchestration_lock.release()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    diagnostic = actuator.last_cancellation_diagnostic
    assert diagnostic is not None
    assert diagnostic.original_exception is raised.value
    assert diagnostic.safe_proven
    reused = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual)
    assert reused.status == PolicyActuationStatus.BLOCKED


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_evidence", (None, object(), "malformed"))
async def test_malformed_manual_grid_evidence_is_structured_blocked_without_mutation(manual_evidence):
    actuator, ha, initial, token, _manual, _clock, _record = policy_fixture()
    result = await actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual_evidence)
    assert result.status == PolicyActuationStatus.BLOCKED
    assert "manual grid-import verification" in result.issues[0]
    assert ha.calls == []


@pytest.mark.asyncio
async def test_explicit_fail_safe_cancellation_is_shielded_and_diagnostic():
    actuator, ha, _initial, _token, _manual, _clock, _record = policy_fixture()
    mode = actuator.config.persistent.storage_mode_entity_id
    ha.states[mode]["state"] = "Feed-In Priority"
    original = ha.async_call
    started = asyncio.Event()
    release = asyncio.Event()

    async def block_once(domain, service, data, *, blocking=True):
        if data["entity_id"] == mode and not started.is_set():
            started.set()
            await release.wait()
        await original(domain, service, data, blocking=blocking)

    ha.async_call = block_once
    task = asyncio.create_task(actuator.async_apply_fail_safe())
    await started.wait()
    task.cancel("original")
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert actuator.last_cancellation_diagnostic.original_exception is raised.value
    assert actuator.last_cancellation_diagnostic.safe_proven


@pytest.mark.asyncio
async def test_cancellation_during_running_candidate_fallback_preserves_original_and_concludes_cleanup():
    actuator, ha, initial, token, manual, _clock, _record = policy_fixture()
    force_id = actuator.config.protection.battery_force_charge_soc_entity_id
    mode_id = actuator.config.persistent.storage_mode_entity_id
    ha.states[mode_id]["state"] = "Feed-In Priority"
    fallback_started = asyncio.Event()
    release = asyncio.Event()
    candidate_failed = False

    async def fail_then_block_fallback(_domain, _service, data):
        nonlocal candidate_failed
        if data["entity_id"] == force_id and not candidate_failed:
            candidate_failed = True
            raise RuntimeError("start fallback")
        if data["entity_id"] == mode_id and not fallback_started.is_set():
            fallback_started.set()
            await release.wait()

    ha.on_call = fail_then_block_fallback
    task = asyncio.create_task(actuator.async_apply_candidate(initial, now=NOW, reserve_target=20, authorization=token, manual_grid_import_verification=manual))
    await fallback_started.wait()
    task.cancel("original")
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    diagnostic = actuator.last_cancellation_diagnostic
    assert diagnostic is not None
    assert diagnostic.original_exception is raised.value
    assert diagnostic.safe_proven
    assert ha.states[mode_id]["state"] == "Self-Use"
    assert ha.states[actuator.config.protection.battery_reserve_entity_id]["state"] == "off"
