"""Offline lifecycle contracts for the guarded T0011 workflow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from custom_components.house_battery_control.commissioning import (
    APPLICATION_AUTHORIZATION_LIFETIME,
    COMMISSIONING_OBSERVATION_WINDOW,
    CONFIRMATION_PHRASE,
    CleanupProof,
    CleanupStateEvidence,
    CommissionedEnvelopeEvidenceProvider,
    CommissioningSession,
    ManualGridImportVerification,
    REQUIRED_OUTCOMES,
    CommissioningStatus,
    CommissioningWorkflow,
    service_schemas,
)
from custom_components.house_battery_control.contracts import ControllerHealth, StorageMode
from custom_components.house_battery_control.solis_policy import EphemeralAuthorizationStore
from custom_components.house_battery_control.solis_policy import PolicyActuationResult, PolicyActuationStatus


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FP = "a" * 64
POLICY = "b" * 64


def _cap(value: str = "10") -> SimpleNamespace:
    return SimpleNamespace(current_value=Decimal(value), minimum=Decimal("0"), maximum=Decimal("100"), step=Decimal("1"), unit="%")


def _state(at: datetime, *, device_at: datetime | None = None) -> SimpleNamespace:
    direction = SimpleNamespace(enabled=False)
    slots = tuple(SimpleNamespace(charge=direction, discharge=direction) for _ in range(6))
    persistent = SimpleNamespace(
        storage_mode=StorageMode.FEED_IN_PRIORITY.value,
        allow_grid_charging=True,
        allow_export=True,
        grid_peak_shaving=True,
        battery_reserve=True,
        over_discharge_soc=_cap("10"),
        force_charge_soc=_cap("7"),
        recovery_soc=_cap("10"),
        maximum_charge_soc=_cap("100"),
        battery_reserve_soc=_cap("20"),
        inverter_on_off=True,
        inverter_time=at,
    )
    telemetry = SimpleNamespace(device_timestamp=device_at or at, battery_power_kw=Decimal("0"), state_of_charge_percent=Decimal("50"))
    return SimpleNamespace(observed_at=at, telemetry=telemetry, persistent=persistent, slots=slots, capabilities=SimpleNamespace())


class FakeActuator:
    def __init__(self, *, status: str = "APPLIED_HA_PENDING_DEVICE_RECONCILIATION") -> None:
        self.mapping_fingerprint = FP
        self.policy_fingerprint = POLICY
        self.ephemeral_authorizations = EphemeralAuthorizationStore()
        self.fail_safe_calls = 0
        self.status = status

    async def async_apply_candidate(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        if self.status == "APPLIED_HA_PENDING_DEVICE_RECONCILIATION":
            return PolicyActuationResult(PolicyActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION)
        return PolicyActuationResult(self.status)

    async def async_apply_fail_safe(self) -> SimpleNamespace:
        self.fail_safe_calls += 1
        return PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION)

    async def async_fresh_fail_safe_proof(self, attempt_started_at: datetime) -> CleanupProof:
        # The production workflow requires the same exact guard + fifteen
        # controlled entities from a shared actuator proof.  Keeping this
        # explicit in the double prevents tests from accidentally exercising
        # the removed "typed result is enough" exception.
        entity_ids = ["input_boolean.house_battery_control_disable"]
        entity_ids.extend(f"switch.slot_{index}_{direction}" for index in range(1, 7) for direction in ("charge", "discharge"))
        entity_ids.extend(("select.storage_mode", "switch.grid_peak_shaving", "switch.battery_reserve"))
        observed_at = attempt_started_at + timedelta(microseconds=1)
        states = tuple(
            CleanupStateEvidence(entity_id, "on" if entity_id.startswith("input_boolean") or "peak" in entity_id else "off", "on" if entity_id.startswith("input_boolean") or "peak" in entity_id else "off", attempt_started_at, f"revision-{index}", True, observed_at)
            for index, entity_id in enumerate(entity_ids)
        )
        return CleanupProof(observed_at, attempt_started_at, states, True, True, source="shared-actuator")


class FakeHass:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.services = SimpleNamespace(async_call=self._call)

    async def _call(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


def _coordinator(state: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(data=SimpleNamespace(
        health=ControllerHealth.HEALTHY,
        fail_safe_obligation=False,
        fail_safe_pending=False,
        guard_quality="valid",
        guard_state="off",
        fail_safe_proof=SimpleNamespace(complete=True, ha_safe=True, states=(SimpleNamespace(matches=True),)),
        solis=SimpleNamespace(is_healthy=True, snapshot=state),
    ))


def _begin_data() -> dict[str, object]:
    return {
        "confirmation": CONFIRMATION_PHRASE,
        "reserve_target": "20",
        "operator_attestation": True,
        "manual_grid_import_power_kw": "0.1",
        "manual_grid_setting_verified": True,
        "manual_grid_verified_at": NOW.isoformat(),
        "manual_grid_mapping_fingerprint": FP,
        "manual_grid_policy_fingerprint": POLICY,
    }


def _validate_data(session_id: str, observed: datetime) -> dict[str, object]:
    return {
        "session_id": session_id,
        "device_readback_attested": True,
        "observation_after_candidate": True,
        "home_assistant_context_consistent": True,
        "observed_device_timestamp": observed.isoformat(),
        "outcomes": [
            {"name": name, "status": "PASS", "evidence_note": "independent readback", "observed_at": observed.isoformat()}
            for name in REQUIRED_OUTCOMES
        ],
        "commissioned_power_envelope": None,
    }


@pytest.mark.asyncio
async def test_default_disabled_and_prestart_are_structured_and_mutation_free() -> None:
    actuator = FakeActuator()
    workflow = CommissioningWorkflow(FakeHass(), _coordinator(_state(NOW)), enabled=False, clock=lambda: NOW, actuator=actuator)
    response = await workflow.async_begin(_begin_data())
    assert response["status"] == CommissioningStatus.BLOCKED.value
    assert response["session_id"] is None
    assert actuator.fail_safe_calls == 0


@pytest.mark.asyncio
async def test_nonce_is_consumed_before_candidate_await_and_success_is_review_only() -> None:
    actuator = FakeActuator()
    coordinator = _coordinator(_state(NOW))
    workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    begin = await workflow.async_begin(_begin_data())
    assert begin["status"] == CommissioningStatus.TEST_APPLIED_HA_PENDING_DEVICE.value
    session = workflow.session
    assert session is not None
    assert session.deadline - session.candidate_applied_at == COMMISSIONING_OBSERVATION_WINDOW
    assert session.nonce.nonce in actuator.ephemeral_authorizations._consumed
    observed = NOW + timedelta(seconds=2)
    coordinator.data.solis.snapshot = _state(observed, device_at=observed)
    workflow.clock = lambda: observed
    result = await workflow.async_validate(_validate_data(session.session_id, observed))
    assert result["status"] == CommissioningStatus.COMPLETE_REVIEW_REQUIRED.value
    assert result["iac_snippet"].count("services_enabled: false") == 1
    assert "production_slot_commissioning:" not in result["iac_snippet"]
    assert "nonce" not in result["iac_snippet"]
    assert workflow.session is None


@pytest.mark.asyncio
async def test_abort_and_expiry_assert_guard_and_run_fail_safe() -> None:
    actuator = FakeActuator()
    workflow = CommissioningWorkflow(FakeHass(), _coordinator(_state(NOW)), enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    # A fake actuator result is enough to establish the pending lifecycle.
    await workflow.async_begin(_begin_data())
    expired = await workflow.async_expire()
    assert expired["status"] == CommissioningStatus.FAILED_SAFE.value
    assert actuator.fail_safe_calls == 1
    aborted = await workflow.async_abort()
    assert aborted["status"] == CommissioningStatus.FAILED_SAFE.value
    assert actuator.fail_safe_calls == 2


@pytest.mark.asyncio
async def test_guard_revision_is_changed_only_for_off_to_on_transition() -> None:
    actuator = FakeActuator()
    hass = FakeHass()
    entity_id = "input_boolean.house_battery_control_disable"
    current = SimpleNamespace(state="off", context=SimpleNamespace(id="before"))

    class States:
        def get(self, requested: str) -> object:
            assert requested == entity_id
            return current

    hass.states = States()

    async def turn_on(*_args: object, **_kwargs: object) -> None:
        nonlocal current
        current = SimpleNamespace(state="on", context=SimpleNamespace(id="after"))

    hass.services.async_call = turn_on
    workflow = CommissioningWorkflow(hass, _coordinator(_state(NOW)), enabled=True, clock=lambda: NOW, actuator=actuator)
    changed = await workflow._assert_guard()
    assert changed == frozenset({entity_id})

    # An already-on guard is idempotent and does not require a new revision.
    changed = await workflow._assert_guard()
    assert changed == frozenset()


@pytest.mark.asyncio
async def test_expiry_drain_propagates_cancellation_and_keeps_child_owned() -> None:
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    workflow = CommissioningWorkflow(FakeHass(), _coordinator(_state(NOW)), enabled=True, clock=lambda: NOW, actuator=FakeActuator())
    child = asyncio.create_task(stubborn())
    workflow._expiry_task = child
    drain = asyncio.create_task(workflow._drain_expiry_task())
    await asyncio.sleep(0)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    assert workflow._expiry_task is child
    release.set()
    await child


@pytest.mark.asyncio
async def test_cancellation_resistant_fail_safe_io_is_bounded_and_owned() -> None:
    release = asyncio.Event()

    class StubbornActuator(FakeActuator):
        async def async_apply_fail_safe(self, *_args: object, **_kwargs: object) -> object:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE)

    workflow = CommissioningWorkflow(FakeHass(), _coordinator(_state(NOW)), enabled=True, clock=lambda: NOW, actuator=StubbornActuator())
    result, _changed, _proof = await workflow._invoke_fail_safe(NOW + timedelta(milliseconds=10), NOW)
    assert isinstance(result, PolicyActuationResult)
    assert result.status == PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE
    assert workflow._cleanup_io_task is not None
    release.set()
    await asyncio.wait_for(asyncio.shield(workflow._cleanup_io_task), 1)


@pytest.mark.asyncio
async def test_missing_behavior_evidence_stays_pending_without_extending_deadline() -> None:
    actuator = FakeActuator()
    coordinator = _coordinator(_state(NOW))
    workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    await workflow.async_begin(_begin_data())
    session = workflow.session
    assert session is not None
    observed = NOW + timedelta(seconds=2)
    coordinator.data.solis.snapshot = _state(observed, device_at=observed)
    workflow.clock = lambda: observed
    pending_data = _validate_data(session.session_id, observed)
    pending_data["outcomes"] = [
        {"name": name, "status": "MISSING", "evidence_note": "awaiting supervised observation", "observed_at": observed.isoformat()}
        for name in REQUIRED_OUTCOMES
    ]
    result = await workflow.async_validate(pending_data)
    assert result["status"] == CommissioningStatus.PENDING_EVIDENCE.value
    assert workflow.session is session
    assert session.deadline == NOW + COMMISSIONING_OBSERVATION_WINDOW
    assert actuator.fail_safe_calls == 0


@pytest.mark.asyncio
async def test_fingerprint_drift_and_candidate_failure_are_fail_safe() -> None:
    actuator = FakeActuator(status="FAILED_UNSAFE")
    workflow = CommissioningWorkflow(FakeHass(), _coordinator(_state(NOW)), enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    failed = await workflow.async_begin(_begin_data())
    assert failed["status"] == CommissioningStatus.FAILED_SAFE.value
    assert actuator.fail_safe_calls == 1

    actuator = FakeActuator()
    coordinator = _coordinator(_state(NOW))
    workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    await workflow.async_begin(_begin_data())
    actuator.mapping_fingerprint = "c" * 64
    result = await workflow.async_validate({})
    assert result["status"] == CommissioningStatus.FAILED_SAFE.value
    assert "fingerprint_drift" in result["issues"]
    assert actuator.fail_safe_calls == 1


@pytest.mark.asyncio
async def test_validation_mismatch_asserts_guard_and_does_not_emit_iac() -> None:
    actuator = FakeActuator()
    coordinator = _coordinator(_state(NOW))
    workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    await workflow.async_begin(_begin_data())
    session = workflow.session
    assert session is not None
    observed = NOW + timedelta(seconds=2)
    invalid_state = _state(observed, device_at=observed)
    invalid_state.persistent.storage_mode = StorageMode.SELF_USE.value
    coordinator.data.solis.snapshot = invalid_state
    workflow.clock = lambda: observed
    result = await workflow.async_validate(_validate_data(session.session_id, observed))
    assert result["status"] == CommissioningStatus.FAILED_SAFE.value
    assert result["iac_snippet"] is None
    assert actuator.fail_safe_calls == 1


def test_named_lifetimes_and_strict_service_shapes() -> None:
    assert APPLICATION_AUTHORIZATION_LIFETIME == timedelta(minutes=10)
    assert COMMISSIONING_OBSERVATION_WINDOW == timedelta(hours=2)
    schemas = service_schemas()
    with pytest.raises(Exception):
        schemas["begin_candidate_commissioning"]({"confirmation": "wrong"})
    with pytest.raises(Exception):
        schemas["abort_candidate_commissioning"]({"unexpected": True})


@pytest.mark.asyncio
async def test_begin_preconditions_are_fail_closed_and_restart_drops_session() -> None:
    for mutate in (
        lambda c: setattr(c.data, "guard_state", "on"),
        lambda c: setattr(c.data, "fail_safe_obligation", True),
        lambda c: setattr(c.data.solis, "is_healthy", False),
    ):
        coordinator = _coordinator(_state(NOW))
        mutate(coordinator)
        actuator = FakeActuator()
        workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
        await workflow.async_start()
        result = await workflow.async_begin(_begin_data())
        assert result["status"] == CommissioningStatus.BLOCKED.value
        assert actuator.fail_safe_calls == 0

    coordinator = _coordinator(_state(NOW))
    actuator = FakeActuator()
    workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    await workflow.async_begin(_begin_data())
    assert workflow.session is not None
    await workflow.async_stop()
    assert workflow.session is None
    assert actuator.fail_safe_calls == 1


def test_ephemeral_authorization_is_forgery_bound_single_use_and_expiring() -> None:
    store = EphemeralAuthorizationStore()
    token = store.issue(now=NOW, mapping=FP, policy=POLICY)
    forged = type(token)(token.issued_at, token.expires_at, token.nonce, "c" * 64, POLICY)
    assert not store.prepare_before_await(forged)
    assert not store.consume_attempt(forged)
    token = store.issue(now=NOW, mapping=FP, policy=POLICY)
    assert store.prepare_before_await(token)
    assert store.consume_attempt(token)
    assert not store.consume_attempt(token)
    expired = store.issue(now=NOW, mapping=FP, policy=POLICY)
    assert not store.consume(expired, now=NOW + APPLICATION_AUTHORIZATION_LIFETIME + timedelta(seconds=1), mapping=FP, policy=POLICY)


@pytest.mark.asyncio
async def test_default_envelope_provider_is_explicitly_incomplete() -> None:
    manual = ManualGridImportVerification(Decimal("0.1"), NOW, FP, POLICY, True)
    session = CommissioningSession("source-session", FP, POLICY, Decimal("20"), NOW, NOW, NOW + COMMISSIONING_OBSERVATION_WINDOW, _state(NOW), NOW, True, manual, {}, (), object(), object(), "inverter")
    provider = CommissionedEnvelopeEvidenceProvider()
    assert await provider.provide(
        session=session,
        observed_device_timestamp=NOW + timedelta(seconds=2),
        observed_at=NOW + timedelta(seconds=2),
        now=NOW + timedelta(seconds=2),
        mapping_fingerprint=FP,
        policy_fingerprint=POLICY,
        manual_grid_fingerprint=FP,
        capability_fingerprint=POLICY,
    ) is None


@pytest.mark.asyncio
async def test_begin_service_cannot_submit_capability_or_envelope_authority() -> None:
    actuator = FakeActuator()
    workflow = CommissioningWorkflow(FakeHass(), _coordinator(_state(NOW)), enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    data = _begin_data()
    data["capability_resolutions"] = []
    data["commissioned_power_envelope"] = {"maximum_charge_power_kw": "4"}
    response = await workflow.async_begin(data)
    assert response["status"] == CommissioningStatus.BLOCKED.value
    assert "strict_begin_schema_rejected" in response["issues"]


@pytest.mark.asyncio
async def test_guard_and_health_lifecycle_abort_pending_session() -> None:
    coordinator = _coordinator(_state(NOW))
    actuator = FakeActuator()
    workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    await workflow.async_begin(_begin_data())
    coordinator.data.guard_state = "on"
    await workflow.async_handle_lifecycle()
    assert workflow.session is None
    assert actuator.fail_safe_calls == 1


@pytest.mark.asyncio
async def test_every_candidate_readback_field_mismatch_is_critical() -> None:
    fields = ("allow_grid_charging", "allow_export", "grid_peak_shaving", "battery_reserve", "over_discharge_soc", "force_charge_soc", "recovery_soc", "maximum_charge_soc", "battery_reserve_soc", "inverter_on_off")
    for field in fields:
        coordinator = _coordinator(_state(NOW))
        actuator = FakeActuator()
        workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
        await workflow.async_start()
        await workflow.async_begin(_begin_data())
        session = workflow.session
        assert session is not None
        observed = NOW + timedelta(seconds=2)
        state = _state(observed, device_at=observed)
        if field.endswith("soc"):
            setattr(state.persistent, field, _cap("19"))
        elif field == "inverter_on_off":
            state.persistent.inverter_on_off = False
        else:
            setattr(state.persistent, field, False)
        coordinator.data.solis.snapshot = state
        workflow.clock = lambda: observed
        result = await workflow.async_validate(_validate_data(session.session_id, observed))
        assert result["status"] == CommissioningStatus.FAILED_SAFE.value


@pytest.mark.asyncio
async def test_future_envelope_authority_is_rejected_without_review_snippet() -> None:
    coordinator = _coordinator(_state(NOW))
    actuator = FakeActuator()
    workflow = CommissioningWorkflow(FakeHass(), coordinator, enabled=True, clock=lambda: NOW, actuator=actuator)
    await workflow.async_start()
    await workflow.async_begin(_begin_data())
    session = workflow.session
    assert session is not None
    observed = NOW + timedelta(seconds=2)
    coordinator.data.solis.snapshot = _state(observed, device_at=observed)
    workflow.clock = lambda: observed
    data = _validate_data(session.session_id, observed)
    data["commissioned_power_envelope"] = {key: value for key, value in {
        "maximum_charge_power_kw": "5", "maximum_discharge_power_kw": "5", "maximum_grid_import_power_kw": "0.1",
        "schema_version": "1", "inverter_identity": "inverter", "mapping_fingerprint": FP,
        "candidate_policy_fingerprint": POLICY, "manual_grid_fingerprint": FP, "capability_fingerprint": POLICY,
        "evidence_source": "device", "validated_at": (observed + timedelta(days=1)).isoformat(),
    }.items()}
    result = await workflow.async_validate(data)
    assert result["status"] == CommissioningStatus.FAILED_SAFE.value
    assert result["iac_snippet"] is None
