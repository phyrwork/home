"""Verified Solis fail-safe and healthy-runtime writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, tzinfo
from decimal import Decimal, ROUND_CEILING

from .contracts import ControllerHealth, SlotIntent, StorageMode
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
from .write_contracts import NumberWriteRequest, SelectWriteRequest, SwitchWriteRequest, WriteResult


@dataclass(frozen=True, slots=True)
class PolicyActuationResult:
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

    async def _apply_fail_safe_locked(self, *, deadline: datetime | None = None) -> PolicyActuationResult:
        """Apply fail-safe while this task owns the policy lock.

        The slot actuator shares this orchestration lock.  Calling its public
        cancellation wrapper from a second task would therefore deadlock while
        the cancelled task still owns the lock.
        """

        # The independent watchdog is the sole guard assertion boundary.  A
        # coordinator shutdown or component fault may request cleanup before
        # that automation has asserted the guard; fail closed with no Solis
        # writes in that case.
        if not self._guard_on():
            return PolicyActuationResult(
                False,
                False,
                "control-disable guard is not asserted or unavailable",
            )
        del deadline  # Writes are individually bounded by HomeAssistantWriter.
        results: list[WriteResult] = []
        try:
            disabled = await self.slots._disable_all_once(results)
            persistent = self.config.persistent
            protection = self.config.protection
            async with self.writer.transaction() as transaction:
                for request in (
                    SelectWriteRequest(self.writer.capture_precondition(persistent.storage_mode_entity_id), StorageMode.SELF_USE.value),
                    SwitchWriteRequest(self.writer.capture_precondition(protection.battery_reserve_entity_id), False),
                ):
                    # Continue through every persistent control after a normal
                    # rejection so the returned proof includes all controls.
                    results.append(await transaction.async_write(request))
            safe = disabled.safe and all(result.success for result in results)
            return PolicyActuationResult(safe, safe, "fail-safe applied" if safe else "fail-safe readback incomplete", tuple(results))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return PolicyActuationResult(False, False, f"fail-safe failed: {exc}", tuple(results))

    @staticmethod
    def _clear_cancellation() -> None:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()

    async def _finish_fail_safe_after_cancellation(self, *, deadline: datetime | None = None) -> PolicyActuationResult:
        """Finish cleanup despite repeated cancellation delivery."""

        self._clear_cancellation()
        while True:
            try:
                return await self._apply_fail_safe_locked(deadline=deadline)
            except asyncio.CancelledError:
                self._clear_cancellation()

    async def async_apply_fail_safe(self, *, deadline: datetime | None = None) -> PolicyActuationResult:
        """Disable every slot and restore autonomous Self-Use operation."""

        async with self._lock:
            try:
                return await self._apply_fail_safe_locked(deadline=deadline)
            except asyncio.CancelledError as original:
                await self._finish_fail_safe_after_cancellation(deadline=deadline)
                raise original

    async def async_apply_healthy(
        self,
        *,
        observation: SolisStateReadResult,
        reserve_soc_percent: Decimal,
        intent: SlotIntent | None,
        now: datetime,
    ) -> PolicyActuationResult:
        """Apply Feed-In Priority, dynamic reserve and zero or one slot."""

        async with self._lock:
            results: list[WriteResult] = []
            try:
                if not self._guard_off():
                    return await self._apply_fail_safe_locked()
                if observation.health is not ControllerHealth.HEALTHY or observation.snapshot is None:
                    return await self._apply_fail_safe_locked()
                snapshot = observation.snapshot
                reserve_capability = snapshot.persistent.battery_reserve_soc
                reserve, reserve_error = self._reserve_target(reserve_soc_percent, reserve_capability)
                if reserve is None:
                    fallback = await self._apply_fail_safe_locked()
                    return PolicyActuationResult(
                        False,
                        fallback.safe,
                        f"healthy actuation rejected: {reserve_error}",
                        fallback.results,
                    )
                persistent = self.config.persistent
                protection = self.config.protection
                async with self.writer.transaction() as transaction:
                    for request in (
                        SelectWriteRequest(self.writer.capture_precondition(persistent.storage_mode_entity_id), StorageMode.FEED_IN_PRIORITY.value),
                        SwitchWriteRequest(self.writer.capture_precondition(persistent.allow_grid_charging_entity_id), True),
                        NumberWriteRequest(
                            self.writer.capture_precondition(protection.battery_reserve_soc_entity_id),
                            reserve,
                            capability=reserve_capability,
                        ),
                        SwitchWriteRequest(self.writer.capture_precondition(protection.battery_reserve_entity_id), True),
                    ):
                        result = await transaction.async_write(request)
                        results.append(result)
                        if not result.success:
                            raise RuntimeError(result.message)
                if intent is None:
                    disabled = await self.slots._disable_all_once(results)
                    if not disabled.safe:
                        raise RuntimeError("not all Solis slots were proven disabled")
                    return PolicyActuationResult(True, False, "healthy baseline active; all slots off", tuple(results))
                slot_results: list[WriteResult] = []
                slot_result = await self.slots._async_apply_intent(intent, observation, now, slot_results)
                results.extend(slot_results)
                if slot_result.status is not SlotActuationStatus.APPLIED:
                    raise RuntimeError(slot_result.message or "slot actuation failed")
                return PolicyActuationResult(True, False, "healthy baseline and slot applied", tuple(results), slot_result)
            except asyncio.CancelledError as original:
                await self._finish_fail_safe_after_cancellation()
                raise original
            except Exception as exc:
                fallback = await self._apply_fail_safe_locked()
                return PolicyActuationResult(False, fallback.safe, f"healthy actuation failed: {exc}", tuple(results) + fallback.results)

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

    def _guard_on(self) -> bool:
        try:
            return self.writer.capture_precondition(self.control_disable_guard_entity_id).state == "on"
        except Exception:
            return False

    def _guard_off(self) -> bool:
        try:
            return self.writer.capture_precondition(self.control_disable_guard_entity_id).state == "off"
        except Exception:
            return False


__all__ = ["PolicyActuationResult", "SolisPolicyActuator"]
