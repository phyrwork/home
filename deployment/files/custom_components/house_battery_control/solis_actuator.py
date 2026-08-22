"""Fail-closed transactional actuator for the mapped Solis schedule slots.

This module is deliberately kept at the Home Assistant boundary.  It accepts
the immutable configuration and a verified writer, but it does not import
Home Assistant or make a device/cloud request.  A Home Assistant write is
treated as an acknowledgement only; device reconciliation is a separate
commissioning concern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re

from .contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from .domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from .ha_writer import HomeAssistantWriter, WriteTransaction
from .solis_config import (
    BatteryPowerSign,
    SolisConfig,
    SolisSlotDirectionConfig,
    SolisSlotOwner,
)
from .solis_state import SolisStateReadResult, SolisStateSnapshot
from .write_contracts import (
    NumberWriteRequest,
    StatePrecondition,
    SwitchWriteRequest,
    TextWriteRequest,
    WriteOutcome,
    WriteResult,
)


def _remaining_deadline(
    deadline: datetime,
    clock: Callable[[], datetime] | None = None,
) -> float:
    """Return remaining seconds for an absolute, timezone-aware deadline."""

    now = clock() if clock is not None else datetime.now(timezone.utc)
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("fail-safe deadline must be timezone-aware")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fail-safe clock must be timezone-aware")
    return max(0.0, (deadline - now).total_seconds())


MAXIMUM_INVERTER_CLOCK_SKEW = timedelta(minutes=1)
COMMISSIONING_SCHEMA_VERSION = 1
MAPPING_FINGERPRINT_SCHEMA_VERSION = 1

_TIME_TEXT = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class ReentrantAsyncLock:
    """Task-reentrant lock shared by all Solis policy and slot entry points."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0

    async def acquire(self) -> bool:
        task = asyncio.current_task()
        if task is not None and task is self._owner:
            self._depth += 1
            return True
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return True

    def release(self) -> None:
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("Solis orchestration lock released by non-owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "ReentrantAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class CommissioningRecord:
    """The persisted proof that this exact entity map was commissioned."""

    commissioned_at: datetime
    mapping_fingerprint: str
    ha_readback_validated: bool
    device_reconciliation_validated: bool
    schema_version: int = COMMISSIONING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.commissioned_at.tzinfo is None or self.commissioned_at.utcoffset() is None:
            raise ValueError("commissioned_at must be timezone-aware")
        if not isinstance(self.mapping_fingerprint, str) or not self.mapping_fingerprint:
            raise ValueError("mapping_fingerprint must not be empty")
        if not isinstance(self.ha_readback_validated, bool):
            raise TypeError("ha_readback_validated must be bool")
        if not isinstance(self.device_reconciliation_validated, bool):
            raise TypeError("device_reconciliation_validated must be bool")
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise TypeError("schema_version must be an integer")


@dataclass(frozen=True, slots=True)
class DisableAllResult:
    """Ordered cleanup evidence; ``safe`` means every switch was proven off."""

    results: tuple[WriteResult, ...]
    safe: bool

    @property
    def proven_off(self) -> bool:
        return self.safe


class SlotActuationStatus(str, Enum):
    """String statuses intentionally kept free of HA-specific enums."""

    BLOCKED_UNCOMMISSIONED_SAFE = "BLOCKED_UNCOMMISSIONED_SAFE"
    BLOCKED_UNCOMMISSIONED_UNSAFE = "BLOCKED_UNCOMMISSIONED_UNSAFE"
    APPLIED_HA_PENDING_DEVICE_RECONCILIATION = "APPLIED_HA_PENDING_DEVICE_RECONCILIATION"
    FAILED_SAFE = "FAILED_SAFE"
    FAILED_UNSAFE = "FAILED_UNSAFE"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SlotActuationResult:
    status: SlotActuationStatus
    results: tuple[WriteResult, ...] = ()
    mandatory_disable_deadline: datetime | None = None
    message: str = ""

    @property
    def safe(self) -> bool:
        return self.status in {
            SlotActuationStatus.BLOCKED_UNCOMMISSIONED_SAFE,
            SlotActuationStatus.FAILED_SAFE,
        }

    @property
    def pending_device_reconciliation(self) -> bool:
        return self.status == SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION

    @property
    def ordered_results(self) -> tuple[WriteResult, ...]:
        return self.results


@dataclass(frozen=True, slots=True)
class CancellationDiagnostic:
    """Immutable evidence captured before propagating the original cancellation."""

    original_exception: asyncio.CancelledError
    actuation_results: tuple[WriteResult, ...]
    cleanup_results: tuple[WriteResult, ...]
    all_directions_proven_off: bool


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _direction_payload(direction: SolisSlotDirectionConfig) -> dict[str, object]:
    return {
        "enable_entity_id": direction.enable_entity_id,
        "time_entity_id": direction.time_entity_id,
        "current_entity_id": direction.current_entity_id,
        "target_soc_entity_id": direction.target_soc_entity_id,
        "owner": _enum_value(direction.owner),
    }


def canonical_mapping(config: SolisConfig) -> bytes:
    """Return the canonical bytes covered by the commissioning fingerprint."""

    slots: list[dict[str, object]] = []
    for slot in sorted(config.slots, key=lambda item: item.physical_slot):
        slots.append(
            {
                "physical_slot": slot.physical_slot,
                "charge": _direction_payload(slot.charge),
                "discharge": _direction_payload(slot.discharge),
            }
        )
    data: dict[str, object] = {
        "fingerprint_schema_version": MAPPING_FINGERPRINT_SCHEMA_VERSION,
        "telemetry": {
            "state_of_charge_entity_id": config.telemetry.state_of_charge_entity_id,
            "battery_power_entity_id": config.telemetry.battery_power_entity_id,
            "battery_power_sign": (
                config.telemetry.battery_power_sign.value
                if isinstance(config.telemetry.battery_power_sign, BatteryPowerSign)
                else None
            ),
            "device_timestamp_entity_id": config.telemetry.device_timestamp_entity_id,
        },
        "persistent": {
            "storage_mode_entity_id": config.persistent.storage_mode_entity_id,
            "allow_grid_charging_entity_id": config.persistent.allow_grid_charging_entity_id,
            "allow_export_entity_id": config.persistent.allow_export_entity_id,
            "grid_peak_shaving_entity_id": config.persistent.grid_peak_shaving_entity_id,
            "inverter_on_off_entity_id": config.persistent.inverter_on_off_entity_id,
            "inverter_time_entity_id": config.persistent.inverter_time_entity_id,
        },
        "protection": {
            "battery_over_discharge_soc_entity_id": config.protection.battery_over_discharge_soc_entity_id,
            "battery_force_charge_soc_entity_id": config.protection.battery_force_charge_soc_entity_id,
            "battery_recovery_soc_entity_id": config.protection.battery_recovery_soc_entity_id,
            "battery_max_charge_soc_entity_id": config.protection.battery_max_charge_soc_entity_id,
            "battery_reserve_entity_id": config.protection.battery_reserve_entity_id,
            "battery_reserve_soc_entity_id": config.protection.battery_reserve_soc_entity_id,
        },
        "capability": {
            "battery_max_charge_current_entity_id": config.capability.battery_max_charge_current_entity_id,
            "battery_max_discharge_current_entity_id": config.capability.battery_max_discharge_current_entity_id,
            "max_output_power_entity_id": config.capability.max_output_power_entity_id,
            "max_export_power_entity_id": config.capability.max_export_power_entity_id,
        },
        "maximum_grid_import_policy": {
            "mode": _enum_value(config.maximum_grid_import_policy),
            "maximum_grid_import_power_kw": _canonical_decimal(MAXIMUM_GRID_IMPORT_POWER_KW),
        },
        "slots": slots,
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("fingerprint policy Decimal must be finite")
    return format(value, "f")


def mapping_fingerprint(config: SolisConfig) -> str:
    return hashlib.sha256(canonical_mapping(config)).hexdigest()


def _result_failure(entity_id: str, message: str) -> WriteResult:
    return WriteResult(entity_id, WriteOutcome.REJECTED, message)


def _capability_accepts(capability: object, target: Decimal) -> bool:
    minimum = getattr(capability, "minimum", None)
    maximum = getattr(capability, "maximum", None)
    step = getattr(capability, "step", None)
    if not all(isinstance(value, Decimal) for value in (minimum, maximum, step)):
        return False
    return minimum <= target <= maximum and (target - minimum) % step == 0


def _local_time_valid(value: datetime, zone: tzinfo) -> bool:
    """Reject nonexistent and ambiguous local wall-clock times."""

    local = value.astimezone(zone)
    naive = local.replace(tzinfo=None)
    candidates = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    round_trips = [candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) for candidate in candidates]
    if any(round_trip != naive for round_trip in round_trips):
        return False
    return candidates[0].utcoffset() == candidates[1].utcoffset()


def encode_schedule(start: datetime, end: datetime, inverter_timezone: tzinfo) -> str:
    """Encode an aware interval in Solis local wall-clock notation."""

    if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("schedule datetimes must be timezone-aware")
    if end <= start or end - start >= timedelta(days=1):
        raise ValueError("schedule duration must be positive and less than 24 hours")
    local_start = start.astimezone(inverter_timezone)
    local_end = end.astimezone(inverter_timezone)
    if not _local_time_valid(local_start, inverter_timezone) or not _local_time_valid(local_end, inverter_timezone):
        raise ValueError("schedule local time is ambiguous or nonexistent")
    if local_start.utcoffset() != local_end.utcoffset():
        raise ValueError("schedule interval crosses a daylight-saving transition")
    if local_start.second or local_start.microsecond or local_end.second or local_end.microsecond:
        raise ValueError("schedule must be representable to exact minutes")
    encoded_start = local_start.strftime("%H:%M")
    encoded_end = local_end.strftime("%H:%M")
    if encoded_start == encoded_end:
        raise ValueError("enabled schedule cannot have equal start and end")
    text = f"{encoded_start}-{encoded_end}"
    if not _TIME_TEXT.fullmatch(text):
        raise ValueError("schedule has invalid Solis wall-clock representation")
    return text


class SolisSlotActuator:
    """Apply exactly one active Solis slot intent under a fail-closed gate."""

    def __init__(
        self,
        config: SolisConfig,
        writer: HomeAssistantWriter,
        *,
        control_disable_guard_entity_id: str,
        inverter_timezone: tzinfo,
        orchestration_lock: ReentrantAsyncLock | None = None,
    ) -> None:
        if not control_disable_guard_entity_id.startswith("input_boolean.") and not control_disable_guard_entity_id.startswith("switch."):
            raise ValueError("control-disable guard must be a switch-like entity")
        if not isinstance(inverter_timezone, tzinfo):
            raise TypeError("inverter_timezone must be explicit")
        self.config = config
        self.writer = writer
        self.control_disable_guard_entity_id = control_disable_guard_entity_id
        self.inverter_timezone = inverter_timezone
        self.last_cancellation_result: CancellationDiagnostic | None = None
        self.orchestration_lock = orchestration_lock or ReentrantAsyncLock()

    def _directions(self) -> tuple[tuple[SolisSlotDirectionConfig, int, SlotDirection], ...]:
        result: list[tuple[SolisSlotDirectionConfig, int, SlotDirection]] = []
        for slot in sorted(self.config.slots, key=lambda item: item.physical_slot):
            result.extend(((slot.charge, slot.physical_slot, SlotDirection.CHARGE), (slot.discharge, slot.physical_slot, SlotDirection.DISCHARGE)))
        return tuple(result)

    def _snapshot_direction(self, snapshot: SolisStateSnapshot, physical_slot: int, direction: SlotDirection):
        for slot in snapshot.slots:
            if slot.physical_slot == physical_slot:
                return slot.charge if direction is SlotDirection.CHARGE else slot.discharge
        return None

    def _preflight(
        self, intent: SlotIntent, observation: SolisStateReadResult, now: datetime
    ) -> tuple[SolisSlotDirectionConfig, object, str] | str:
        if now.tzinfo is None or now.utcoffset() is None:
            return "now must be timezone-aware"
        if not isinstance(observation, SolisStateReadResult):
            return "healthy Solis snapshot is required"
        if observation.health is not ControllerHealth.HEALTHY or observation.snapshot is None:
            return "healthy Solis snapshot is required"
        snapshot = observation.snapshot
        if snapshot.persistent.inverter_time.tzinfo is None or snapshot.persistent.inverter_time.utcoffset() is None:
            return "inverter clock must be timezone-aware"
        if abs(now - snapshot.persistent.inverter_time) > MAXIMUM_INVERTER_CLOCK_SKEW:
            return "inverter clock exceeds allowed skew"
        if snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
            return "snapshot observation time must be timezone-aware"
        target = {
            (SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE): (1, SolisSlotOwner.CHEAP_CHARGING),
            (SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE): (1, SolisSlotOwner.FULL_SOC_CYCLING),
            (SlotOwner.PRE_DISCHARGE, SlotDirection.DISCHARGE): (2, SolisSlotOwner.PRE_DISCHARGE),
        }.get((intent.owner, intent.direction))
        if target is None or intent.physical_slot != target[0]:
            return "slot owner and direction are not targetable"
        direction_config = None
        for candidate, physical_slot, direction in self._directions():
            if physical_slot == intent.physical_slot and direction is intent.direction:
                direction_config = candidate
                break
        if direction_config is None or direction_config.owner is not target[1]:
            return "slot owner and direction do not match the commissioned map"
        direction_state = self._snapshot_direction(snapshot, intent.physical_slot, intent.direction)
        if direction_state is None:
            return "target slot observation is missing"
        if not _capability_accepts(direction_state.current, intent.current):
            return "target current is outside the observed capability"
        if not _capability_accepts(direction_state.target_soc, intent.target_soc):
            return "target SOC is outside the observed capability"
        try:
            schedule = encode_schedule(intent.start, intent.end, self.inverter_timezone)
        except ValueError as exc:
            return str(exc)
        effective_end = min(intent.end, intent.expiry)
        if not intent.start <= now < effective_end:
            return "slot intent is not active"
        if not self._guard_off():
            return "control-disable guard is asserted or unavailable"
        return direction_config, direction_state, schedule

    def _verified_precondition(self, entity_id: str) -> StatePrecondition | None:
        """Capture one public, revision-bearing state suitable for proof."""

        try:
            precondition = self.writer.capture_precondition(entity_id)
        except Exception:
            return None
        if not isinstance(precondition.context_id, str) or not precondition.context_id:
            return None
        return precondition

    def _guard_off(self) -> bool:
        observed = self._verified_precondition(self.control_disable_guard_entity_id)
        return observed is not None and observed.state == "off"

    def _prove_all_off(self) -> bool:
        for direction, _physical_slot, _kind in self._directions():
            observed = self._verified_precondition(direction.enable_entity_id)
            if observed is None or observed.state != "off":
                return False
        return True

    def _prove_exact_target(self, target_entity_id: str) -> bool:
        for direction, _physical_slot, _kind in self._directions():
            observed = self._verified_precondition(direction.enable_entity_id)
            expected = "on" if direction.enable_entity_id == target_entity_id else "off"
            if observed is None or observed.state != expected:
                return False
        return True

    async def _write_recorded(
        self,
        transaction: WriteTransaction,
        request: SwitchWriteRequest | TextWriteRequest | NumberWriteRequest,
        results: list[WriteResult],
        *,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> WriteResult:
        """Retain an ordered uncertain marker if cancellation interrupts a write."""

        index = len(results)
        results.append(
            _result_failure(
                request.entity_id,
                "write outcome is uncertain because the operation was interrupted",
            )
        )
        if deadline is None:
            result = await transaction.async_write(request)
        else:
            remaining = _remaining_deadline(deadline, clock)
            if remaining <= 0:
                raise TimeoutError("fail-safe deadline exhausted")
            result = await asyncio.wait_for(
                transaction.async_write(request), remaining
            )
        results[index] = result
        return result

    async def _disable_all_locked(
        self, transaction: WriteTransaction, results: list[WriteResult],
        deadline: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> bool:
        for direction, _physical_slot, _kind in self._directions():
            if deadline is not None and _remaining_deadline(deadline, clock) <= 0:
                results.append(_result_failure(direction.enable_entity_id, "fail-safe deadline exhausted"))
                return False
            observed = self._verified_precondition(direction.enable_entity_id)
            if observed is None:
                results.append(_result_failure(direction.enable_entity_id, "unable to capture switch precondition"))
                continue
            if observed.state == "on":
                request = SwitchWriteRequest(observed, False)
                await self._write_recorded(
                    transaction,
                    request,
                    results,
                    deadline=deadline,
                    clock=clock,
                )
            elif observed.state != "off":
                results.append(_result_failure(direction.enable_entity_id, "enable state is invalid"))
        return self._prove_all_off()

    async def _disable_all_once(
        self,
        results: list[WriteResult] | None = None,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> DisableAllResult:
        sink = results if results is not None else []
        attempt_start = len(sink)
        proven = False
        transaction = self.writer.transaction()
        entered = False
        try:
            if deadline is None:
                await transaction.__aenter__()
            else:
                remaining = _remaining_deadline(deadline, clock)
                if remaining <= 0:
                    raise TimeoutError("fail-safe deadline exhausted before writer transaction")
                await asyncio.wait_for(transaction.__aenter__(), remaining)
            entered = True
            proven = await self._disable_all_locked(
                transaction, sink, deadline, clock
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sink.append(_result_failure("solis.slot", f"disable-all transaction failed: {exc}"))
            proven = False
        finally:
            if entered:
                await transaction.__aexit__(None, None, None)
        attempt_results = tuple(sink[attempt_start:])
        return DisableAllResult(
            attempt_results,
            proven and all(result.success for result in attempt_results),
        )

    async def async_disable_all(self) -> DisableAllResult:
        """Disable every direction, independently of health or commissioning."""

        async with self.orchestration_lock:
            self.last_cancellation_result = None
            partial_results: list[WriteResult] = []
            try:
                return await self._disable_all_once(partial_results)
            except asyncio.CancelledError as original:
                await self._record_cancelled_cleanup(original, partial_results)
                raise original

    async def _cleanup_after_failure(self, results: list[WriteResult]) -> bool:
        cleanup = await self._disable_all_once(results)
        return cleanup.safe

    async def _record_cancelled_cleanup(
        self,
        original: asyncio.CancelledError,
        actuation_results: list[WriteResult],
    ) -> None:
        cleanup_results: list[WriteResult] = []
        cleanup_task = asyncio.create_task(self._disable_all_once(cleanup_results))
        cleanup = await _await_cleanup(cleanup_task)
        self.last_cancellation_result = CancellationDiagnostic(
            original_exception=original,
            actuation_results=tuple(actuation_results),
            cleanup_results=tuple(cleanup_results),
            all_directions_proven_off=cleanup.safe,
        )

    async def async_apply_intent(
        self,
        intent: SlotIntent,
        observation: SolisStateReadResult,
        *,
        now: datetime,
    ) -> SlotActuationResult:
        """Apply one active intent or prove the system safe after failure."""

        async with self.orchestration_lock:
            self.last_cancellation_result = None
            results: list[WriteResult] = []
            try:
                return await self._async_apply_intent(intent, observation, now, results)
            except asyncio.CancelledError as original:
                await self._record_cancelled_cleanup(original, results)
                raise original

    async def _async_apply_intent(
        self,
        intent: SlotIntent,
        observation: SolisStateReadResult,
        now: datetime,
        results: list[WriteResult],
    ) -> SlotActuationResult:
        try:
            preflight = self._preflight(intent, observation, now)
            if isinstance(preflight, str):
                safe = await self._cleanup_after_failure(results)
                status = SlotActuationStatus.FAILED_SAFE if safe else SlotActuationStatus.FAILED_UNSAFE
                return SlotActuationResult(status, tuple(results), message=preflight)
            target_config, target_state, schedule = preflight
            deadline = min(intent.end, intent.expiry)
            async with self.writer.transaction() as transaction:
                # All twelve switch preconditions are captured immediately
                # before their individual CAS writes.
                result_count = len(results)
                proven = await self._disable_all_locked(transaction, results)
                disable_results = results[result_count:]
                if not proven or not all(result.success for result in disable_results):
                    raise RuntimeError("all Solis slot directions were not proven disabled")
                time_request = TextWriteRequest(
                    self._require_verified_precondition(target_config.time_entity_id),
                    schedule,
                    text_validator=lambda value: _TIME_TEXT.fullmatch(value) is not None and value.split("-")[0] != value.split("-")[1],
                )
                current_request = NumberWriteRequest(
                    self._require_verified_precondition(target_config.current_entity_id),
                    intent.current,
                    capability=target_state.current,
                )
                soc_request = NumberWriteRequest(
                    self._require_verified_precondition(target_config.target_soc_entity_id),
                    intent.target_soc,
                    capability=target_state.target_soc,
                )
                for request in (time_request, current_request, soc_request):
                    write_result = await self._write_recorded(transaction, request, results)
                    if not write_result.success:
                        raise RuntimeError(f"configuration write failed: {write_result.message}")
                if not self._prove_all_off():
                    raise RuntimeError("slot direction changed while target configuration was written")
                if not self._guard_off():
                    raise RuntimeError("control-disable guard asserted before slot enable")
                enable_request = SwitchWriteRequest(
                    self._require_verified_precondition(target_config.enable_entity_id), True
                )
                write_result = await self._write_recorded(transaction, enable_request, results)
                if not write_result.success:
                    raise RuntimeError(f"slot enable failed: {write_result.message}")
                if not self._guard_off():
                    raise RuntimeError("control-disable guard asserted after slot enable")
                if not self._prove_exact_target(target_config.enable_entity_id):
                    raise RuntimeError("final slot enable proof failed")
            return SlotActuationResult(
                SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION,
                tuple(results),
                mandatory_disable_deadline=deadline,
                message="Home Assistant readback confirmed; device reconciliation remains pending",
            )
        except Exception as exc:
            safe = await self._cleanup_after_failure(results)
            status = SlotActuationStatus.FAILED_SAFE if safe else SlotActuationStatus.FAILED_UNSAFE
            return SlotActuationResult(status, tuple(results), message=str(exc))

    def _require_verified_precondition(self, entity_id: str) -> StatePrecondition:
        observed = self._verified_precondition(entity_id)
        if observed is None:
            raise RuntimeError(f"unable to capture verified state for {entity_id}")
        return observed


async def _await_cleanup(task: asyncio.Task[object]) -> object:
    """Shield cleanup through repeated cancellation delivery."""

    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if current is not None:
                current.uncancel()
            continue
    return await task


# Names used by later adapters and tests.
SolisSlotActuation = SolisSlotActuator
MappingCommissioningRecord = CommissioningRecord
canonical_mapping_bytes = canonical_mapping
compute_mapping_fingerprint = mapping_fingerprint


__all__ = [
    "COMMISSIONING_SCHEMA_VERSION",
    "CancellationDiagnostic",
    "CommissioningRecord",
    "DisableAllResult",
    "MAXIMUM_INVERTER_CLOCK_SKEW",
    "MAPPING_FINGERPRINT_SCHEMA_VERSION",
    "ReentrantAsyncLock",
    "SlotActuationResult",
    "SlotActuationStatus",
    "SolisSlotActuator",
    "SolisSlotActuation",
    "canonical_mapping",
    "canonical_mapping_bytes",
    "compute_mapping_fingerprint",
    "encode_schedule",
    "mapping_fingerprint",
]
