"""Guarded, offline-testable candidate commissioning workflow.

This module is the only owner of the two-step persistent-candidate
commissioning lifecycle.  It deliberately keeps the runtime session in
memory: the generated configuration is a disabled human-review artifact and
never an authorization to write slots or to enable the dynamic controller.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import voluptuous as vol

from .contracts import ControllerHealth, StorageMode
from .domain_constants import (
    FORCE_CHARGE_SOC_PERCENT,
    FULL_SOC_PERCENT,
    MAXIMUM_GRID_IMPORT_POWER_KW,
    MINIMUM_SOC_PERCENT,
)
from .reserve_planner import CommissionedPowerEnvelope
from .solis_actuator import COMMISSIONING_SCHEMA_VERSION, MAXIMUM_INVERTER_CLOCK_SKEW, mapping_fingerprint
from .solis_policy import (
    CapabilityResolutionRecord,
    ManualGridImportVerification,
    PolicyActuationResult,
    PolicyActuationStatus,
    bounded_reserve_soc,
    policy_fingerprint,
)
from .solis_state import MAXIMUM_TELEMETRY_AGE

# Keep these names explicit and independent.  T0007's ten-minute nonce and
# this workflow's supervised two-hour observation window are different clocks.
APPLICATION_AUTHORIZATION_LIFETIME = timedelta(minutes=10)
COMMISSIONING_OBSERVATION_WINDOW = timedelta(hours=2)
CONFIRMATION_PHRASE = "COMMISSION HOUSE BATTERY CANDIDATE"

SERVICE_BEGIN = "begin_candidate_commissioning"
SERVICE_VALIDATE = "validate_candidate_commissioning"
SERVICE_ABORT = "abort_candidate_commissioning"

REQUIRED_OUTCOMES = (
    "reserve_prevents_discharge_below_target",
    "reserve_does_not_unwanted_grid_charge",
    "reserve_consistent_feed_in_priority",
    "peak_shaving_respects_manual_import_limit",
    "protection_and_slot_soc_bounds",
    "feed_in_priority_and_slots_stable",
)


class CommissioningStatus(str, Enum):
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"
    PENDING_EVIDENCE = "PENDING_EVIDENCE"
    TEST_APPLIED_HA_PENDING_DEVICE = "TEST_APPLIED_HA_PENDING_DEVICE"
    TEST_PROVEN_OFF = "TEST_PROVEN_OFF"
    COMPLETE_REVIEW_REQUIRED = "COMPLETE_REVIEW_REQUIRED"
    APPLIED = "APPLIED"
    VALIDATED = "VALIDATED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"
    FAILED_SAFE = "FAILED_SAFE"
    FAILED_UNSAFE = "FAILED_UNSAFE"


# T0007's actuator is deliberately the only fail-safe authority.  This
# workflow-owned bound is used for guard, actuator, and fresh-proof work as a
# single absolute deadline; it is never reset between those operations.
CLEANUP_DEADLINE = timedelta(seconds=45)


@dataclass(frozen=True, slots=True)
class CleanupStateEvidence:
    """One exact post-attempt state used by the commissioning proof."""

    entity_id: str
    expected_state: str
    observed_state: str | None
    observed_at: datetime | None
    revision: str | None
    matches: bool
    state_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CleanupProof:
    """Typed proof of the specific fail-safe attempt, never coordinator data."""

    observed_at: datetime
    attempt_started_at: datetime
    states: tuple[CleanupStateEvidence, ...]
    complete: bool
    ha_safe: bool
    source: str = "commissioning"

    @property
    def all_slots_off(self) -> bool:
        slot_states = [item for item in self.states if ".slot" in item.entity_id or "slot" in item.entity_id]
        return self.complete and len(slot_states) == 12 and all(item.matches for item in slot_states)

    @property
    def post_attempt(self) -> bool:
        return self.observed_at > self.attempt_started_at


@dataclass(slots=True)
class CleanupObligation:
    """Persistent fail-safe work independent of the commissioning session."""

    reason: str
    deadline: datetime
    attempt_started_at: datetime | None = None
    result: PolicyActuationResult | None = None
    proof: CleanupProof | None = None
    issues: list[str] = field(default_factory=list)
    completed_at: datetime | None = None
    generation: int = 0

    @property
    def pending(self) -> bool:
        return self.result is None or self.proof is None

    @property
    def safe(self) -> bool:
        ids = {item.entity_id for item in self.proof.states} if type(self.proof) is CleanupProof else set()
        slots = {item for item in ids if "slot" in item}
        non_slots = ids - slots
        exact_state_shape = (
            len(ids) == 16
            and len(slots) == 12
            and len(non_slots) == 4
            and any("disable" in item or "guard" in item for item in non_slots)
            and any("storage" in item or "mode" in item for item in non_slots)
            and any("peak" in item for item in non_slots)
            and any("reserve" in item for item in non_slots)
        )
        return (
            type(self.result) is PolicyActuationResult
            and self.result.status == PolicyActuationStatus.FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION
            and type(self.proof) is CleanupProof
            and self.proof.complete
            and self.proof.ha_safe
            and self.proof.post_attempt
            and len(self.proof.states) == 16
            and exact_state_shape
            and self.proof.all_slots_off
            and all(item.matches for item in self.proof.states)
        )


@dataclass(frozen=True, slots=True)
class CommissionedEnvelopeEvidenceRecord:
    """A typed provider record produced from a post-apply device observation."""

    envelope: CommissionedPowerEnvelope
    session_id: str
    observed_device_timestamp: datetime
    observed_at: datetime
    mapping_fingerprint: str
    policy_fingerprint: str
    manual_grid_fingerprint: str
    capability_fingerprint: str
    device_revision: str = ""


class CommissionedEnvelopeEvidenceProvider:
    """Concrete source for independently verified AC boundary evidence.

    The workflow never treats a service payload as evidence.  A deployment may
    feed this provider a typed record after its cloud/device readback process;
    ``record`` validates that record is tied to the exact session measurement
    and fingerprints.  The default provider has no record, so the envelope is
    correctly absent rather than guessed from HA metadata or amperes.
    """

    def __init__(self, records: Mapping[str, CommissionedEnvelopeEvidenceRecord] | None = None) -> None:
        self._records = dict(records or {})

    def _record_reviewed(
        self,
        *,
        session_id: str,
        envelope: CommissionedPowerEnvelope,
        observed_device_timestamp: datetime,
        observed_at: datetime,
        mapping_fingerprint: str,
        policy_fingerprint: str,
        manual_grid_fingerprint: str,
        capability_fingerprint: str,
        device_revision: str | None = None,
    ) -> CommissionedEnvelopeEvidenceRecord:
        if type(envelope) is not CommissionedPowerEnvelope:
            raise TypeError("commissioned envelope must be the concrete typed envelope")
        checked_observed_at = _aware(observed_at, "observed_at")
        checked_device_timestamp = _aware(observed_device_timestamp, "observed_device_timestamp")
        checked_mapping = _fingerprint(mapping_fingerprint, "mapping_fingerprint")
        checked_policy = _fingerprint(policy_fingerprint, "policy_fingerprint")
        checked_manual = _fingerprint(manual_grid_fingerprint, "manual_grid_fingerprint")
        checked_capability = _fingerprint(capability_fingerprint, "capability_fingerprint")
        if envelope.validated_at != checked_observed_at:
            raise ValueError("commissioned envelope validation time must equal its observation")
        if envelope.maximum_charge_power_kw <= 0 or envelope.maximum_discharge_power_kw <= 0 or envelope.maximum_grid_import_power_kw != MAXIMUM_GRID_IMPORT_POWER_KW:
            raise ValueError("commissioned envelope AC boundaries must be nonzero and use the manual import limit")
        if envelope.mapping_fingerprint != checked_mapping or envelope.candidate_policy_fingerprint != checked_policy or envelope.manual_grid_fingerprint != checked_manual or envelope.capability_fingerprint != checked_capability:
            raise ValueError("commissioned envelope authority fingerprints do not match its evidence record")
        record = CommissionedEnvelopeEvidenceRecord(
            envelope, _text(session_id, "session_id"), checked_device_timestamp,
            checked_observed_at, checked_mapping, checked_policy, checked_manual,
            checked_capability, _text(device_revision or checked_device_timestamp.isoformat(), "device_revision"),
        )
        self._records[session_id] = record
        return record

    async def provide(self, *, session: "CommissioningSession", observed_device_timestamp: datetime, observed_at: datetime, now: datetime, mapping_fingerprint: str, policy_fingerprint: str, manual_grid_fingerprint: str, capability_fingerprint: str, device_revision: str | None = None, **_: object) -> CommissionedPowerEnvelope | None:
        record = self._records.get(session.session_id)
        if record is None:
            return None
        if (
            record.session_id != session.session_id
            or record.observed_device_timestamp != observed_device_timestamp
            or record.observed_at != observed_at
            or record.mapping_fingerprint != mapping_fingerprint
            or record.policy_fingerprint != policy_fingerprint
            or record.manual_grid_fingerprint != manual_grid_fingerprint
            or record.capability_fingerprint != capability_fingerprint
            or (device_revision is not None and record.device_revision != device_revision)
            or record.observed_at < session.candidate_applied_at
            or record.observed_at > min(now, session.deadline)
        ):
            return None
        return record.envelope

    async def async_provide(self, **kwargs: object) -> CommissionedPowerEnvelope | None:
        return await self.provide(**kwargs)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return result


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a SHA-256 fingerprint")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 fingerprint") from exc
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _jsonable(value: object) -> object:
    """Convert immutable evidence objects to deterministic JSON-shaped data."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if hasattr(value, "__dict__"):
        return {str(name): _jsonable(item) for name, item in sorted(vars(value).items())}
    return value


@dataclass(frozen=True, slots=True)
class BehaviorOutcome:
    name: str
    status: str
    evidence_note: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.name not in REQUIRED_OUTCOMES:
            raise ValueError(f"unknown commissioning outcome: {self.name}")
        if self.status not in {"PASS", "FAIL", "AMBIGUOUS", "MISSING"}:
            raise ValueError("commissioning outcome status is invalid")
        if not self.evidence_note:
            raise ValueError("commissioning outcome evidence_note is required")
        _aware(self.observed_at, "outcome observed_at")


@dataclass(frozen=True, slots=True)
class CommissioningEvidence:
    """Evidence captured after the candidate was applied."""

    outcomes: tuple[BehaviorOutcome, ...]
    device_readback_attested: bool
    observation_after_candidate: bool
    home_assistant_context_consistent: bool
    observed_device_timestamp: datetime | None = None
    power_envelope: CommissionedPowerEnvelope | None = None
    capability_evidence: Mapping[str, object] = field(default_factory=dict)
    manual_grid_verification: ManualGridImportVerification | None = None
    capability_resolutions: tuple[CapabilityResolutionRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class CommissioningSession:
    session_id: str
    mapping_fingerprint: str
    policy_fingerprint: str
    reserve_target: Decimal
    issued_at: datetime
    candidate_applied_at: datetime
    deadline: datetime
    before_snapshot: object
    before_device_timestamp: datetime | None
    preserved_inverter_on: bool | None
    manual_grid_verification: ManualGridImportVerification
    before_capabilities: Mapping[str, object]
    capability_resolutions: tuple[CapabilityResolutionRecord, ...]
    nonce: object
    candidate_result: object
    inverter_identity: str | None = None


def _outcome_from_data(item: object, now: datetime) -> BehaviorOutcome:
    if not isinstance(item, Mapping):
        raise ValueError("each outcome must be a mapping")
    expected = {"name", "status", "evidence_note", "observed_at"}
    if set(item) != expected:
        raise ValueError("outcome contains missing or unknown keys")
    value = item["observed_at"]
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return BehaviorOutcome(
        str(item["name"]), str(item["status"]), str(item["evidence_note"]), _aware(value, "outcome observed_at")
    )


class CommissioningWorkflow:
    """Serialized commissioning service/lifecycle boundary.

    ``coordinator`` and ``actuator`` are intentionally duck-typed.  This keeps
    every policy decision testable with small fakes and avoids a second control
    path in the production coordinator.
    """

    def __init__(
        self,
        hass: object,
        coordinator: object,
        *,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
        scheduler: Callable[[float, Callable[[], object]], object] | None = None,
        actuator: object | None = None,
        envelope_verifier: Callable[..., object] | None = None,
        envelope_provider: CommissionedEnvelopeEvidenceProvider | object | None = None,
        capability_resolutions: Sequence[CapabilityResolutionRecord] = (),
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.enabled = bool(enabled)
        self.clock = clock or _now
        self._scheduler = scheduler
        self.actuator = actuator
        # ``envelope_verifier`` is retained as a compatibility injection point,
        # but its output is accepted only as typed provider evidence.  The
        # validate service's envelope field is never passed to it.
        self.envelope_verifier = envelope_verifier
        self.envelope_provider = envelope_provider if envelope_provider is not None else CommissionedEnvelopeEvidenceProvider()
        self.capability_resolutions = tuple(capability_resolutions)
        self._lock = asyncio.Lock()
        self._session: CommissioningSession | None = None
        self._deadline_handle: object | None = None
        self._expiry_task: asyncio.Task[object] | None = None
        self.last_fail_safe_result: object | None = None
        self.last_fail_safe_proof: object | None = None
        self._cleanup: CleanupObligation | None = None
        self._cleanup_task: asyncio.Task[object] | None = None
        self._cleanup_io_task: asyncio.Task[object] | None = None
        self._cleanup_generation = 0
        self._cleanup_pre_revisions: dict[str, str] = {}
        self._last_cleanup_deadline: datetime | None = None
        self._started = False
        self._draining_expiry = False
        self._unsub_guard: Callable[[], object] | None = None

    @property
    def session(self) -> CommissioningSession | None:
        return self._session

    @property
    def started(self) -> bool:
        return self._started

    @property
    def cleanup_obligation(self) -> CleanupObligation | None:
        """The persistent cleanup obligation, even when no session exists."""

        return self._cleanup

    @property
    def _cleanup_obligation(self) -> CleanupObligation | None:
        """Compatibility view for older diagnostics; never a second owner."""

        return self._cleanup

    @property
    def cleanup_pending(self) -> bool:
        return self._cleanup is not None and not self._cleanup.safe

    def _current_time(self) -> datetime:
        return _aware(self.clock(), "workflow clock")

    def _snapshot(self) -> object | None:
        return getattr(self.coordinator, "data", None)

    def _actuator(self) -> object | None:
        if self.actuator is not None:
            return self.actuator
        return getattr(self.coordinator, "policy_actuator", None) or getattr(self.coordinator, "_policy", None)

    def _fingerprints(self) -> tuple[str | None, str | None]:
        actuator = self._actuator()
        try:
            # Recompute from the exact T0003/T0007 definitions whenever the
            # shared production actuator exposes its config.  Properties are
            # a fallback solely for small offline doubles.
            config = getattr(actuator, "config", None)
            mapping = mapping_fingerprint(config) if config is not None else getattr(actuator, "mapping_fingerprint", None)
            policy = policy_fingerprint() if config is not None else getattr(actuator, "policy_fingerprint", None)
            return (mapping if isinstance(mapping, str) else None, policy if isinstance(policy, str) else None)
        except Exception:
            return None, None

    def _blocked(self, issues: Sequence[str], *, status: CommissioningStatus = CommissioningStatus.BLOCKED) -> dict[str, object]:
        mapping, policy = self._fingerprints()
        return self._response(status, None, mapping, policy, tuple(issues))

    def _response(
        self,
        status: CommissioningStatus,
        session_id: str | None,
        mapping: str | None,
        policy: str | None,
        issues: tuple[str, ...] = (),
        *,
        checklist: Mapping[str, object] | None = None,
        evidence: Mapping[str, object] | None = None,
        snippet: str | None = None,
    ) -> dict[str, object]:
        return {
            "status": status.value,
            "session_id": session_id,
            "mapping_fingerprint": mapping,
            "policy_fingerprint": policy,
            "checklist": dict(sorted((checklist or {}).items())),
            "issues": list(issues),
            "evidence_summary": _jsonable(evidence or {}),
            "iac_snippet": snippet,
        }

    def _fail_safe_evidence(self) -> dict[str, object]:
        return {
            "fail_safe_result": self.last_fail_safe_result,
            "fail_safe_proof_local": self.last_fail_safe_proof,
            "fail_safe_proof": self.last_fail_safe_proof,
            "cleanup_obligation": self._cleanup,
        }

    def _ordered_cleanup_issues(self, issues: Sequence[str]) -> tuple[str, ...]:
        """Keep terminal cause first, then exact cleanup diagnostics order."""

        return tuple(issues) + tuple(self._cleanup.issues if self._cleanup is not None else ())

    def _cleanup_status(self, result: object | None = None) -> CommissioningStatus:
        return CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(result or self.last_fail_safe_result) else CommissioningStatus.FAILED_UNSAFE

    async def async_start(self) -> None:
        # A reload must not leave a previous timer/listener alive.  Runtime
        # sessions are intentionally discarded; an unresolved cleanup remains
        # and is retried before any new begin.
        if self._started:
            return
        self._started = True
        try:
            getter = getattr(self.coordinator, "async_get_policy_actuator", None)
            if callable(getter):
                result = getter()
                self.actuator = await result if inspect.isawaitable(result) else result
        except asyncio.CancelledError:
            self._started = False
            await self._cancel_cleanup()
            raise
        except Exception:
            self._started = False
            if self._cleanup is not None:
                await self._ensure_cleanup("start_unexpected_exception")
            raise
        # Sessions are never restored.  Guard observation is best-effort and
        # remains safe if a test double does not expose HA event tracking.
        try:
            from homeassistant.helpers.event import async_track_state_change_event

            entity_id = getattr(getattr(self.coordinator, "config", None), "control_disable_guard_entity_id", "input_boolean.house_battery_control_disable")
            self._unsub_guard = async_track_state_change_event(
                self.hass,
                [entity_id, "sensor.house_battery_control_health"],
                self._async_guard_changed,
            )
        except Exception:
            self._unsub_guard = None

    async def _async_guard_changed(self, event: object) -> None:
        data = getattr(event, "data", event)
        if not isinstance(data, Mapping):
            await self.async_handle_lifecycle()
            return
        entity_id = data.get("entity_id")
        if entity_id != self._guard_entity():
            await self.async_handle_lifecycle()
            return
        new_state = data.get("new_state")
        value = new_state.get("state") if isinstance(new_state, Mapping) else getattr(new_state, "state", None)
        if value != "off":
            async with self._lock:
                await self._invalidate("guard_event_not_exactly_off", invoke_fail_safe=True)
            return
        await self.async_handle_lifecycle()

    async def async_stop(self) -> None:
        unsub = self._unsub_guard
        async def shutdown_locked() -> None:
            async with self._lock:
                self._cancel_deadline()
                self._draining_expiry = True
                await self._drain_expiry_task()
                await self._invalidate("shutdown", invoke_fail_safe=self._session is not None or self._cleanup is not None)
        task = asyncio.create_task(shutdown_locked())
        def shutdown_done(done: asyncio.Task[object]) -> None:
            if self._cleanup_task is done:
                self._cleanup_task = None
            self._started = (
                (self._cleanup_io_task is not None and not self._cleanup_io_task.done())
                or (self._expiry_task is not None and not self._expiry_task.done())
            )
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass
            if self._started is False and self._unsub_guard is not None:
                callback_unsub = self._unsub_guard
                self._unsub_guard = None
                try:
                    callback_unsub()
                except Exception:
                    pass
        task.add_done_callback(shutdown_done)
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.1)
        except asyncio.CancelledError:
            if not task.done():
                self._cleanup_task = task
            else:
                await self._cancel_cleanup()
            raise
        except asyncio.TimeoutError:
            self._cleanup_task = task
            return
        except Exception:
            if not task.done():
                self._cleanup_task = task
            raise
        finally:
            self._draining_expiry = False
            self._started = (
                (self._cleanup_task is not None and not self._cleanup_task.done())
                or (self._cleanup_io_task is not None and not self._cleanup_io_task.done())
                or (self._expiry_task is not None and not self._expiry_task.done())
            )
            self._cancel_deadline()
            if task.done() and unsub is not None and self._unsub_guard is unsub:
                try:
                    unsub()
                finally:
                    self._unsub_guard = None

    def _coordinator_ready(self) -> tuple[bool, tuple[str, ...]]:
        if not self._started:
            return False, ("coordinator_not_started",)
        snapshot = self._snapshot()
        if snapshot is None:
            return False, ("coordinator_not_started",)
        health = getattr(snapshot, "health", None)
        if health is not ControllerHealth.HEALTHY and getattr(health, "value", health) != ControllerHealth.HEALTHY.value:
            return False, ("coordinator_not_healthy",)
        if getattr(snapshot, "fail_safe_obligation", True) or getattr(snapshot, "fail_safe_pending", True):
            return False, ("fail_safe_obligation_or_pending",)
        if getattr(snapshot, "guard_quality", "invalid") != "valid" or getattr(snapshot, "guard_state", None) != "off":
            return False, ("guard_not_exactly_off",)
        solis = getattr(snapshot, "solis", None)
        if solis is None or not getattr(solis, "is_healthy", False) or getattr(solis, "snapshot", None) is None:
            return False, ("complete_solis_telemetry_required",)
        telemetry = getattr(getattr(solis, "snapshot", None), "telemetry", None)
        if telemetry is None or not isinstance(getattr(telemetry, "device_timestamp", None), datetime) or getattr(telemetry, "battery_power_kw", None) is None:
            return False, ("complete_t0004_telemetry_required",)
        try:
            observed_at = _aware(getattr(solis.snapshot, "observed_at", None), "snapshot observed_at")
            if observed_at > self._current_time() or self._current_time() - observed_at > MAXIMUM_TELEMETRY_AGE:
                return False, ("telemetry_is_stale_or_future",)
            power = _decimal(getattr(telemetry, "battery_power_kw"), "battery_power_kw")
            soc = _decimal(getattr(telemetry, "state_of_charge_percent"), "state_of_charge_percent")
            if not Decimal("0") <= soc <= Decimal("100") or not power.is_finite():
                return False, ("telemetry_values_invalid",)
            inverter_time = _aware(getattr(getattr(solis, "snapshot", None).persistent, "inverter_time", None), "inverter_time")
            if abs(self._current_time() - inverter_time) > MAXIMUM_INVERTER_CLOCK_SKEW:
                return False, ("inverter_clock_out_of_skew",)
        except (AttributeError, TypeError, ValueError):
            return False, ("complete_t0004_telemetry_required",)
        return True, ()

    @staticmethod
    def _snapshot_values(snapshot: object) -> tuple[object, ...]:
        solis = getattr(snapshot, "solis", None)
        state = getattr(solis, "snapshot", None)
        if state is None:
            return ()
        return (state, getattr(state, "telemetry", None), getattr(state, "persistent", None), getattr(state, "slots", ()))

    def _slots_off(self, state: object) -> bool:
        slots = getattr(state, "slots", ())
        if not isinstance(slots, Sequence) or len(slots) != 6:
            return False
        return all(not getattr(direction, "enabled", True) for slot in slots for direction in (getattr(slot, "charge", None), getattr(slot, "discharge", None)))

    @staticmethod
    def _global_capabilities(state: object) -> dict[str, object]:
        capabilities = getattr(state, "capabilities", None)
        if capabilities is None:
            return {}
        return {
            name: getattr(capabilities, name, None)
            for name in (
                "maximum_charge_current", "maximum_discharge_current",
                "maximum_output_power", "maximum_feed_in_power",
            )
        }

    @staticmethod
    def _inverter_identity(state: object) -> str | None:
        persistent = getattr(state, "persistent", None)
        for name in ("inverter_identity", "inverter_id", "serial_number", "device_id"):
            value = getattr(persistent, name, None)
            if isinstance(value, str) and value:
                return value
        return None

    def _current_inverter_identity(self, state: object) -> str | None:
        identity = self._inverter_identity(state)
        if identity is not None:
            return identity
        actuator = self._actuator()
        for source in (actuator, getattr(actuator, "config", None)):
            for name in ("inverter_identity", "inverter_id", "serial_number", "device_id"):
                value = getattr(source, name, None)
                if isinstance(value, str) and value:
                    return value
        return None

    def _attested_manual_grid(self, data: Mapping[str, object], mapping: str, policy: str, now: datetime) -> bool:
        if data.get("operator_attestation") is not True or data.get("manual_grid_setting_verified") is not True:
            return False
        try:
            value = _decimal(data.get("manual_grid_import_power_kw"), "manual_grid_import_power_kw")
            verified_at = data.get("manual_grid_verified_at", now)
            if isinstance(verified_at, str):
                verified_at = datetime.fromisoformat(verified_at)
            verified_at = _aware(verified_at, "manual_grid_verified_at")
            return value == MAXIMUM_GRID_IMPORT_POWER_KW and verified_at <= now and data.get("manual_grid_mapping_fingerprint") == mapping and data.get("manual_grid_policy_fingerprint") == policy
        except (TypeError, ValueError):
            return False

    def _guard_entity(self) -> str:
        return getattr(getattr(self.coordinator, "config", None), "control_disable_guard_entity_id", "input_boolean.house_battery_control_disable")

    async def _assert_guard(self) -> frozenset[str]:
        services = getattr(self.hass, "services", None)
        call = getattr(services, "async_call", None)
        changed: set[str] = set()
        if callable(call):
            entity_id = self._guard_entity()
            before = None
            prior_revision = None
            states = getattr(self.hass, "states", None)
            if states is not None and hasattr(states, "get"):
                before_state = states.get(entity_id)
                before = getattr(before_state, "state", None)
                prior_context = getattr(before_state, "context", None)
                prior_revision = None if before_state is None else (getattr(prior_context, "id", None) or getattr(before_state, "context_id", None))
            domain, _ = entity_id.split(".", 1)
            result = call(domain, "turn_on", {"entity_id": entity_id}, blocking=True)
            if inspect.isawaitable(result):
                await result
            if states is not None and hasattr(states, "get"):
                guard = states.get(entity_id)
                if getattr(guard, "state", None) != "on":
                    raise RuntimeError("control-disable guard was not proven on")
                if before != "on":
                    context = getattr(guard, "context", None)
                    revision = getattr(context, "id", None) or getattr(guard, "context_id", None)
                    if not isinstance(revision, str) or not revision or revision == prior_revision:
                        raise RuntimeError("guard turn_on did not produce an advanced state revision")
                    changed.add(entity_id)
        return frozenset(changed)

    def _guard_on(self) -> bool:
        states = getattr(self.hass, "states", None)
        if states is None or not hasattr(states, "get"):
            # Offline doubles without a state registry are handled by the
            # typed actuator result; production HA always has this registry.
            return True
        return getattr(states.get(self._guard_entity()), "state", None) == "on"

    def _configured_safe_entities(self) -> tuple[tuple[str, str], ...]:
        actuator = self._actuator()
        config = getattr(actuator, "config", None)
        solis = config if config is not None else None
        if solis is None:
            return ()
        entities: list[tuple[str, str]] = []
        try:
            for slot in solis.slots:
                for direction in (slot.charge, slot.discharge):
                    entities.append((direction.enable_entity_id, "off"))
            entities.extend(
                (
                    (solis.persistent.storage_mode_entity_id, StorageMode.SELF_USE.value),
                    (solis.persistent.grid_peak_shaving_entity_id, "on"),
                    (solis.protection.battery_reserve_entity_id, "off"),
                )
            )
        except Exception:
            return ()
        return tuple(entities)

    def _capture_cleanup_revisions(self) -> dict[str, str]:
        states_api = getattr(self.hass, "states", None)
        expected = ((self._guard_entity(), "on"),) + self._configured_safe_entities()
        if states_api is None or not hasattr(states_api, "get"):
            return {}
        revisions: dict[str, str] = {}
        for entity_id, _ in expected:
            state = states_api.get(entity_id)
            context = getattr(state, "context", None)
            revision = getattr(context, "id", None) or getattr(state, "context_id", None)
            if isinstance(revision, str) and revision:
                revisions[entity_id] = revision
        return revisions

    @staticmethod
    def _coerce_actuator_proof(value: object, attempt_started_at: datetime, changed_entities: frozenset[str] = frozenset()) -> CleanupProof | None:
        if type(value) is CleanupProof:
            if value.observed_at <= attempt_started_at:
                return None
            for item in value.states:
                if not isinstance(item.revision, str) or not item.revision:
                    return None
                if item.entity_id in changed_entities and (item.state_updated_at or item.observed_at) is None:
                    return None
                if item.entity_id in changed_entities and (item.state_updated_at or item.observed_at) <= attempt_started_at:
                    return None
            return value
        if value is None or not hasattr(value, "states"):
            return None
        observed_at = getattr(value, "observed_at", None)
        if not isinstance(observed_at, datetime):
            return None
        states: list[CleanupStateEvidence] = []
        for item in getattr(value, "states", ()):
            entity_id = getattr(item, "entity_id", None)
            expected = getattr(item, "expected_state", None)
            if not isinstance(entity_id, str) or not isinstance(expected, str):
                return None
            updated = getattr(item, "last_updated", None) or getattr(item, "observed_at", None)
            revision = getattr(item, "revision", None) or getattr(item, "context_id", None)
            valid_updated = isinstance(updated, datetime) and (entity_id not in changed_entities or updated > attempt_started_at)
            valid_revision = isinstance(revision, str) and bool(revision)
            states.append(CleanupStateEvidence(entity_id, expected, getattr(item, "observed_state", None), getattr(item, "observed_at", None) if isinstance(getattr(item, "observed_at", None), datetime) else observed_at, revision if valid_revision else None, bool(getattr(item, "matches", False)) and valid_updated and valid_revision, updated if isinstance(updated, datetime) else None))
        complete = bool(getattr(value, "complete", False))
        safe = bool(getattr(value, "ha_safe", False))
        return CleanupProof(observed_at, attempt_started_at, tuple(states), complete, safe, source="shared-actuator")

    def _fresh_cleanup_proof(self, attempt_started_at: datetime, changed_entities: frozenset[str] = frozenset(), pre_revisions: Mapping[str, str] | None = None) -> CleanupProof:
        now = self._current_time()
        pre_revisions = pre_revisions or {}
        if type(self.last_fail_safe_proof) is CleanupProof and self.last_fail_safe_proof.source == "shared-actuator":
            current = self._coerce_actuator_proof(self.last_fail_safe_proof, attempt_started_at, changed_entities)
            if current is not None:
                return current
        actuator = self._actuator()
        for name in ("last_fail_safe_proof", "fail_safe_proof"):
            provided = self._coerce_actuator_proof(getattr(actuator, name, None), attempt_started_at, changed_entities)
            if provided is not None:
                return provided
        states_api = getattr(self.hass, "states", None)
        expected = ((self._guard_entity(), "on"),) + self._configured_safe_entities()
        evidence: list[CleanupStateEvidence] = []
        if states_api is not None and hasattr(states_api, "get") and expected:
            for entity_id, expected_state in expected:
                state = states_api.get(entity_id)
                observed = getattr(state, "state", None)
                updated = getattr(state, "last_updated", None)
                context = getattr(state, "context", None)
                revision = getattr(context, "id", None)
                if revision is None:
                    revision = getattr(state, "context_id", None)
                valid_time = isinstance(updated, datetime) and updated.tzinfo is not None and updated.utcoffset() is not None
                valid_revision = isinstance(revision, str) and bool(revision)
                requires_mutation = entity_id in changed_entities
                stable_revision = entity_id in pre_revisions and revision == pre_revisions[entity_id]
                changed_revision = entity_id in pre_revisions and revision != pre_revisions[entity_id]
                matches = observed == expected_state and valid_time and valid_revision and (
                    (requires_mutation and updated > attempt_started_at and changed_revision)
                    or (not requires_mutation and (not pre_revisions or stable_revision))
                )
                evidence.append(CleanupStateEvidence(entity_id, expected_state, observed if isinstance(observed, str) else None, updated if valid_time else None, revision if valid_revision else None, matches, updated if valid_time else None))
            complete = len(evidence) == 16 and all(item.observed_state is not None and item.observed_at is not None and item.revision is not None for item in evidence)
            safe = complete and all(item.matches for item in evidence)
            return CleanupProof(now, attempt_started_at, tuple(evidence), complete, safe)
        return CleanupProof(now, attempt_started_at, (), False, False)

    async def _invoke_fail_safe(self, deadline: datetime, attempt_started_at: datetime) -> tuple[PolicyActuationResult | None, frozenset[str], CleanupProof | None]:
        actuator = self._actuator()
        fail_safe = getattr(actuator, "async_apply_fail_safe", None)
        if callable(fail_safe):
            try:
                result = fail_safe(deadline=deadline)
            except TypeError:
                # A no-argument fake is accepted only with a bounded wait; the
                # production public actuator always receives the deadline.
                result = fail_safe()
            if inspect.isawaitable(result):
                result = await self._bounded_cleanup_await(result, deadline)
                if result is None:
                    return PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE, issues=("cleanup_deadline_exhausted",)), frozenset(), None
            if type(result) is not PolicyActuationResult:
                return None, frozenset(), None
            changed_entities = frozenset(
                item.entity_id
                for item in result.results
                if getattr(getattr(item, "outcome", None), "value", getattr(item, "outcome", None)) == "applied_ha_readback"
            )
            proof_getter = getattr(actuator, "async_fresh_fail_safe_proof", None) or getattr(actuator, "fresh_fail_safe_proof", None)
            if callable(proof_getter):
                try:
                    proof_value = proof_getter(attempt_started_at=attempt_started_at)
                except TypeError:
                    proof_value = proof_getter(attempt_started_at)
                if inspect.isawaitable(proof_value):
                    proof_value = await self._bounded_cleanup_await(proof_value, deadline)
                    if proof_value is None:
                        return result, changed_entities, None
                proof = self._coerce_actuator_proof(proof_value, attempt_started_at, changed_entities)
            else:
                proof = None
            return result, changed_entities, proof
        return None, frozenset(), None

    async def _bounded_cleanup_await(self, awaitable: object, deadline: datetime) -> object | None:
        """Bound cleanup I/O while retaining a cancellation-resistant child."""

        task = asyncio.ensure_future(awaitable)
        self._cleanup_io_task = task

        def done(done_task: asyncio.Task[object]) -> None:
            if self._cleanup_io_task is done_task:
                self._cleanup_io_task = None
            try:
                done_task.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done)
        remaining = max(0.0, (deadline - self._current_time()).total_seconds())
        if remaining <= 0:
            task.cancel()
            return None
        finished, _ = await asyncio.wait({task}, timeout=remaining)
        if not finished:
            task.cancel()
            return None
        return task.result()

    def _fail_safe_proven(self, result: object | None) -> bool:
        obligation = self._cleanup
        proof = self.last_fail_safe_proof
        upper_bound = obligation.deadline if obligation is not None else (self._last_cleanup_deadline or self._current_time())
        entity_ids = {item.entity_id for item in proof.states} if type(proof) is CleanupProof else set()
        configured = {self._guard_entity()} | {entity_id for entity_id, _ in self._configured_safe_entities()}
        if len(configured) == 16:
            exact_entities = entity_ids == configured
        else:
            # Offline doubles must still name the same semantic 16-state
            # proof; arbitrary "safe" booleans or coordinator snapshots are
            # not accepted as a substitute.
            non_slots = entity_ids - {item for item in entity_ids if "slot" in item}
            exact_entities = (
                (self._guard_entity() in entity_ids or any("disable" in item or "guard" in item for item in non_slots))
                and len(entity_ids) == 16
                and len([item for item in entity_ids if "slot" in item]) == 12
                and len(non_slots) == 4
                and any("storage" in item or "mode" in item for item in non_slots)
                and any("peak" in item for item in non_slots)
                and any("reserve" in item for item in non_slots)
            )
        return bool(
            type(result) is PolicyActuationResult
            and result.status == PolicyActuationStatus.FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION
            and type(proof) is CleanupProof
            and proof.complete
            and proof.ha_safe
            and proof.post_attempt
            and proof.observed_at <= upper_bound
            and len(proof.states) == 16
            and exact_entities
            and proof.all_slots_off
            and all(item.observed_at is not None and item.observed_at <= upper_bound for item in proof.states)
            and all(item.matches for item in proof.states)
            and (obligation is None or obligation.result is result)
        )

    async def _cleanup_once(self, reason: str, *, deadline: datetime | None = None) -> PolicyActuationResult | None:
        now = self._current_time()
        obligation = self._cleanup
        if obligation is None:
            self._cleanup_generation += 1
            obligation = CleanupObligation(reason, deadline or now + CLEANUP_DEADLINE, generation=self._cleanup_generation)
            self._cleanup = obligation
            self._last_cleanup_deadline = obligation.deadline
        elif deadline is not None and deadline < obligation.deadline:
            # Never lengthen or reset an already active cleanup deadline.
            obligation.deadline = deadline
        self._cleanup_generation += 1
        obligation.generation = self._cleanup_generation
        generation = obligation.generation
        if obligation.safe and self._fail_safe_proven(obligation.result):
            return obligation.result
        attempt_started_at = self._current_time()
        obligation.attempt_started_at = attempt_started_at
        self._cleanup_pre_revisions = self._capture_cleanup_revisions()
        self.last_fail_safe_proof = None
        try:
            if self._current_time() >= obligation.deadline:
                obligation.issues.append("cleanup_deadline_exhausted")
                obligation.result = PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE, issues=("cleanup_deadline_exhausted",))
                obligation.proof = self._fresh_cleanup_proof(attempt_started_at, pre_revisions=self._cleanup_pre_revisions)
                self.last_fail_safe_result = obligation.result
                self.last_fail_safe_proof = obligation.proof
                return obligation.result
            guard_changed = frozenset()
            remaining = max(0.0, (obligation.deadline - self._current_time()).total_seconds())
            if remaining <= 0:
                obligation.issues.append("cleanup_deadline_exhausted_before_guard")
            else:
                try:
                    guard_changed = await asyncio.wait_for(self._assert_guard(), remaining)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Guard failure is recorded, but does not skip the
                    # bounded T0007 fail-safe attempt.
                    obligation.issues.append(f"guard_assertion_error:{type(exc).__name__}")
            if not self._guard_on():
                obligation.issues.append("guard_not_proven_on")
            result, changed_entities, actuator_proof = await self._invoke_fail_safe(obligation.deadline, attempt_started_at)
            changed_entities = changed_entities | guard_changed
            if self._cleanup is not obligation or obligation.generation != generation:
                return obligation.result
            if result is None:
                obligation.issues.append("typed_fail_safe_result_missing")
            obligation.result = result if type(result) is PolicyActuationResult else PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE, issues=("typed_fail_safe_result_missing",))
            # Always capture a proof after this exact attempt, even when the
            # actuator reports an error. Missing/stale proof is unsafe.
            obligation.proof = actuator_proof or self._fresh_cleanup_proof(attempt_started_at, changed_entities, self._cleanup_pre_revisions)
            self.last_fail_safe_result = obligation.result
            self.last_fail_safe_proof = obligation.proof
            if self._current_time() >= obligation.deadline:
                obligation.issues.append("cleanup_deadline_exhausted")
                obligation.result = PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE, issues=("cleanup_deadline_exhausted",))
            if obligation.proof is None or not obligation.proof.post_attempt:
                obligation.issues.append("post_attempt_proof_missing_or_stale")
            if not obligation.proof.complete:
                obligation.issues.append("post_attempt_proof_incomplete")
            if not obligation.proof.ha_safe:
                obligation.issues.append("post_attempt_all_directions_not_off")
            if obligation.result.status != PolicyActuationStatus.FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION:
                obligation.issues.extend(item for item in obligation.result.issues if item not in obligation.issues)
            obligation.completed_at = self._current_time()
            if self._fail_safe_proven(obligation.result):
                self._cleanup = None
            return obligation.result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._cleanup is not obligation or obligation.generation != generation:
                return obligation.result
            obligation.issues.append(f"cleanup_exception:{type(exc).__name__}")
            obligation.result = PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE, issues=(f"cleanup_exception:{type(exc).__name__}",))
            try:
                obligation.proof = self._fresh_cleanup_proof(attempt_started_at, pre_revisions=self._cleanup_pre_revisions)
            except Exception:
                obligation.proof = None
            obligation.completed_at = self._current_time()
            self.last_fail_safe_result = obligation.result
            self.last_fail_safe_proof = obligation.proof
            return obligation.result

    async def _ensure_cleanup(self, reason: str) -> PolicyActuationResult | None:
        """Run or retry the one persistent obligation while serialized."""

        if self._cleanup_io_task is not None and self._cleanup_io_task is not asyncio.current_task():
            task = self._cleanup_io_task
            deadline = self._cleanup.deadline if self._cleanup is not None else self._current_time()
            remaining = max(0.0, (deadline - self._current_time()).total_seconds())
            finished, _ = await asyncio.wait({task}, timeout=remaining)
            if not finished:
                if self._cleanup is not None and self._cleanup.result is None:
                    self._cleanup.issues.append("cleanup_deadline_exhausted")
                    self._cleanup.result = PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE, issues=("cleanup_deadline_exhausted",))
                return self._cleanup.result if self._cleanup is not None else self.last_fail_safe_result
            if self._cleanup_io_task is task:
                self._cleanup_io_task = None

        if self._cleanup_task is not None and self._cleanup_task is not asyncio.current_task():
            task = self._cleanup_task
            deadline = self._cleanup.deadline if self._cleanup is not None else self._current_time() + CLEANUP_DEADLINE
            remaining = max(0.0, (deadline - self._current_time()).total_seconds())
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, Exception):
                if self._cleanup is not None and self._cleanup.result is None:
                    self._cleanup_generation += 1
                    self._cleanup.generation = self._cleanup_generation
                    self._cleanup.issues.append("cleanup_deadline_exhausted")
                    self._cleanup.result = PolicyActuationResult(PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE, issues=("cleanup_deadline_exhausted",))
                return self._cleanup.result if self._cleanup is not None else self.last_fail_safe_result
            if task.done() and self._cleanup_task is task:
                self._cleanup_task = None
            if self._cleanup is None:
                return self.last_fail_safe_result
        return await self._cleanup_once(reason)

    async def _invalidate(self, issue: str, *, invoke_fail_safe: bool) -> object | None:
        self._cancel_deadline()
        self._session = None
        if not invoke_fail_safe:
            return None
        return await self._ensure_cleanup(issue)

    @staticmethod
    def _capability_fingerprint(records: Sequence[CapabilityResolutionRecord], state: object | None = None) -> str:
        payload: object = [_jsonable(record) for record in records]
        if state is not None:
            payload = {"verified_resolutions": payload, "current_metadata": CommissioningWorkflow._capture_capabilities(state)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _manual_fingerprint(record: ManualGridImportVerification) -> str:
        return hashlib.sha256(json.dumps(_jsonable(record), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _parse_capability_records(value: object, *, now: datetime, mapping: str, policy: str) -> tuple[CapabilityResolutionRecord, ...]:
        if value is None:
            return ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("capability_resolutions must be a sequence")
        result: list[CapabilityResolutionRecord] = []
        from .contracts import DocumentedUnlimitedValue
        for item in value:
            if isinstance(item, CapabilityResolutionRecord):
                record = item
            elif isinstance(item, Mapping):
                expected = {"entity_id", "observed_unit", "verified_target", "verified_at", "mapping_fingerprint", "policy_fingerprint", "evidence_classification", "documented_unlimited_target"}
                if set(item) != expected:
                    raise ValueError("capability resolution contains missing or unknown keys")
                verified_at = datetime.fromisoformat(item["verified_at"]) if isinstance(item["verified_at"], str) else item["verified_at"]
                target = DocumentedUnlimitedValue() if item["verified_target"] == "documented_unlimited" else _decimal(item["verified_target"], "verified_target")
                unlimited = item["documented_unlimited_target"]
                record = CapabilityResolutionRecord(_text(item["entity_id"], "entity_id"), _text(item["observed_unit"], "observed_unit"), target, _aware(verified_at, "verified_at"), _fingerprint(item["mapping_fingerprint"], "mapping_fingerprint"), _fingerprint(item["policy_fingerprint"], "policy_fingerprint"), _text(item["evidence_classification"], "evidence_classification"), None if unlimited is None else _decimal(unlimited, "documented_unlimited_target"))
            else:
                raise ValueError("capability resolution must be typed evidence")
            if not record.valid(now=now, mapping=mapping, policy=policy, unit=record.observed_unit, entity_id=record.entity_id):
                raise ValueError(f"capability resolution is stale or fingerprint-mismatched: {record.entity_id}")
            result.append(record)
        return tuple(result)

    async def _verify_envelope(
        self,
        supplied: object,
        *,
        session: CommissioningSession,
        state: object,
        now: datetime,
        mapping: str,
        policy: str,
        capability_resolutions: tuple[CapabilityResolutionRecord, ...] = (),
    ) -> CommissionedPowerEnvelope | None:
        if supplied is not None and self.envelope_provider is None and self.envelope_verifier is None:
            raise ValueError("service envelope payload is not authority")
        device_timestamp = getattr(getattr(state, "telemetry", None), "device_timestamp", None)
        observed_at = getattr(state, "observed_at", None)
        if session.before_device_timestamp is None or not isinstance(device_timestamp, datetime) or device_timestamp <= session.before_device_timestamp:
            if supplied is not None:
                raise ValueError("independent envelope evidence lacks an advanced device revision")
            return None
        if not isinstance(observed_at, datetime) or observed_at < session.candidate_applied_at or observed_at > min(now, session.deadline):
            if supplied is not None:
                raise ValueError("independent envelope observation time is outside the active window")
            return None
        if not session.manual_grid_verification.valid(now=now, mapping=mapping, policy=policy):
            raise ValueError("verified power envelope lacks current manual-grid authority")
        manual_fp = self._manual_fingerprint(session.manual_grid_verification)
        capability_fp = self._capability_fingerprint(capability_resolutions, state=state)
        result: object | None = None
        provider = self.envelope_provider
        try:
            method = None if self.envelope_verifier is not None else (getattr(provider, "async_provide", None) or getattr(provider, "provide", None) or getattr(provider, "async_collect", None) or getattr(provider, "collect", None) or getattr(provider, "async_get", None) or getattr(provider, "get", None) or (provider if callable(provider) else None))
            if callable(method):
                result = method(session=session, state=state, observed_device_timestamp=device_timestamp, observed_at=observed_at, now=now, mapping_fingerprint=mapping, policy_fingerprint=policy, manual_grid_fingerprint=manual_fp, capability_fingerprint=capability_fp, capability_resolutions=capability_resolutions)
                if inspect.isawaitable(result):
                    result = await result
            elif self.envelope_verifier is not None:
                result = self.envelope_verifier(session=session, state=state, before_device_timestamp=session.before_device_timestamp, manual_grid_verification=session.manual_grid_verification, now=now, mapping_fingerprint=mapping, policy_fingerprint=policy, manual_grid_fingerprint=manual_fp, capability_fingerprint=capability_fp, capability_resolutions=capability_resolutions)
                if inspect.isawaitable(result):
                    result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ValueError(f"envelope_provider_error:{type(exc).__name__}") from exc
        if result is None:
            if supplied is not None:
                raise ValueError("independent envelope evidence is absent")
            return None
        if isinstance(result, CommissionedEnvelopeEvidenceRecord):
            result = result.envelope
        if type(result) is not CommissionedPowerEnvelope:
            raise ValueError("envelope provider did not return typed evidence")
        if not self._capability_metadata_complete(state):
            raise ValueError("complete current global, protection, and all-slot capability metadata is required")
        current_identity = session.inverter_identity or self._inverter_identity(state)
        if current_identity is not None and result.inverter_identity != current_identity:
            raise ValueError("verified power envelope inverter identity mismatch")
        if result.mapping_fingerprint != mapping or result.candidate_policy_fingerprint != policy:
            raise ValueError("verified power envelope authority fingerprint mismatch")
        if result.manual_grid_fingerprint != manual_fp or result.capability_fingerprint != capability_fp:
            raise ValueError("verified power envelope evidence fingerprint mismatch")
        if result.validated_at != observed_at or result.validated_at < session.candidate_applied_at or result.validated_at > min(now, session.deadline):
            raise ValueError("power envelope evidence time is outside the active observation")
        if result.schema_version != str(COMMISSIONING_SCHEMA_VERSION) or not result.inverter_identity or not result.evidence_source:
            raise ValueError("power envelope schema, inverter identity, and evidence source are invalid")
        if result.maximum_grid_import_power_kw != MAXIMUM_GRID_IMPORT_POWER_KW or result.maximum_charge_power_kw <= 0 or result.maximum_discharge_power_kw <= 0:
            raise ValueError("power envelope AC boundary evidence is invalid")
        return result

    def _cancel_deadline(self) -> None:
        handle = self._deadline_handle
        self._deadline_handle = None
        cancel = getattr(handle, "cancel", None)
        if callable(cancel):
            cancel()

    async def _drain_expiry_task(self) -> None:
        task = self._expiry_task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.1)
        except asyncio.CancelledError:
            # Never consume the caller's cancellation.  The task remains
            # owned by the workflow and its done callback will clear it.
            if not task.done():
                self._expiry_task = task
            raise
        except (asyncio.TimeoutError, Exception):
            # A scheduler callback is never allowed to hold shutdown hostage.
            # Keep the task reference until its done callback consumes it.
            if not task.done():
                self._expiry_task = task
                return
        if self._expiry_task is task:
            self._expiry_task = None

    def _schedule_deadline(self, deadline: datetime) -> None:
        delay = max(0.0, (deadline - self._current_time()).total_seconds())
        def callback() -> None:
            try:
                task = asyncio.create_task(self.async_expire())
            except Exception:
                # A scheduler failure is terminal and must create the same
                # persistent cleanup obligation as every other error path.
                task = asyncio.create_task(self._scheduler_failure())
            self._expiry_task = task
            task.add_done_callback(self._expiry_done)
        if self._scheduler is not None:
            self._deadline_handle = self._scheduler(delay, callback)
        else:
            self._deadline_handle = asyncio.get_running_loop().call_later(delay, callback)

    def _expiry_done(self, task: asyncio.Task[object]) -> None:
        if self._expiry_task is task:
            self._expiry_task = None
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            # The public expiry boundary catches and records all unexpected
            # exceptions; consuming here prevents event-loop warnings.
            pass

    async def _scheduler_failure(self) -> dict[str, object]:
        async with self._lock:
            session_id = None if self._session is None else self._session.session_id
            await self._invalidate("expiry_scheduler_error", invoke_fail_safe=True)
            mapping, policy = self._fingerprints()
            status = self._cleanup_status()
            return self._response(status, session_id, mapping, policy, ("expiry_scheduler_error",), evidence=self._fail_safe_evidence())

    async def async_expire(self) -> dict[str, object]:
        try:
            async with self._lock:
                if self._session is None:
                    if self._cleanup is None:
                        return self._blocked(("no_pending_session",), status=CommissioningStatus.ABORTED)
                    await self._ensure_cleanup("expiry_without_session")
                    mapping, policy = self._fingerprints()
                    status = self._cleanup_status()
                    return self._response(status, None, mapping, policy, ("expiry_without_session",), evidence=self._fail_safe_evidence())
                session = self._session
                safe_result = await self._invalidate("observation_window_expired", invoke_fail_safe=True)
                mapping, policy = self._fingerprints()
                status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(status, session.session_id, mapping, policy, ("observation_window_expired",) + tuple(self._cleanup.issues if self._cleanup else ()), evidence=self._fail_safe_evidence())
        except asyncio.CancelledError:
            if self._draining_expiry:
                raise
            await self._cancel_cleanup()
            raise
        except Exception as exc:
            async with self._lock:
                await self._invalidate("expiry_exception", invoke_fail_safe=True)
                mapping, policy = self._fingerprints()
                return self._response(self._cleanup_status(), None, mapping, policy, (f"expiry_exception:{type(exc).__name__}",), evidence=self._fail_safe_evidence())

    async def async_begin(self, data: Mapping[str, object]) -> dict[str, object]:
        try:
            return await self._async_begin_inner(data)
        except asyncio.CancelledError:
            await self._cancel_cleanup()
            raise
        except Exception as exc:
            async with self._lock:
                await self._invalidate("begin_unexpected_exception", invoke_fail_safe=True)
                mapping, policy = self._fingerprints()
                return self._response(self._cleanup_status(), None, mapping, policy, (f"begin_unexpected_exception:{type(exc).__name__}",), evidence=self._fail_safe_evidence())

    async def _async_begin_inner(self, data: Mapping[str, object]) -> dict[str, object]:
        async with self._lock:
            if self._session is not None:
                return self._blocked(("commissioning_session_already_pending",))
            if (
                self._cleanup_task is not None and not self._cleanup_task.done()
            ) or (
                self._cleanup_io_task is not None and not self._cleanup_io_task.done()
            ):
                return self._blocked(("cleanup_child_pending",))
            base_keys = {"confirmation", "reserve_target", "operator_attestation", "manual_grid_import_power_kw", "manual_grid_setting_verified", "manual_grid_verified_at", "manual_grid_mapping_fingerprint", "manual_grid_policy_fingerprint"}
            if set(data) != base_keys:
                return self._blocked(("strict_begin_schema_rejected",))
            if data.get("confirmation") != CONFIRMATION_PHRASE:
                return self._blocked(("confirmation_phrase_mismatch",))
            if not self.enabled:
                return self._blocked(("commissioning_services_disabled",))
            if self._cleanup is not None:
                await self._ensure_cleanup("begin_cleanup_obligation")
                if self._cleanup is not None:
                    return self._blocked(("cleanup_obligation_pending",))
            ready, issues = self._coordinator_ready()
            if not ready:
                return self._blocked(issues)
            mapping, policy = self._fingerprints()
            if mapping is None or policy is None or len(mapping) != 64 or len(policy) != 64:
                return self._blocked(("current_fingerprints_unavailable",))
            snapshot = self._snapshot()
            solis_state = getattr(getattr(snapshot, "solis", None), "snapshot", None)
            if not self._slots_off(solis_state):
                return self._blocked(("all_twelve_slots_must_be_proven_off",))
            now = self._current_time()
            if not self._attested_manual_grid(data, mapping, policy, now):
                return self._blocked(("manual_grid_attestation_invalid",))
            try:
                capability_records = self._parse_capability_records(self.capability_resolutions, now=now, mapping=mapping, policy=policy)
            except (TypeError, ValueError) as exc:
                return self._blocked((f"capability_resolution_invalid:{exc}",))
            try:
                reserve_target = bounded_reserve_soc(_decimal(data["reserve_target"], "reserve_target"))
                if reserve_target < MINIMUM_SOC_PERCENT or reserve_target > FULL_SOC_PERCENT:
                    raise ValueError("reserve_target outside named SOC bounds")
            except ValueError as exc:
                return self._blocked((str(exc),))
            actuator = self._actuator()
            issue = getattr(getattr(actuator, "ephemeral_authorizations", None), "issue", None)
            if not callable(issue):
                return self._blocked(("candidate_authorization_api_unavailable",))
            nonce = issue(now=now, mapping=mapping, policy=policy, ttl=APPLICATION_AUTHORIZATION_LIFETIME)
            before = solis_state
            telemetry = getattr(before, "telemetry", None)
            before_device_time = getattr(telemetry, "device_timestamp", None)
            preserved_on = getattr(getattr(before, "persistent", None), "inverter_on_off", None)
            apply = getattr(actuator, "async_apply_candidate", None)
            if not callable(apply):
                return self._blocked(("candidate_actuator_unavailable",))
            prepare = getattr(getattr(actuator, "ephemeral_authorizations", None), "prepare_before_await", None)
            if callable(prepare) and not prepare(nonce):
                return self._blocked(("candidate_authorization_prepare_failed",))
            manual_record = self._manual_record(data, mapping, policy, now)
            try:
                result = apply(
                    getattr(snapshot, "solis"), now=now, reserve_target=reserve_target,
                    authorization=nonce,
                    manual_grid_import_verification=manual_record,
                    capability_resolutions={record.entity_id: record for record in capability_records},
                )
                result = await result if inspect.isawaitable(result) else result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                safe_result = await self._invalidate("candidate_application_exception", invoke_fail_safe=True)
                status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(status, None, mapping, policy, self._ordered_cleanup_issues((f"candidate_application_exception:{type(exc).__name__}",)), evidence=self._fail_safe_evidence())
            if type(result) is not PolicyActuationResult or result.status != PolicyActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION:
                safe_result = await self._invalidate("candidate_application_failed", invoke_fail_safe=True)
                failure_status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(failure_status, None, mapping, policy, self._ordered_cleanup_issues(("candidate_application_failed",) + tuple(getattr(result, "issues", ()))), evidence=self._fail_safe_evidence())
            session_id = secrets.token_urlsafe(18)
            applied_at = self._current_time()
            deadline = applied_at + COMMISSIONING_OBSERVATION_WINDOW
            self._session = CommissioningSession(session_id, mapping, policy, reserve_target, now, applied_at, deadline, before, before_device_time, preserved_on, manual_record, self._global_capabilities(before), capability_records, nonce, result, self._current_inverter_identity(before))
            try:
                self._schedule_deadline(deadline)
            except Exception as exc:
                safe_result = await self._invalidate("expiry_scheduler_error", invoke_fail_safe=True)
                failure_status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(failure_status, session_id, mapping, policy, self._ordered_cleanup_issues((f"expiry_scheduler_error:{type(exc).__name__}",)), evidence=self._fail_safe_evidence())
            return self._response(CommissioningStatus.TEST_APPLIED_HA_PENDING_DEVICE, session_id, mapping, policy, checklist={"candidate_applied": True, "slots_disabled": True, "observation_deadline": deadline.isoformat()}, evidence={"before_snapshot": before})

    @staticmethod
    def _manual_record(data: Mapping[str, object], mapping: str, policy: str, now: datetime) -> ManualGridImportVerification:
        verified_at = data["manual_grid_verified_at"]
        if isinstance(verified_at, str):
            verified_at = datetime.fromisoformat(verified_at)
        return ManualGridImportVerification(_decimal(data["manual_grid_import_power_kw"], "manual_grid_import_power_kw"), _aware(verified_at, "manual_grid_verified_at"), mapping, policy, bool(data["manual_grid_setting_verified"]))

    async def async_validate(self, data: Mapping[str, object]) -> dict[str, object]:
        try:
            return await self._async_validate_inner(data)
        except asyncio.CancelledError:
            await self._cancel_cleanup()
            raise
        except Exception as exc:
            async with self._lock:
                session_id = None if self._session is None else self._session.session_id
                await self._invalidate("validate_unexpected_exception", invoke_fail_safe=self._session is not None or self._cleanup is not None)
                mapping, policy = self._fingerprints()
                return self._response(self._cleanup_status(), session_id, mapping, policy, (f"validate_unexpected_exception:{type(exc).__name__}",), evidence=self._fail_safe_evidence())

    async def _async_validate_inner(self, data: Mapping[str, object]) -> dict[str, object]:
        async with self._lock:
            session = self._session
            if session is None:
                return self._blocked(("no_pending_session",))
            now = self._current_time()
            mapping, policy = self._fingerprints()
            if mapping != session.mapping_fingerprint or policy != session.policy_fingerprint:
                safe_result = await self._invalidate("fingerprint_drift", invoke_fail_safe=True)
                failure_status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(failure_status, session.session_id, mapping, policy, self._ordered_cleanup_issues(("fingerprint_drift",)), evidence=self._fail_safe_evidence())
            expected = {"session_id", "device_readback_attested", "observation_after_candidate", "home_assistant_context_consistent", "observed_device_timestamp", "outcomes", "commissioned_power_envelope"}
            if set(data) != expected:
                return self._blocked(("strict_validate_schema_rejected",))
            if data.get("session_id") != session.session_id:
                return self._blocked(("session_id_mismatch",))
            if now >= session.deadline:
                safe_result = await self._invalidate("observation_window_expired", invoke_fail_safe=True)
                status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(status, session.session_id, mapping, policy, self._ordered_cleanup_issues(("observation_window_expired",)), evidence=self._fail_safe_evidence())
            ready, issues = self._coordinator_ready()
            if not ready:
                safe_result = await self._invalidate("coordinator_degraded", invoke_fail_safe=True)
                status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(status, session.session_id, mapping, policy, self._ordered_cleanup_issues(issues), evidence=self._fail_safe_evidence())
            snapshot = self._snapshot()
            solis = getattr(snapshot, "solis", None)
            state = getattr(solis, "snapshot", None)
            critical: list[str] = []
            telemetry = getattr(state, "telemetry", None)
            device_time = getattr(telemetry, "device_timestamp", None)
            if session.before_device_timestamp is None or not isinstance(device_time, datetime) or device_time <= session.before_device_timestamp:
                critical.append("device_timestamp_did_not_advance")
            try:
                supplied_device_time = data["observed_device_timestamp"]
                if isinstance(supplied_device_time, str):
                    supplied_device_time = datetime.fromisoformat(supplied_device_time)
                if _aware(supplied_device_time, "observed_device_timestamp") != device_time:
                    critical.append("observed_device_timestamp_readback_mismatch")
            except (TypeError, ValueError):
                critical.append("observed_device_timestamp_invalid")
            observed_at = getattr(state, "observed_at", None)
            if not isinstance(observed_at, datetime) or observed_at < session.candidate_applied_at:
                critical.append("observation_not_after_candidate_application")
            if data.get("device_readback_attested") is not True or data.get("observation_after_candidate") is not True:
                critical.append("separate_device_readback_attestation_required")
            if data.get("home_assistant_context_consistent") is not True:
                critical.append("home_assistant_context_consistency_missing")
            if not self._slots_off(state):
                critical.append("slot_readback_not_all_disabled")
            critical.extend(self._exact_candidate_readback(state, session))
            if self._global_capabilities(state) != dict(session.before_capabilities):
                critical.append("global_capability_readback_not_preserved")
            capability_records = session.capability_resolutions
            try:
                outcome_items = data["outcomes"]
                if not isinstance(outcome_items, Sequence) or len(outcome_items) != len(REQUIRED_OUTCOMES):
                    raise ValueError("all required behavior outcomes are required")
                outcomes = tuple(_outcome_from_data(item, now) for item in outcome_items)
                if tuple(item.name for item in outcomes) != REQUIRED_OUTCOMES:
                    raise ValueError("required outcomes must be in the defined order")
                upper_time = min(now, session.deadline)
                if any(item.observed_at < session.candidate_applied_at or item.observed_at > upper_time for item in outcomes):
                    raise ValueError("behavior evidence time is outside the active observation")
                critical.extend(item.name + "_" + item.status.lower() for item in outcomes if item.status in {"FAIL", "AMBIGUOUS"})
            except (TypeError, ValueError) as exc:
                critical.append(str(exc))
                outcomes = ()
            try:
                envelope = await self._verify_envelope(data.get("commissioned_power_envelope"), session=session, state=state, now=now, mapping=mapping or "", policy=policy or "", capability_resolutions=capability_records)
            except ValueError as exc:
                critical.append(str(exc))
                envelope = None
            try:
                evidence = CommissioningEvidence(outcomes, bool(data.get("device_readback_attested")), bool(data.get("observation_after_candidate")), bool(data.get("home_assistant_context_consistent")), device_time, envelope, self._capture_capabilities(state), session.manual_grid_verification, capability_records)
            except Exception as exc:
                safe_result = await self._invalidate("capability_evidence_error", invoke_fail_safe=True)
                failure_status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(failure_status, session.session_id, mapping, policy, (f"capability_evidence_error:{type(exc).__name__}",))
            if critical:
                safe_result = await self._invalidate("critical_validation_failure", invoke_fail_safe=True)
                failure_status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(failure_status, session.session_id, mapping, policy, self._ordered_cleanup_issues(tuple(critical)), evidence={"validation": evidence, **self._fail_safe_evidence()})
            if any(item.status == "MISSING" for item in outcomes):
                # Exact post-candidate state is proven, but supervised behavior
                # evidence is incomplete.  Keep the same bounded session alive;
                # this path can never emit the review snippet.
                return self._response(CommissioningStatus.PENDING_EVIDENCE, session.session_id, mapping, policy, ("required_behavior_evidence_pending",), checklist={"test_proven_off": True, "observation_deadline": session.deadline.isoformat()}, evidence=_jsonable(evidence))
            self._cancel_deadline()
            try:
                snippet = self._iac_snippet(mapping or session.mapping_fingerprint, policy or session.policy_fingerprint, now, evidence)
            except Exception as exc:
                safe_result = await self._invalidate("snippet_generation_error", invoke_fail_safe=True)
                failure_status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
                return self._response(failure_status, session.session_id, mapping, policy, self._ordered_cleanup_issues((f"snippet_generation_error:{type(exc).__name__}",)), evidence={"validation": evidence, **self._fail_safe_evidence()})
            self._session = None
            return self._response(CommissioningStatus.COMPLETE_REVIEW_REQUIRED, session.session_id, mapping, policy, checklist={"persistent_candidate_authorized": False, "production_slot_authorized": False, "all_slots_disabled": True}, evidence=_jsonable(evidence), snippet=snippet)

    @staticmethod
    def _exact_candidate_readback(state: object, session: CommissioningSession) -> list[str]:
        persistent = getattr(state, "persistent", None)
        if persistent is None:
            return ["persistent_readback_missing"]
        expected = {
            "storage_mode": StorageMode.FEED_IN_PRIORITY.value,
            "allow_grid_charging": True,
            "allow_export": True,
            "grid_peak_shaving": True,
            "battery_reserve": True,
        }
        issues = [name + "_mismatch" for name, value in expected.items() if getattr(persistent, name, None) != value]
        checks = {
            "over_discharge_soc": Decimal(MINIMUM_SOC_PERCENT),
            "force_charge_soc": Decimal(FORCE_CHARGE_SOC_PERCENT),
            "recovery_soc": Decimal(MINIMUM_SOC_PERCENT),
            "maximum_charge_soc": Decimal(FULL_SOC_PERCENT),
            "battery_reserve_soc": session.reserve_target.to_integral_value(),
        }
        for name, target in checks.items():
            capability = getattr(persistent, name, None)
            if getattr(capability, "current_value", None) != target:
                issues.append(name + "_mismatch")
        if getattr(persistent, "inverter_on_off", None) != session.preserved_inverter_on:
            issues.append("inverter_on_off_not_preserved")
        return issues

    @staticmethod
    def _capture_capabilities(state: object) -> dict[str, object]:
        if state is None:
            return {}
        values: dict[str, object] = {}
        for section_name in ("capabilities",):
            section = getattr(state, section_name, None)
            if section is not None:
                values[section_name] = _jsonable(section)
        persistent = getattr(state, "persistent", None)
        if persistent is not None:
            for name in ("over_discharge_soc", "force_charge_soc", "recovery_soc", "maximum_charge_soc", "battery_reserve_soc"):
                values[name] = _jsonable(getattr(persistent, name, None))
        values["slots"] = _jsonable(getattr(state, "slots", ()))
        return values

    @staticmethod
    def _capability_metadata_complete(state: object) -> bool:
        capabilities = getattr(state, "capabilities", None)
        persistent = getattr(state, "persistent", None)
        slots = getattr(state, "slots", ())
        if capabilities is None or persistent is None or not isinstance(slots, Sequence) or len(slots) != 6:
            return False
        required_persistent = ("over_discharge_soc", "force_charge_soc", "recovery_soc", "maximum_charge_soc", "battery_reserve_soc")
        if any(not hasattr(persistent, name) for name in required_persistent):
            return False
        for slot in slots:
            for direction in (getattr(slot, "charge", None), getattr(slot, "discharge", None)):
                if direction is None or not hasattr(direction, "current") or not hasattr(direction, "target_soc"):
                    return False
        return True

    async def async_abort(self) -> dict[str, object]:
        try:
            return await self._async_abort_inner()
        except asyncio.CancelledError:
            await self._cancel_cleanup()
            raise
        except Exception as exc:
            async with self._lock:
                await self._invalidate("abort_unexpected_exception", invoke_fail_safe=True)
                mapping, policy = self._fingerprints()
                return self._response(self._cleanup_status(), None, mapping, policy, (f"abort_unexpected_exception:{type(exc).__name__}",), evidence=self._fail_safe_evidence())

    async def _async_abort_inner(self) -> dict[str, object]:
        async with self._lock:
            session_id = None if self._session is None else self._session.session_id
            safe_result = await self._invalidate("explicit_abort", invoke_fail_safe=True)
            mapping, policy = self._fingerprints()
            status = CommissioningStatus.FAILED_SAFE if self._fail_safe_proven(safe_result) else CommissioningStatus.FAILED_UNSAFE
            return self._response(status, session_id, mapping, policy, self._ordered_cleanup_issues(("explicit_abort",)), evidence=self._fail_safe_evidence())

    async def _cancel_cleanup(self) -> None:
        """Run bounded shielded fail-safe cleanup before propagating cancellation."""

        async def cleanup() -> None:
            async with self._lock:
                await self._invalidate("cancelled", invoke_fail_safe=True)

        task = self._cleanup_task
        if task is None or task.done():
            task = asyncio.create_task(cleanup())
            self._cleanup_task = task
        deadline = time.monotonic() + CLEANUP_DEADLINE.total_seconds()
        while not task.done() and time.monotonic() < deadline:
            try:
                await asyncio.wait_for(asyncio.shield(task), max(0.001, deadline - time.monotonic()))
            except asyncio.CancelledError:
                # A caller may cancel repeatedly. Shield keeps the cleanup
                # child alive and no uncancel() is used to hide cancellation.
                continue
            except asyncio.TimeoutError:
                break
        if not task.done():
            # Cancellation-resistant device I/O must never extend the
            # caller's cancellation path.  Preserve one typed unsafe
            # obligation and its deadline diagnostic; a later lifecycle
            # invocation may retry it.
            now = self._current_time()
            obligation = self._cleanup
            if obligation is None:
                obligation = CleanupObligation("cancelled", now)
                self._cleanup = obligation
            self._cleanup_generation += 1
            obligation.generation = self._cleanup_generation
            if "cleanup_deadline_exhausted" not in obligation.issues:
                obligation.issues.append("cleanup_deadline_exhausted")
            obligation.result = PolicyActuationResult(
                PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE,
                issues=("cleanup_deadline_exhausted",),
            )
            obligation.proof = self.last_fail_safe_proof if type(self.last_fail_safe_proof) is CleanupProof else self._fresh_cleanup_proof(obligation.attempt_started_at or now)
            obligation.completed_at = now
            self.last_fail_safe_result = obligation.result
            self.last_fail_safe_proof = obligation.proof
            task.cancel()
            # Give a cancellation-aware child one short bounded opportunity
            # to run its finally blocks.  Do not await it without a bound.
            try:
                await asyncio.wait_for(asyncio.shield(task), 0.01)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            # Keep a still-running child attached to the obligation.  It is
            # drained by the next cleanup retry and cannot become an orphan.
            if not task.done():
                return
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        if self._cleanup_task is task:
            self._cleanup_task = None

    async def async_handle_lifecycle(self) -> None:
        try:
            async with self._lock:
                if self._session is None:
                    if self._cleanup is not None:
                        await self._ensure_cleanup("lifecycle_cleanup_retry")
                    return
                mapping, policy = self._fingerprints()
                if mapping != self._session.mapping_fingerprint or policy != self._session.policy_fingerprint:
                    await self._invalidate("fingerprint_drift", invoke_fail_safe=True)
                    return
                ready, _ = self._coordinator_ready()
                if not ready:
                    await self._invalidate("lifecycle_degradation", invoke_fail_safe=True)
        except asyncio.CancelledError:
            await self._cancel_cleanup()
            raise
        except Exception:
            async with self._lock:
                await self._invalidate("lifecycle_unexpected_exception", invoke_fail_safe=True)

    def _iac_snippet(self, mapping: str, policy: str, validated_at: datetime, evidence: CommissioningEvidence) -> str:
        # Hand-rendered YAML makes ordering and disabled authority obvious and
        # avoids persisting a token or writing the repository.
        lines = [
            "candidate_commissioning:",
            "  services_enabled: false",
            "  persistent_candidate_authorization: null",
            f"  candidate_mapping_fingerprint: '{mapping}'",
            f"  candidate_policy_fingerprint: '{policy}'",
            f"  candidate_validated_at: {validated_at.isoformat()}",
            "  manual_grid_verification:",
            f"    maximum_grid_import_power_kw: '{evidence.manual_grid_verification.maximum_grid_import_power_kw if evidence.manual_grid_verification else MAXIMUM_GRID_IMPORT_POWER_KW}'",
            f"    verified_at: {(evidence.manual_grid_verification.verified_at if evidence.manual_grid_verification else validated_at).isoformat()}",
            f"    mapping_fingerprint: '{evidence.manual_grid_verification.mapping_fingerprint if evidence.manual_grid_verification else mapping}'",
            f"    policy_fingerprint: '{evidence.manual_grid_verification.policy_fingerprint if evidence.manual_grid_verification else policy}'",
            f"    setting_contains_expected_value: {str(bool(evidence.manual_grid_verification and evidence.manual_grid_verification.setting_contains_expected_value)).lower()}",
            "  capability_resolutions:",
        ]
        if not evidence.capability_resolutions:
            lines[-1] = "  capability_resolutions: []"
        else:
            for record in evidence.capability_resolutions:
                target = "documented_unlimited" if hasattr(record.verified_target, "__class__") and record.verified_target.__class__.__name__ == "DocumentedUnlimitedValue" else str(record.verified_target)
                lines.extend([
                    "    - entity_id: '" + record.entity_id + "'",
                    "      observed_unit: '" + record.observed_unit + "'",
                    "      verified_target: '" + target + "'",
                    "      verified_at: " + record.verified_at.isoformat(),
                    "      mapping_fingerprint: '" + record.mapping_fingerprint + "'",
                    "      policy_fingerprint: '" + record.policy_fingerprint + "'",
                    "      evidence_classification: '" + record.evidence_classification + "'",
                    "      documented_unlimited_target: " + ("null" if record.documented_unlimited_target is None else "'" + str(record.documented_unlimited_target) + "'"),
                ])
        if evidence.power_envelope is None:
            lines.append("  commissioned_power_envelope: null")
        else:
            env = evidence.power_envelope
            lines.extend([
                "  commissioned_power_envelope:",
                f"    maximum_charge_power_kw: '{env.maximum_charge_power_kw}'",
                f"    maximum_discharge_power_kw: '{env.maximum_discharge_power_kw}'",
                f"    maximum_grid_import_power_kw: '{env.maximum_grid_import_power_kw}'",
                f"    schema_version: '{env.schema_version}'",
                f"    inverter_identity: '{env.inverter_identity}'",
                f"    mapping_fingerprint: '{env.mapping_fingerprint}'",
                f"    candidate_policy_fingerprint: '{env.candidate_policy_fingerprint}'",
                f"    manual_grid_fingerprint: '{env.manual_grid_fingerprint}'",
                f"    capability_fingerprint: '{env.capability_fingerprint}'",
                f"    evidence_source: '{env.evidence_source}'",
                f"    validated_at: {env.validated_at.isoformat()}",
            ])
        return "\n".join(lines) + "\n"


def service_schemas() -> dict[str, vol.Schema]:
    """Strict service schemas; handlers return response dictionaries."""

    fingerprint = vol.All(str, vol.Length(min=64, max=64))
    dt = vol.All(str, vol.Length(min=1))
    common_begin = {
        vol.Required("confirmation"): vol.All(str, vol.Equal(CONFIRMATION_PHRASE)),
        vol.Required("reserve_target"): vol.Coerce(str),
        vol.Required("operator_attestation"): vol.Equal(True),
        vol.Required("manual_grid_import_power_kw"): vol.Coerce(str),
        vol.Required("manual_grid_setting_verified"): vol.Equal(True),
        vol.Required("manual_grid_verified_at"): dt,
        vol.Required("manual_grid_mapping_fingerprint"): fingerprint,
        vol.Required("manual_grid_policy_fingerprint"): fingerprint,
    }
    outcome = vol.Schema({
        vol.Required("name"): vol.In(REQUIRED_OUTCOMES),
        vol.Required("status"): vol.In(("PASS", "FAIL", "AMBIGUOUS", "MISSING")),
        vol.Required("evidence_note"): vol.All(str, vol.Length(min=1)),
        vol.Required("observed_at"): dt,
    }, extra=vol.PREVENT_EXTRA)
    envelope = vol.Any(None, vol.Schema({
        vol.Required(key): vol.Any(str, int, float, bool, None) for key in (
            "maximum_charge_power_kw", "maximum_discharge_power_kw", "maximum_grid_import_power_kw",
            "schema_version", "inverter_identity", "mapping_fingerprint", "candidate_policy_fingerprint",
            "manual_grid_fingerprint", "capability_fingerprint", "evidence_source", "validated_at",
        )
    }, extra=vol.PREVENT_EXTRA))
    validate = {
        vol.Required("session_id"): vol.All(str, vol.Length(min=1)),
        vol.Required("device_readback_attested"): vol.Equal(True),
        vol.Required("observation_after_candidate"): vol.Equal(True),
        vol.Required("home_assistant_context_consistent"): vol.Equal(True),
        vol.Required("observed_device_timestamp"): dt,
        vol.Required("outcomes"): vol.All([outcome], vol.Length(min=len(REQUIRED_OUTCOMES), max=len(REQUIRED_OUTCOMES))),
        vol.Required("commissioned_power_envelope"): envelope,
    }
    return {
        SERVICE_BEGIN: vol.Schema(common_begin, extra=vol.PREVENT_EXTRA),
        SERVICE_VALIDATE: vol.Schema(validate, extra=vol.PREVENT_EXTRA),
        SERVICE_ABORT: vol.Schema({}, extra=vol.PREVENT_EXTRA),
    }


__all__ = [
    "APPLICATION_AUTHORIZATION_LIFETIME", "COMMISSIONING_OBSERVATION_WINDOW", "CONFIRMATION_PHRASE",
    "CLEANUP_DEADLINE", "CleanupObligation", "CleanupProof", "CleanupStateEvidence",
    "CommissionedEnvelopeEvidenceProvider", "CommissionedEnvelopeEvidenceRecord", "CommissionedPowerEnvelope",
    "CommissioningEvidence", "CommissioningSession", "CommissioningStatus", "ManualGridImportVerification", "CapabilityResolutionRecord",
    "CommissioningWorkflow", "BehaviorOutcome", "REQUIRED_OUTCOMES", "SERVICE_BEGIN", "SERVICE_VALIDATE",
    "SERVICE_ABORT", "service_schemas",
]
