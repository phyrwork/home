"""Verified Solis fail-safe and healthy-runtime writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal, ROUND_CEILING

from .contracts import ControllerHealth, SlotDirection, SlotIntent, StorageMode
from .domain_constants import MINIMUM_SOC_PERCENT
from .ha_writer import HomeAssistantWriter
from .solis_actuator import (
    ReentrantAsyncLock,
    SlotActuationResult,
    SlotActuationStatus,
    SolisSlotActuator,
)
from .solis_config import SolisConfig
from .solis_state import SolisStateReadResult
from .write_contracts import (
    NumberWriteRequest,
    SelectWriteRequest,
    SwitchWriteRequest,
    WriteOutcome,
    WriteResult,
)


SAFE_BASELINE_TIMEOUT = timedelta(seconds=60)
SAFE_BASELINE_RETRY_DELAY_SECONDS = 0.25
_RETRYABLE_BASELINE_OUTCOMES = frozenset(
    (WriteOutcome.SERVICE_TIMEOUT, WriteOutcome.READBACK_TIMEOUT)
)


@dataclass(frozen=True, slots=True)
class PolicyActuationResult:
    """Policy outcome; ``safe`` is HA readback proof, never device proof."""

    success: bool
    safe: bool
    message: str
    results: tuple[WriteResult, ...] = ()
    slot_result: SlotActuationResult | None = None


class SolisPolicyActuator:
    """Own the only production path that mutates Solis controls."""

    def __init__(
        self,
        config: SolisConfig,
        writer: HomeAssistantWriter,
        *,
        control_disable_guard_entity_id: str,
        inverter_timezone: tzinfo,
    ) -> None:
        self.config = config
        self.writer = writer
        self.control_disable_guard_entity_id = control_disable_guard_entity_id
        self._lock = ReentrantAsyncLock()
        self.slots = SolisSlotActuator(
            config,
            writer,
            control_disable_guard_entity_id=control_disable_guard_entity_id,
            inverter_timezone=inverter_timezone,
            orchestration_lock=self._lock,
        )

    @staticmethod
    def _new_safe_deadline() -> float:
        return (
            asyncio.get_running_loop().time()
            + SAFE_BASELINE_TIMEOUT.total_seconds()
        )

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    async def _apply_safe_baseline_once_locked(
        self, *, deadline: float
    ) -> PolicyActuationResult:
        """Run one complete HA-only baseline attempt under the policy lock."""

        results: list[WriteResult] = []
        try:
            disabled = await self.slots._disable_all_once(
                results, deadline=deadline
            )
            persistent = self.config.persistent
            protection = self.config.protection
            async with self.writer.transaction(deadline=deadline) as transaction:
                for request in (
                    SelectWriteRequest(self.writer.capture_precondition(persistent.storage_mode_entity_id), StorageMode.SELF_USE.value),
                    SwitchWriteRequest(self.writer.capture_precondition(persistent.grid_peak_shaving_entity_id), True),
                    SwitchWriteRequest(self.writer.capture_precondition(protection.battery_reserve_entity_id), False),
                ):
                    # Continue through every persistent control after a normal
                    # rejection so the returned proof includes all controls.
                    results.append(
                        await transaction.async_write(request, deadline=deadline)
                    )
            safe = disabled.safe and all(result.success for result in results)
            return PolicyActuationResult(
                safe,
                safe,
                (
                    "safe baseline proven by Home Assistant state"
                    if safe
                    else "safe baseline HA readback incomplete"
                ),
                tuple(results),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return PolicyActuationResult(False, False, f"safe baseline failed: {exc}", tuple(results))

    @staticmethod
    def _clear_cancellation() -> None:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()

    @staticmethod
    def _baseline_retryable(result: PolicyActuationResult) -> bool:
        failures = tuple(item for item in result.results if not item.success)
        return bool(failures) and all(
            item.outcome in _RETRYABLE_BASELINE_OUTCOMES for item in failures
        )

    async def _apply_safe_baseline_attempts_locked(
        self, *, deadline: float
    ) -> PolicyActuationResult:
        first = await self._apply_safe_baseline_once_locked(deadline=deadline)
        if first.safe or not self._baseline_retryable(first):
            return first
        remaining = self._remaining(deadline)
        if remaining < SAFE_BASELINE_RETRY_DELAY_SECONDS:
            return first
        try:
            # The delay is part of the one absolute budget.  In particular,
            # do not start a retry merely because there is some time left and
            # then let the sleep carry the operation past its deadline.
            await asyncio.wait_for(
                asyncio.sleep(SAFE_BASELINE_RETRY_DELAY_SECONDS),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return first
        if self._remaining(deadline) <= 0:
            return first
        second = await self._apply_safe_baseline_once_locked(deadline=deadline)
        return PolicyActuationResult(
            second.success,
            second.safe,
            (
                "safe baseline proven by HA state after one timeout retry"
                if second.safe
                else "safe baseline HA readback incomplete after one timeout retry"
            ),
            first.results + second.results,
            second.slot_result,
        )

    async def _apply_safe_baseline_with_lock(
        self, *, deadline: float
    ) -> PolicyActuationResult:
        acquired = False
        try:
            await self._lock.acquire(deadline=deadline)
            acquired = True
            return await self._apply_safe_baseline_attempts_locked(
                deadline=deadline
            )
        finally:
            if acquired:
                self._lock.release()

    async def _finish_safe_baseline_after_cancellation(
        self, *, deadline: float
    ) -> PolicyActuationResult:
        """Finish one bounded cleanup while preserving the first cancellation."""

        self._clear_cancellation()
        cleanup = asyncio.create_task(
            self._apply_safe_baseline_with_lock(deadline=deadline)
        )
        while not cleanup.done():
            remaining = self._remaining(deadline)
            if remaining <= 0:
                cleanup.cancel()
                break
            try:
                await asyncio.wait_for(asyncio.shield(cleanup), remaining)
            except asyncio.CancelledError:
                self._clear_cancellation()
            except asyncio.TimeoutError:
                cleanup.cancel()
                break
        if not cleanup.done():
            # Cancellation of a child is itself asynchronous.  Give it one
            # scheduling turn to release locks, but never await it without a
            # deadline.  The done callback below consumes any late result.
            cleanup.cancel()
            cleanup.add_done_callback(self._consume_task)
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                # Preserve the original cancellation owned by the caller;
                # this additional delivery must not extend cleanup.
                self._clear_cancellation()
            return PolicyActuationResult(
                False, False, "safe baseline cleanup deadline exhausted"
            )
        try:
            return cleanup.result()
        except asyncio.CancelledError:
            return PolicyActuationResult(
                False, False, "safe baseline cleanup deadline exhausted"
            )
        except Exception as exc:
            return PolicyActuationResult(
                False, False, f"safe baseline cleanup failed: {exc}"
            )

    @staticmethod
    def _consume_task(task: asyncio.Task[object]) -> None:
        """Consume a detached bounded-cleanup task without leaking warnings."""

        try:
            task.result()
        except BaseException:
            pass

    async def async_apply_safe_baseline(self) -> PolicyActuationResult:
        """Apply at most two HA-proven baseline attempts within one minute."""

        deadline = self._new_safe_deadline()
        try:
            return await self._apply_safe_baseline_with_lock(deadline=deadline)
        except asyncio.CancelledError as original:
            await self._finish_safe_baseline_after_cancellation(
                deadline=deadline
            )
            raise original
        except Exception as exc:
            return PolicyActuationResult(
                False, False, f"safe baseline failed: {exc}"
            )

    async def async_apply_healthy(
        self,
        *,
        observation: SolisStateReadResult,
        reserve_soc_percent: Decimal,
        intent: SlotIntent | None,
        now: datetime,
    ) -> PolicyActuationResult:
        """Apply Feed-In Priority, dynamic reserve and zero or one slot."""

        # Every healthy write and any resulting safe cleanup shares this one
        # absolute budget.  Never create a fresh budget from a failure path.
        deadline = self._new_safe_deadline()
        try:
            await self._lock.acquire(deadline=deadline)
            results: list[WriteResult] = []
            try:
                if not self._guard_off():
                    fallback = await self._apply_safe_baseline_attempts_locked(
                        deadline=deadline
                    )
                    return PolicyActuationResult(
                        False,
                        fallback.safe,
                        "healthy actuation rejected: control-disable guard is asserted or unavailable",
                        fallback.results,
                    )
                if observation.health is not ControllerHealth.HEALTHY or observation.snapshot is None:
                    return await self._apply_safe_baseline_attempts_locked(
                        deadline=deadline
                    )
                if intent is not None:
                    preflight = self.slots._preflight(intent, observation, now)
                    if isinstance(preflight, str):
                        fallback = await self._apply_safe_baseline_attempts_locked(
                            deadline=deadline
                        )
                        return PolicyActuationResult(
                            False,
                            fallback.safe,
                            f"healthy actuation rejected before mutation: {preflight}",
                            fallback.results,
                        )
                snapshot = observation.snapshot
                reserve_capability = snapshot.persistent.battery_reserve_soc
                reserve, reserve_error = self._reserve_target(reserve_soc_percent, reserve_capability)
                if reserve is None:
                    fallback = await self._apply_safe_baseline_attempts_locked(
                        deadline=deadline
                    )
                    return PolicyActuationResult(
                        False,
                        fallback.safe,
                        f"healthy actuation rejected: {reserve_error}",
                        fallback.results,
                    )
                persistent = self.config.persistent
                protection = self.config.protection
                peak_shaving = (
                    intent is None or intent.direction is SlotDirection.DISCHARGE
                )
                async with self.writer.transaction(deadline=deadline) as transaction:
                    for request in (
                        SelectWriteRequest(self.writer.capture_precondition(persistent.storage_mode_entity_id), StorageMode.FEED_IN_PRIORITY.value),
                        SwitchWriteRequest(self.writer.capture_precondition(persistent.allow_grid_charging_entity_id), True),
                        SwitchWriteRequest(self.writer.capture_precondition(persistent.grid_peak_shaving_entity_id), peak_shaving),
                        NumberWriteRequest(
                            self.writer.capture_precondition(protection.battery_reserve_soc_entity_id),
                            reserve,
                            capability=reserve_capability,
                        ),
                        SwitchWriteRequest(self.writer.capture_precondition(protection.battery_reserve_entity_id), True),
                    ):
                        if not self._guard_off():
                            raise RuntimeError("control-disable guard changed during healthy actuation")
                        result = await transaction.async_write(
                            request, deadline=deadline
                        )
                        results.append(result)
                        if not result.success:
                            raise RuntimeError(result.message)
                        if not self._guard_off():
                            raise RuntimeError("control-disable guard changed during healthy actuation")
                if intent is None:
                    if not self._guard_off():
                        raise RuntimeError("control-disable guard changed before slot cleanup")
                    disabled = await self.slots._disable_all_once(
                        results, deadline=deadline
                    )
                    if not disabled.safe:
                        raise RuntimeError("not all Solis slots were proven disabled")
                    return PolicyActuationResult(True, False, "healthy baseline active; all slots off", tuple(results))
                slot_results: list[WriteResult] = []
                if not self._guard_off():
                    raise RuntimeError("control-disable guard changed before slot actuation")
                slot_result = await self.slots._async_apply_intent(
                    intent,
                    observation,
                    now,
                    slot_results,
                    deadline=deadline,
                )
                results.extend(slot_results)
                if slot_result.status is not SlotActuationStatus.APPLIED:
                    raise RuntimeError(slot_result.message or "slot actuation failed")
                return PolicyActuationResult(True, False, "healthy baseline and slot applied", tuple(results), slot_result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never replay an ambiguous healthy mutation.  Move directly
                # to the independently retryable safe baseline.
                fallback = await self._apply_safe_baseline_attempts_locked(
                    deadline=deadline
                )
                return PolicyActuationResult(False, fallback.safe, f"healthy actuation failed: {exc}", tuple(results) + fallback.results)
            finally:
                self._lock.release()
        except asyncio.CancelledError as original:
            await self._finish_safe_baseline_after_cancellation(
                deadline=deadline
            )
            raise original
        except TimeoutError as exc:
            return PolicyActuationResult(False, False, f"healthy actuation deadline exhausted: {exc}")

    @staticmethod
    def _reserve_target(reserve_soc_percent: Decimal, capability: object) -> tuple[Decimal | None, str | None]:
        """Return the upward-step-quantized reserve or a rejection reason."""

        try:
            requested = Decimal(str(reserve_soc_percent))
            minimum = capability.minimum
            maximum = capability.maximum
            step = capability.step
            if not all(value.is_finite() for value in (requested, minimum, maximum, step)) or step <= 0:
                return None, "reserve capability is not finite"
            required = max(Decimal(MINIMUM_SOC_PERCENT), requested, minimum)
            target = minimum + ((required - minimum) / step).to_integral_value(rounding=ROUND_CEILING) * step
        except (AttributeError, ArithmeticError, TypeError, ValueError):
            return None, "reserve capability is invalid"
        if target > maximum:
            return None, "reserve capability cannot represent the required safety reserve"
        if (target - minimum) % step != 0:
            return None, "reserve target is not aligned to the capability step"
        return target, None

    def _guard_off(self) -> bool:
        try:
            return self.writer.capture_precondition(self.control_disable_guard_entity_id).state == "off"
        except Exception:
            return False


__all__ = ["PolicyActuationResult", "SolisPolicyActuator"]
