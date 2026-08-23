"""Fail-closed transactional actuator for the mapped Solis schedule slots.

This module is deliberately kept at the Home Assistant boundary.  It accepts
the configured entity map and a verified writer, but it does not import Home
Assistant or make a device/cloud request.  Every successful write is verified
against Home Assistant state before the next step is attempted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import Enum
import re

from .contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from .ha_writer import HomeAssistantWriter, WriteTransaction
from .solis_config import SolisConfig, SolisSlotDirectionConfig, SolisSlotOwner
from .solis_state import SolisStateReadResult, SolisStateSnapshot
from .write_contracts import (
    NumberWriteRequest,
    StatePrecondition,
    SwitchWriteRequest,
    TextWriteRequest,
    WriteOutcome,
    WriteResult,
)


def _remaining_deadline(deadline: float) -> float:
    """Return seconds remaining on an absolute monotonic deadline."""

    return max(0.0, deadline - asyncio.get_running_loop().time())


MAXIMUM_INVERTER_CLOCK_SKEW = timedelta(minutes=1)
CANCELLATION_CLEANUP_TIMEOUT = timedelta(seconds=60)

_TIME_TEXT = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class ReentrantAsyncLock:
    """Task-reentrant lock shared by all Solis policy and slot entry points."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0

    async def acquire(self, *, deadline: float | None = None) -> bool:
        task = asyncio.current_task()
        if task is not None and task is self._owner:
            self._depth += 1
            return True
        if deadline is None:
            await self._lock.acquire()
        else:
            remaining = _remaining_deadline(deadline)
            if remaining <= 0:
                raise TimeoutError("Solis orchestration deadline exhausted")
            try:
                await asyncio.wait_for(self._lock.acquire(), remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    "Solis orchestration deadline exhausted"
                ) from None
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
class DisableAllResult:
    """Ordered cleanup evidence; ``safe`` means every switch was proven off."""

    results: tuple[WriteResult, ...]
    safe: bool

class SlotActuationStatus(str, Enum):
    """Outcome of one slot operation."""

    APPLIED = "APPLIED"
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
        return self.status is SlotActuationStatus.FAILED_SAFE

@dataclass(frozen=True, slots=True)
class CancellationDiagnostic:
    """Immutable evidence captured before propagating the original cancellation."""

    original_exception: asyncio.CancelledError
    actuation_results: tuple[WriteResult, ...]
    cleanup_results: tuple[WriteResult, ...]
    all_directions_proven_off: bool


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
            (SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE): (2, SolisSlotOwner.RESERVE_EXPORT),
        }.get((intent.owner, intent.direction))
        if target is None or intent.physical_slot != target[0]:
            return "slot owner and direction are not targetable"
        direction_config = None
        for candidate, physical_slot, direction in self._directions():
            if physical_slot == intent.physical_slot and direction is intent.direction:
                direction_config = candidate
                break
        if direction_config is None or direction_config.owner is not target[1]:
            return "slot owner and direction do not match the configured map"
        direction_state = self._snapshot_direction(snapshot, intent.physical_slot, intent.direction)
        if direction_state is None:
            return "target slot observation is missing"
        if not _capability_accepts(direction_state.current, intent.current):
            return "target current is outside the observed capability"
        if not _capability_accepts(direction_state.target_soc, intent.target_soc):
            return "target SOC is outside the observed capability"
        try:
            # Intent times are aware instants (UTC in the runtime model).  The
            # inverter datetime entity is an observation of the device clock,
            # not the timezone boundary for serialisation: Solis expects the
            # configured inverter wall-clock timezone.
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

    def _existing_intent_is_proven(
        self,
        intent: SlotIntent,
        observation: SolisStateReadResult,
        target_state: object,
        target_config: SolisSlotDirectionConfig,
        schedule: str,
    ) -> bool:
        """Return whether the verified observation already proves this intent.

        This deliberately uses the reader's complete snapshot rather than a
        second set of entity reads.  A healthy observation records the state
        of every direction, so an exact match can safely avoid toggling the
        live schedule on each heartbeat.
        """

        snapshot = observation.snapshot
        if snapshot is None:
            return False
        enabled: list[tuple[int, SlotDirection]] = []
        target = None
        for slot in snapshot.slots:
            for direction_state in (slot.charge, slot.discharge):
                if direction_state.enabled:
                    enabled.append((slot.physical_slot, direction_state.direction))
                if (
                    slot.physical_slot == intent.physical_slot
                    and direction_state.direction is intent.direction
                ):
                    target = direction_state
        if enabled != [(intent.physical_slot, intent.direction)]:
            return False
        if target is None or target is not target_state or not target.enabled:
            return False
        observed_schedule_is_active = target.time_text == schedule
        if not observed_schedule_is_active:
            try:
                observed_start, observed_end = (
                    datetime.strptime(value, "%H:%M").time()
                    for value in target.time_text.split("-")
                )
                desired_end = schedule.split("-")[1]
                desired_end_time = datetime.strptime(desired_end, "%H:%M").time()
                local_now = snapshot.persistent.inverter_time.astimezone(
                    self.inverter_timezone
                ).time()
                observed_start_minutes = observed_start.hour * 60 + observed_start.minute
                observed_end_minutes = observed_end.hour * 60 + observed_end.minute
                now_minutes = local_now.hour * 60 + local_now.minute
                active = (
                    (observed_start_minutes <= now_minutes < observed_end_minutes)
                    if observed_start_minutes < observed_end_minutes
                    else (now_minutes >= observed_start_minutes or now_minutes < observed_end_minutes)
                )
                observed_schedule_is_active = active and observed_end == desired_end_time
            except (TypeError, ValueError):
                observed_schedule_is_active = False
        return (
            observed_schedule_is_active
            and target.current.current_value == intent.current
            and target.target_soc.current_value == intent.target_soc
            and self._guard_off()
        )

    @staticmethod
    def _only_target_is_enabled(
        snapshot: SolisStateSnapshot,
        physical_slot: int,
        direction: SlotDirection,
    ) -> bool:
        """Return whether the target is already the sole active direction."""

        enabled = [
            (slot.physical_slot, state.direction)
            for slot in snapshot.slots
            for state in (slot.charge, slot.discharge)
            if state.enabled
        ]
        return enabled == [(physical_slot, direction)]

    async def _write_recorded(
        self,
        transaction: WriteTransaction,
        request: SwitchWriteRequest | TextWriteRequest | NumberWriteRequest,
        results: list[WriteResult],
        *,
        deadline: float | None = None,
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
            remaining = _remaining_deadline(deadline)
            if remaining <= 0:
                raise TimeoutError("fail-safe deadline exhausted")
            result = await transaction.async_write(request, deadline=deadline)
        results[index] = result
        return result

    async def _disable_all_locked(
        self, transaction: WriteTransaction, results: list[WriteResult],
        deadline: float | None = None,
    ) -> bool:
        for direction, _physical_slot, _kind in self._directions():
            if deadline is not None and _remaining_deadline(deadline) <= 0:
                results.append(_result_failure(direction.enable_entity_id, "fail-safe deadline exhausted"))
                return False
            observed = self._verified_precondition(direction.enable_entity_id)
            if observed is None:
                results.append(_result_failure(direction.enable_entity_id, "unable to capture switch precondition"))
                continue
            if observed.state == "on":
                request = SwitchWriteRequest(observed, False)
                try:
                    await self._write_recorded(
                        transaction,
                        request,
                        results,
                        deadline=deadline,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Continue through every direction.  A failed switch must
                    # not prevent the remaining slots from being made safe.
                    # _write_recorded has already left an uncertain result in
                    # the ordered evidence list.
                    continue
            elif observed.state != "off":
                results.append(_result_failure(direction.enable_entity_id, "enable state is invalid"))
        return self._prove_all_off()

    async def _disable_all_once(
        self,
        results: list[WriteResult] | None = None,
        deadline: float | None = None,
    ) -> DisableAllResult:
        sink = results if results is not None else []
        attempt_start = len(sink)
        proven = False
        transaction = self.writer.transaction(deadline=deadline)
        entered = False
        try:
            if deadline is None:
                await transaction.__aenter__()
            else:
                remaining = _remaining_deadline(deadline)
                if remaining <= 0:
                    raise TimeoutError("fail-safe deadline exhausted before writer transaction")
                await transaction.__aenter__()
            entered = True
            proven = await self._disable_all_locked(transaction, sink, deadline)
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
        """Disable every direction, independently of controller health."""

        async with self.orchestration_lock:
            self.last_cancellation_result = None
            partial_results: list[WriteResult] = []
            try:
                return await self._disable_all_once(partial_results)
            except asyncio.CancelledError as original:
                await self._record_cancelled_cleanup(original, partial_results)
                raise original

    async def _cleanup_after_failure(
        self, results: list[WriteResult], *, deadline: float | None = None
    ) -> bool:
        cleanup = await self._disable_all_once(results, deadline=deadline)
        return cleanup.safe

    async def _record_cancelled_cleanup(
        self,
        original: asyncio.CancelledError,
        actuation_results: list[WriteResult],
    ) -> None:
        cleanup_results: list[WriteResult] = []
        deadline = (
            asyncio.get_running_loop().time()
            + CANCELLATION_CLEANUP_TIMEOUT.total_seconds()
        )
        cleanup_task = asyncio.create_task(
            self._disable_all_once(cleanup_results, deadline=deadline)
        )
        cleanup = await _await_cleanup(cleanup_task, deadline=deadline)
        self.last_cancellation_result = CancellationDiagnostic(
            original_exception=original,
            actuation_results=tuple(actuation_results),
            cleanup_results=tuple(cleanup_results),
            all_directions_proven_off=cleanup is not None and cleanup.safe,
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
        *,
        deadline: float | None = None,
    ) -> SlotActuationResult:
        try:
            preflight = self._preflight(intent, observation, now)
            if isinstance(preflight, str):
                safe = await self._cleanup_after_failure(results, deadline=deadline)
                status = SlotActuationStatus.FAILED_SAFE if safe else SlotActuationStatus.FAILED_UNSAFE
                return SlotActuationResult(status, tuple(results), message=preflight)
            target_config, target_state, schedule = preflight
            mandatory_disable_deadline = min(intent.end, intent.expiry)
            if self._existing_intent_is_proven(
                intent, observation, target_state, target_config, schedule
            ):
                return SlotActuationResult(
                    SlotActuationStatus.APPLIED,
                    tuple(results),
                    mandatory_disable_deadline=mandatory_disable_deadline,
                    message="slot already enabled and Home Assistant readback verified",
                )
            async with self.writer.transaction(deadline=deadline) as transaction:
                preserve_target = (
                    observation.snapshot is not None
                    and target_state.enabled
                    and self._only_target_is_enabled(
                        observation.snapshot, intent.physical_slot, intent.direction
                    )
                )
                # All twelve switch preconditions are captured immediately
                # before their individual CAS writes.
                result_count = len(results)
                proven = preserve_target or await self._disable_all_locked(
                    transaction, results, deadline=deadline
                )
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
                for request, current in (
                    (time_request, target_state.time_text),
                    (current_request, target_state.current.current_value),
                    (soc_request, target_state.target_soc.current_value),
                ):
                    if current == request.target:
                        continue
                    write_result = await self._write_recorded(
                        transaction, request, results, deadline=deadline
                    )
                    if not write_result.success:
                        raise RuntimeError(f"configuration write failed: {write_result.message}")
                if not preserve_target and not self._prove_all_off():
                    raise RuntimeError("slot direction changed while target configuration was written")
                if not self._guard_off():
                    raise RuntimeError("control-disable guard asserted before slot enable")
                enable_precondition = self._require_verified_precondition(target_config.enable_entity_id)
                if enable_precondition.state != "on":
                    enable_request = SwitchWriteRequest(enable_precondition, True)
                    write_result = await self._write_recorded(
                        transaction, enable_request, results, deadline=deadline
                    )
                    if not write_result.success:
                        raise RuntimeError(f"slot enable failed: {write_result.message}")
                if not self._guard_off():
                    raise RuntimeError("control-disable guard asserted after slot enable")
                if not self._prove_exact_target(target_config.enable_entity_id):
                    raise RuntimeError("final slot enable proof failed")
            return SlotActuationResult(
                SlotActuationStatus.APPLIED,
                tuple(results),
                mandatory_disable_deadline=mandatory_disable_deadline,
                message="slot enabled and Home Assistant readback verified",
            )
        except Exception as exc:
            safe = await self._cleanup_after_failure(results, deadline=deadline)
            status = SlotActuationStatus.FAILED_SAFE if safe else SlotActuationStatus.FAILED_UNSAFE
            return SlotActuationResult(status, tuple(results), message=str(exc))

    def _require_verified_precondition(self, entity_id: str) -> StatePrecondition:
        observed = self._verified_precondition(entity_id)
        if observed is None:
            raise RuntimeError(f"unable to capture verified state for {entity_id}")
        return observed


async def _await_cleanup(
    task: asyncio.Task[DisableAllResult], *, deadline: float
) -> DisableAllResult | None:
    """Wait through repeated cancellation, but never past the hard deadline."""

    current = asyncio.current_task()
    while not task.done():
        remaining = _remaining_deadline(deadline)
        if remaining <= 0:
            break
        try:
            done, _pending = await asyncio.wait((task,), timeout=remaining)
            if not done:
                break
        except asyncio.CancelledError:
            if current is not None:
                while current.cancelling():
                    current.uncancel()
    if not task.done():
        task.cancel()
        task.add_done_callback(_consume_cleanup_task)
        return None
    try:
        return task.result()
    except BaseException:
        return None


def _consume_cleanup_task(task: asyncio.Task[DisableAllResult]) -> None:
    """Consume a cleanup task detached at its hard deadline."""

    try:
        task.result()
    except BaseException:
        pass


__all__ = [
    "CancellationDiagnostic",
    "DisableAllResult",
    "MAXIMUM_INVERTER_CLOCK_SKEW",
    "ReentrantAsyncLock",
    "SlotActuationResult",
    "SlotActuationStatus",
    "SolisSlotActuator",
    "encode_schedule",
]
