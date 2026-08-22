"""Persistent Solis policy builders and guarded candidate/fail-safe actuator.

The policy boundary is intentionally Home Assistant free apart from the
injected :class:`HomeAssistantWriter`.  It owns commissioning authorization,
policy fingerprints, and the ordered persistent writes; timed-slot writes
remain owned by :mod:`solis_actuator`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import inspect
import json
import secrets
from typing import Any

from .contracts import (
    CapabilityTarget,
    DocumentedUnlimitedValue,
    InverterPolicy,
    MaximumVerifiedValue,
    PreserveCurrentValue,
    StorageMode,
)
from .domain_constants import (
    FORCE_CHARGE_SOC_PERCENT,
    FULL_SOC_PERCENT,
    MAXIMUM_GRID_IMPORT_POWER_KW,
    MINIMUM_SOC_PERCENT,
)
from .ha_writer import HomeAssistantWriter
from .solis_actuator import DisableAllResult, SolisSlotActuator, mapping_fingerprint
from .solis_config import SolisConfig
from .solis_state import SolisStateReadResult, SolisStateSnapshot
from .write_contracts import (
    NumberWriteRequest,
    SelectWriteRequest,
    StatePrecondition,
    SwitchWriteRequest,
    WriteOutcome,
    WriteResult,
)


POLICY_FINGERPRINT_SCHEMA_VERSION = 1
ACTUATOR_ORDERING_VERSION = 1
COMMISSIONING_AUTH_SCHEMA_VERSION = 1
MAXIMUM_EPHEMERAL_AUTHORIZATION_AGE = timedelta(minutes=10)


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _not_future(value: datetime, now: datetime, name: str) -> None:
    _aware(value, name)
    _aware(now, "now")
    if value > now:
        raise ValueError(f"{name} must not be in the future")


def _fingerprint(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a SHA-256 fingerprint")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 fingerprint") from exc


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be Decimal-compatible")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be Decimal-compatible") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(_decimal(value, "fingerprint value"), "f")


def bounded_reserve_soc(target: Decimal | int | str) -> Decimal:
    """Round reserve upward and apply only the named physical SOC bounds."""

    value = _decimal(target, "reserve target")
    if value < Decimal(0) or value > Decimal(100):
        raise ValueError("reserve target must be from 0 through 100 percent")
    rounded = value.to_integral_value(rounding=ROUND_CEILING)
    return max(Decimal(MINIMUM_SOC_PERCENT), min(Decimal(FULL_SOC_PERCENT), rounded))


def build_candidate_policy(
    reserve_target: Decimal | int | str,
    *,
    output_power_target: CapabilityTarget | None = None,
    feed_in_power_target: CapabilityTarget | None = None,
) -> InverterPolicy:
    """Build the healthy persistent candidate from named safety constants."""

    return InverterPolicy(
        storage_mode=StorageMode.FEED_IN_PRIORITY,
        grid_charge_allowed=True,
        export_allowed=True,
        peak_shaving_enabled=True,
        over_discharge_soc=Decimal(MINIMUM_SOC_PERCENT),
        force_charge_soc=Decimal(FORCE_CHARGE_SOC_PERCENT),
        recovery_soc=Decimal(MINIMUM_SOC_PERCENT),
        maximum_charge_soc=Decimal(FULL_SOC_PERCENT),
        battery_reserve_enabled=True,
        battery_reserve_soc=bounded_reserve_soc(reserve_target),
        output_power_target=output_power_target or PreserveCurrentValue(),
        feed_in_power_target=feed_in_power_target or PreserveCurrentValue(),
    )


def build_fail_safe_policy() -> InverterPolicy:
    """Build the persistent safe policy; physical protections are preserved."""

    return InverterPolicy(
        storage_mode=StorageMode.SELF_USE,
        grid_charge_allowed=True,
        export_allowed=True,
        peak_shaving_enabled=True,
        over_discharge_soc=Decimal(MINIMUM_SOC_PERCENT),
        force_charge_soc=Decimal(FORCE_CHARGE_SOC_PERCENT),
        recovery_soc=Decimal(MINIMUM_SOC_PERCENT),
        maximum_charge_soc=Decimal(FULL_SOC_PERCENT),
        battery_reserve_enabled=False,
        battery_reserve_soc=Decimal(MINIMUM_SOC_PERCENT),
        output_power_target=PreserveCurrentValue(),
        feed_in_power_target=PreserveCurrentValue(),
    )


def _target_fingerprint(target: CapabilityTarget) -> object:
    if isinstance(target, MaximumVerifiedValue):
        return {"kind": "maximum_verified"}
    if isinstance(target, DocumentedUnlimitedValue):
        return {"kind": "documented_unlimited"}
    if isinstance(target, PreserveCurrentValue):
        return {"kind": "preserve_current"}
    return {"kind": type(target).__name__}


def canonical_policy() -> bytes:
    """Return canonical bytes for the policy definition, excluding live reserve."""

    data: dict[str, object] = {
        "policy_fingerprint_schema_version": POLICY_FINGERPRINT_SCHEMA_VERSION,
        "actuator_ordering_version": ACTUATOR_ORDERING_VERSION,
        "safety_constants": {
            "MINIMUM_SOC_PERCENT": str(MINIMUM_SOC_PERCENT),
            "FORCE_CHARGE_SOC_PERCENT": str(FORCE_CHARGE_SOC_PERCENT),
            "FULL_SOC_PERCENT": str(FULL_SOC_PERCENT),
        },
        "candidate": {
            "storage_mode": StorageMode.FEED_IN_PRIORITY.value,
            "grid_charge_allowed": True,
            "export_allowed": True,
            "peak_shaving_enabled": True,
            "over_discharge_soc": _decimal_text(Decimal(MINIMUM_SOC_PERCENT)),
            "force_charge_soc": _decimal_text(Decimal(FORCE_CHARGE_SOC_PERCENT)),
            "recovery_soc": _decimal_text(Decimal(MINIMUM_SOC_PERCENT)),
            "maximum_charge_soc": _decimal_text(Decimal(FULL_SOC_PERCENT)),
            "battery_reserve_enabled": True,
            "battery_reserve_target_rule": "ceil_then_bound_by_MINIMUM_SOC_PERCENT_and_FULL_SOC_PERCENT",
            "capability_targets": {
                "global_charge_current": "preserve_unless_verified",
                "global_discharge_current": "preserve_unless_verified",
                "output_power": _target_fingerprint(PreserveCurrentValue()),
                "feed_in_power": _target_fingerprint(PreserveCurrentValue()),
            },
        },
        "fail_safe": {
            "storage_mode": StorageMode.SELF_USE.value,
            "grid_charge_allowed": "preserve",
            "export_allowed": "preserve",
            "peak_shaving_enabled": True,
            "battery_reserve_enabled": False,
            "slots": "all_off",
            "inverter_power_and_protection": "preserve",
        },
        "manual_maximum_grid_import_policy": {
            "mode": "manual_commissioning",
            "expected_value_kw": _decimal_text(MAXIMUM_GRID_IMPORT_POWER_KW),
        },
        "capability_resolution_policy": "exact_entity_unit_and_fingerprint_bound_verified_maximum_or_documented_unlimited",
        "mapping_fingerprint_schema_version": 1,
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def policy_fingerprint() -> str:
    return hashlib.sha256(canonical_policy()).hexdigest()


canonical_policy_bytes = canonical_policy
compute_policy_fingerprint = policy_fingerprint


@dataclass(frozen=True, slots=True)
class EphemeralCandidateAuthorization:
    issued_at: datetime
    expires_at: datetime
    nonce: str
    mapping_fingerprint: str
    policy_fingerprint: str
    schema_version: int = COMMISSIONING_AUTH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _aware(self.issued_at, "issued_at")
        _aware(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("authorization expiry must be after issuance")
        if self.expires_at - self.issued_at > MAXIMUM_EPHEMERAL_AUTHORIZATION_AGE:
            raise ValueError("ephemeral authorization may not last more than ten minutes")
        if not self.nonce:
            raise ValueError("authorization nonce must not be empty")
        _fingerprint(self.mapping_fingerprint, "mapping_fingerprint")
        _fingerprint(self.policy_fingerprint, "policy_fingerprint")


@dataclass(frozen=True, slots=True)
class PersistentCandidateAuthorization:
    issued_at: datetime
    mapping_fingerprint: str
    policy_fingerprint: str
    ha_readback_validated: bool
    device_reconciliation_validated: bool
    schema_version: int = COMMISSIONING_AUTH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _aware(self.issued_at, "issued_at")
        _fingerprint(self.mapping_fingerprint, "mapping_fingerprint")
        _fingerprint(self.policy_fingerprint, "policy_fingerprint")
        if not isinstance(self.ha_readback_validated, bool) or not isinstance(self.device_reconciliation_validated, bool):
            raise TypeError("authorization validation flags must be bool")


@dataclass(frozen=True, slots=True)
class ManualGridImportVerification:
    maximum_grid_import_power_kw: Decimal
    verified_at: datetime
    mapping_fingerprint: str
    policy_fingerprint: str
    setting_contains_expected_value: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "maximum_grid_import_power_kw", _decimal(self.maximum_grid_import_power_kw, "maximum_grid_import_power_kw"))
        _aware(self.verified_at, "verified_at")
        _fingerprint(self.mapping_fingerprint, "mapping_fingerprint")
        _fingerprint(self.policy_fingerprint, "policy_fingerprint")
        if not isinstance(self.setting_contains_expected_value, bool):
            raise TypeError("setting_contains_expected_value must be bool")

    def valid(self, *, now: datetime, mapping: str, policy: str) -> bool:
        try:
            _not_future(self.verified_at, now, "verified_at")
        except ValueError:
            return False
        return (
            self.maximum_grid_import_power_kw == MAXIMUM_GRID_IMPORT_POWER_KW
            and self.mapping_fingerprint == mapping
            and self.policy_fingerprint == policy
            and self.setting_contains_expected_value
        )


@dataclass(frozen=True, slots=True)
class CapabilityResolutionRecord:
    entity_id: str
    observed_unit: str
    verified_target: Decimal | DocumentedUnlimitedValue
    verified_at: datetime
    mapping_fingerprint: str
    policy_fingerprint: str
    evidence_classification: str

    def __post_init__(self) -> None:
        if not self.entity_id or not self.observed_unit or not self.evidence_classification:
            raise ValueError("capability verification identifiers must not be empty")
        if not isinstance(self.verified_target, DocumentedUnlimitedValue):
            object.__setattr__(self, "verified_target", _decimal(self.verified_target, "verified_target"))
        _aware(self.verified_at, "verified_at")
        _fingerprint(self.mapping_fingerprint, "mapping_fingerprint")
        _fingerprint(self.policy_fingerprint, "policy_fingerprint")

    def valid(self, *, now: datetime, mapping: str, policy: str, unit: str) -> bool:
        try:
            _not_future(self.verified_at, now, "verified_at")
        except ValueError:
            return False
        return self.entity_id and self.observed_unit == unit and self.mapping_fingerprint == mapping and self.policy_fingerprint == policy


class EphemeralAuthorizationStore:
    """Non-persistent, single-use bootstrap authorization store."""

    def __init__(self) -> None:
        self._issued: dict[str, EphemeralCandidateAuthorization] = {}
        self._consumed: set[str] = set()

    def issue(self, *, now: datetime, mapping: str, policy: str, ttl: timedelta = MAXIMUM_EPHEMERAL_AUTHORIZATION_AGE) -> EphemeralCandidateAuthorization:
        _aware(now, "now")
        if ttl <= timedelta(0) or ttl > MAXIMUM_EPHEMERAL_AUTHORIZATION_AGE:
            raise ValueError("ephemeral authorization TTL is outside the allowed range")
        token = EphemeralCandidateAuthorization(now, now + ttl, secrets.token_urlsafe(24), mapping, policy)
        self._issued[token.nonce] = token
        return token

    def consume(self, token: EphemeralCandidateAuthorization, *, now: datetime, mapping: str, policy: str) -> bool:
        if not isinstance(token, EphemeralCandidateAuthorization) or token.nonce in self._consumed:
            return False
        issued = self._issued.get(token.nonce)
        if issued != token or token.schema_version != COMMISSIONING_AUTH_SCHEMA_VERSION:
            return False
        self._consumed.add(token.nonce)
        return token.issued_at <= now <= token.expires_at and token.mapping_fingerprint == mapping and token.policy_fingerprint == policy


@dataclass(frozen=True, slots=True)
class PolicyActuationResult:
    status: str
    candidate_results: tuple[WriteResult, ...] = ()
    fallback_results: tuple[WriteResult, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def results(self) -> tuple[WriteResult, ...]:
        return self.candidate_results + self.fallback_results

    @property
    def safe(self) -> bool:
        return self.status in {"FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION", "CANDIDATE_FAILED_FAIL_SAFE_APPLIED"}


class PolicyActuationStatus:
    BLOCKED = "BLOCKED"
    APPLIED_HA_PENDING_DEVICE_RECONCILIATION = "APPLIED_HA_PENDING_DEVICE_RECONCILIATION"
    FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION = "FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION"
    CANDIDATE_FAILED_FAIL_SAFE_APPLIED = "CANDIDATE_FAILED_FAIL_SAFE_APPLIED"
    FAIL_SAFE_FAILED_UNSAFE = "FAIL_SAFE_FAILED_UNSAFE"
    FAILED_UNSAFE = "FAILED_UNSAFE"


class SolisPolicyActuator:
    """Apply persistent candidate/fail-safe state with fresh CAS and proofs."""

    def __init__(
        self,
        config: SolisConfig,
        writer: HomeAssistantWriter,
        *,
        control_disable_guard_entity_id: str,
        inverter_timezone: Any,
        slot_actuator: SolisSlotActuator | None = None,
        observation_refresh: Callable[..., SolisStateReadResult] | None = None,
        persistent_authorization: PersistentCandidateAuthorization | None = None,
        ephemeral_authorizations: EphemeralAuthorizationStore | None = None,
    ) -> None:
        self.config = config
        self.writer = writer
        self.control_disable_guard_entity_id = control_disable_guard_entity_id
        self.slot_actuator = slot_actuator or SolisSlotActuator(config, writer, control_disable_guard_entity_id=control_disable_guard_entity_id, inverter_timezone=inverter_timezone)
        self.observation_refresh = observation_refresh
        self.persistent_authorization = persistent_authorization
        self.ephemeral_authorizations = ephemeral_authorizations or EphemeralAuthorizationStore()

    @property
    def mapping_fingerprint(self) -> str:
        return mapping_fingerprint(self.config)

    @property
    def policy_fingerprint(self) -> str:
        return policy_fingerprint()

    def _guard_off(self) -> bool:
        try:
            return self.writer.capture_precondition(self.control_disable_guard_entity_id).state == "off"
        except Exception:
            return False

    def _precondition(self, entity_id: str) -> StatePrecondition:
        return self.writer.capture_precondition(entity_id)

    async def _refresh(self, now: datetime) -> SolisStateReadResult | None:
        if self.observation_refresh is None:
            return None
        try:
            result = self.observation_refresh(now)
        except TypeError:
            # Keep the boundary convenient for a reader callback that closes
            # over its clock; production callers should prefer the explicit
            # ``now`` form so refresh timing is auditable.
            result = self.observation_refresh()
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, SolisStateReadResult) else None

    async def _candidate_write(self, request: object, results: list[WriteResult]) -> WriteResult:
        if not self._guard_off():
            raise RuntimeError("control-disable guard is not exactly off")
        result = await self.writer.async_write(request)  # type: ignore[arg-type]
        results.append(result)
        if not result.success:
            raise RuntimeError(f"{result.entity_id}: {result.message or result.outcome.value}")
        if not self._guard_off():
            raise RuntimeError("control-disable guard changed during candidate application")
        return result

    def _number_request(self, entity_id: str, target: Decimal, capability: object) -> NumberWriteRequest:
        return NumberWriteRequest(self._precondition(entity_id), target, capability=capability)  # type: ignore[arg-type]

    async def _safe_write(self, entity_id: str, target: object, results: list[WriteResult], *, domain: str, capability: object | None = None) -> None:
        try:
            precondition = self._precondition(entity_id)
            if domain == "switch":
                request = SwitchWriteRequest(precondition, bool(target))
            elif domain == "select":
                request = SelectWriteRequest(precondition, str(target))
            else:
                request = NumberWriteRequest(precondition, target, capability=capability)  # type: ignore[arg-type]
            results.append(await self.writer.async_write(request))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            results.append(WriteResult(entity_id, WriteOutcome.REJECTED, str(exc)))

    @staticmethod
    def _required_safe_state(writer: HomeAssistantWriter, config: SolisConfig) -> bool:
        try:
            for slot in config.slots:
                for direction in (slot.charge, slot.discharge):
                    if writer.capture_precondition(direction.enable_entity_id).state != "off":
                        return False
            p = config.persistent
            return (
                writer.capture_precondition(p.storage_mode_entity_id).state == StorageMode.SELF_USE.value
                and writer.capture_precondition(p.grid_peak_shaving_entity_id).state == "on"
                and writer.capture_precondition(config.protection.battery_reserve_entity_id).state == "off"
            )
        except Exception:
            return False

    async def _apply_fail_safe_internal(self) -> tuple[tuple[WriteResult, ...], bool]:
        results: list[WriteResult] = []
        try:
            disable: DisableAllResult = await self.slot_actuator.async_disable_all()
            results.extend(disable.results)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            results.append(WriteResult("solis.slots", WriteOutcome.REJECTED, str(exc)))
        p = self.config.persistent
        protection = self.config.protection
        # Continue independently after every expected failure.  The safe mode,
        # peak shaving, and reserve writes are the only persistent mutations.
        await self._safe_write(p.storage_mode_entity_id, StorageMode.SELF_USE.value, results, domain="select")
        await self._safe_write(p.grid_peak_shaving_entity_id, True, results, domain="switch")
        await self._safe_write(protection.battery_reserve_entity_id, False, results, domain="switch")
        safe = self._required_safe_state(self.writer, self.config)
        return tuple(results), safe

    async def async_apply_fail_safe(self) -> PolicyActuationResult:
        try:
            results, safe = await self._apply_fail_safe_internal()
            status = PolicyActuationStatus.FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION if safe else PolicyActuationStatus.FAIL_SAFE_FAILED_UNSAFE
            return PolicyActuationResult(status, fallback_results=results)
        except asyncio.CancelledError as original:
            task = asyncio.create_task(self._apply_fail_safe_internal())
            await self._await_cleanup(task)
            raise original

    def _authorization_valid(self, authorization: object, now: datetime) -> bool:
        mapping = self.mapping_fingerprint
        policy = self.policy_fingerprint
        if isinstance(authorization, PersistentCandidateAuthorization):
            try:
                _not_future(authorization.issued_at, now, "issued_at")
            except ValueError:
                return False
            return authorization.schema_version == COMMISSIONING_AUTH_SCHEMA_VERSION and authorization.mapping_fingerprint == mapping and authorization.policy_fingerprint == policy and authorization.ha_readback_validated and authorization.device_reconciliation_validated
        return isinstance(authorization, EphemeralCandidateAuthorization) and authorization.mapping_fingerprint == mapping and authorization.policy_fingerprint == policy and authorization.schema_version == COMMISSIONING_AUTH_SCHEMA_VERSION

    def _fresh_capability(self, snapshot: SolisStateSnapshot, entity_id: str) -> object | None:
        c = snapshot.capabilities
        p = self.config.capability
        return {
            p.battery_max_charge_current_entity_id: c.maximum_charge_current,
            p.battery_max_discharge_current_entity_id: c.maximum_discharge_current,
            p.max_output_power_entity_id: c.maximum_output_power,
            p.max_export_power_entity_id: c.maximum_feed_in_power,
        }.get(entity_id)

    async def async_apply_candidate(
        self,
        observation: SolisStateReadResult,
        *,
        now: datetime,
        reserve_target: Decimal | int | str,
        authorization: EphemeralCandidateAuthorization | PersistentCandidateAuthorization,
        manual_grid_import_verification: ManualGridImportVerification,
        capability_resolutions: Mapping[str, CapabilityResolutionRecord] | None = None,
    ) -> PolicyActuationResult:
        results: list[WriteResult] = []
        if not self._authorization_valid(authorization, now):
            return PolicyActuationResult(PolicyActuationStatus.BLOCKED, issues=("candidate authorization is invalid",))
        if isinstance(authorization, EphemeralCandidateAuthorization):
            if not self.ephemeral_authorizations.consume(authorization, now=now, mapping=self.mapping_fingerprint, policy=self.policy_fingerprint):
                return PolicyActuationResult(PolicyActuationStatus.BLOCKED, issues=("ephemeral authorization is expired, reused, or mismatched",))
        policy = build_candidate_policy(reserve_target)
        if not manual_grid_import_verification.valid(now=now, mapping=self.mapping_fingerprint, policy=self.policy_fingerprint):
            return PolicyActuationResult(PolicyActuationStatus.BLOCKED, issues=("manual grid-import verification is invalid",))
        if not self._guard_off() or not isinstance(observation, SolisStateReadResult) or not observation.is_healthy or observation.snapshot is None:
            return PolicyActuationResult(PolicyActuationStatus.BLOCKED, issues=("healthy observation and exact guard-off are required",))
        try:
            disabled = await self.slot_actuator.async_disable_all()
            results.extend(disabled.results)
            if not disabled.safe:
                raise RuntimeError("all Solis slot directions were not proven disabled")
            p = self.config.persistent
            protection = self.config.protection
            snap = observation.snapshot
            # Protection is deliberately first; every request captures a fresh
            # CAS revision and rechecks the disable guard around the mutation.
            for entity_id, target, capability in (
                (protection.battery_over_discharge_soc_entity_id, Decimal(MINIMUM_SOC_PERCENT), snap.persistent.over_discharge_soc),
                (protection.battery_force_charge_soc_entity_id, Decimal(FORCE_CHARGE_SOC_PERCENT), snap.persistent.force_charge_soc),
                (protection.battery_recovery_soc_entity_id, Decimal(MINIMUM_SOC_PERCENT), snap.persistent.recovery_soc),
                (protection.battery_max_charge_soc_entity_id, Decimal(FULL_SOC_PERCENT), snap.persistent.maximum_charge_soc),
            ):
                await self._candidate_write(self._number_request(entity_id, target, capability), results)
            fresh = await self._refresh(now)
            if fresh is None or not fresh.is_healthy or fresh.snapshot is None:
                raise RuntimeError("fresh Solis capability read is required after protection writes")
            snap = fresh.snapshot
            reserve_cap = snap.persistent.battery_reserve_soc
            if not (reserve_cap.minimum <= policy.battery_reserve_soc <= reserve_cap.maximum and (policy.battery_reserve_soc - reserve_cap.minimum) % reserve_cap.step == 0):
                raise RuntimeError("fresh Battery Reserve SOC capability rejects the requested target")

            for entity_id, capability in (
                (self.config.capability.battery_max_charge_current_entity_id, snap.capabilities.maximum_charge_current),
                (self.config.capability.battery_max_discharge_current_entity_id, snap.capabilities.maximum_discharge_current),
                (self.config.capability.max_output_power_entity_id, snap.capabilities.maximum_output_power),
                (self.config.capability.max_export_power_entity_id, snap.capabilities.maximum_feed_in_power),
            ):
                record = (capability_resolutions or {}).get(entity_id)
                if record is None or capability is None or not record.valid(now=now, mapping=self.mapping_fingerprint, policy=self.policy_fingerprint, unit=capability.unit):
                    continue
                if isinstance(record.verified_target, DocumentedUnlimitedValue):
                    continue
                if not capability.minimum <= record.verified_target <= capability.maximum or (record.verified_target - capability.minimum) % capability.step != 0:
                    raise RuntimeError(f"capability resolution is incompatible with fresh metadata for {entity_id}")
                await self._candidate_write(self._number_request(entity_id, record.verified_target, capability), results)

            await self._candidate_write(SwitchWriteRequest(self._precondition(p.allow_grid_charging_entity_id), True), results)
            await self._candidate_write(SwitchWriteRequest(self._precondition(p.allow_export_entity_id), True), results)
            await self._candidate_write(SwitchWriteRequest(self._precondition(p.grid_peak_shaving_entity_id), True), results)
            await self._candidate_write(self._number_request(protection.battery_reserve_soc_entity_id, policy.battery_reserve_soc, snap.persistent.battery_reserve_soc), results)
            await self._candidate_write(SwitchWriteRequest(self._precondition(protection.battery_reserve_entity_id), True), results)
            # Storage mode is intentionally last among persistent candidate writes.
            await self._candidate_write(SelectWriteRequest(self._precondition(p.storage_mode_entity_id), StorageMode.FEED_IN_PRIORITY.value), results)
            verified = await self._refresh(now)
            if verified is None or not verified.is_healthy or verified.snapshot is None:
                raise RuntimeError("complete candidate readback is unavailable")
            final = verified.snapshot
            if not self._guard_off() or any(slot.charge.enabled or slot.discharge.enabled for slot in final.slots):
                raise RuntimeError("candidate readback did not prove all slots disabled")
            if final.persistent.storage_mode != StorageMode.FEED_IN_PRIORITY.value or not final.persistent.allow_grid_charging or not final.persistent.allow_export or not final.persistent.grid_peak_shaving or not final.persistent.battery_reserve:
                raise RuntimeError("candidate readback does not match the requested persistent policy")
            return PolicyActuationResult(PolicyActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION, candidate_results=tuple(results))
        except asyncio.CancelledError as original:
            task = asyncio.create_task(self._apply_fail_safe_internal())
            await self._await_cleanup(task)
            raise original
        except Exception as exc:
            issues = (str(exc),)
            try:
                fallback, safe = await self._apply_fail_safe_internal()
            except asyncio.CancelledError:
                raise
            except Exception as fallback_exc:
                return PolicyActuationResult(PolicyActuationStatus.FAILED_UNSAFE, tuple(results), issues=issues + (str(fallback_exc),))
            status = PolicyActuationStatus.CANDIDATE_FAILED_FAIL_SAFE_APPLIED if safe else PolicyActuationStatus.FAILED_UNSAFE
            return PolicyActuationResult(status, tuple(results), fallback, issues)

    async def _await_cleanup(self, task: asyncio.Task[object]) -> object:
        current = asyncio.current_task()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if current is not None:
                    current.uncancel()
                continue
        return await task


# Compatibility vocabulary for adapters and tests.
CandidateAuthorization = EphemeralCandidateAuthorization
PersistentAuthorization = PersistentCandidateAuthorization
ManualGridLimitVerification = ManualGridImportVerification
CapabilityVerification = CapabilityResolutionRecord
SolisCandidateActuator = SolisPolicyActuator
CandidatePolicyActuator = SolisPolicyActuator


__all__ = [
    "ACTUATOR_ORDERING_VERSION",
    "CapabilityResolutionRecord",
    "CapabilityVerification",
    "CandidateAuthorization",
    "CandidatePolicyActuator",
    "EphemeralAuthorizationStore",
    "EphemeralCandidateAuthorization",
    "ManualGridImportVerification",
    "ManualGridLimitVerification",
    "PersistentAuthorization",
    "PersistentCandidateAuthorization",
    "PolicyActuationResult",
    "PolicyActuationStatus",
    "SolisCandidateActuator",
    "SolisPolicyActuator",
    "bounded_reserve_soc",
    "build_candidate_policy",
    "build_fail_safe_policy",
    "canonical_policy",
    "canonical_policy_bytes",
    "compute_policy_fingerprint",
    "policy_fingerprint",
]
