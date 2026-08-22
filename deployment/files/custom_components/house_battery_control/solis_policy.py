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
    DisableAllResult,
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

    async def async_apply_fail_safe(self, *, deadline: datetime | None = None) -> PolicyActuationResult:
        """Disable every slot and restore autonomous Self-Use operation."""

        del deadline  # Writes are individually bounded by HomeAssistantWriter.
        async with self._lock:
            results: list[WriteResult] = []
            try:
                disabled = await self.slots.async_disable_all()
                results.extend(disabled.results)
                persistent = self.config.persistent
                protection = self.config.protection
                async with self.writer.transaction() as transaction:
                    for request in (
                        SelectWriteRequest(self.writer.capture_precondition(persistent.storage_mode_entity_id), StorageMode.SELF_USE.value),
                        SwitchWriteRequest(self.writer.capture_precondition(persistent.grid_peak_shaving_entity_id), True),
                        SwitchWriteRequest(self.writer.capture_precondition(protection.battery_reserve_entity_id), False),
                    ):
                        results.append(await transaction.async_write(request))
                safe = disabled.safe and all(result.success for result in results)
                return PolicyActuationResult(safe, safe, "fail-safe applied" if safe else "fail-safe readback incomplete", tuple(results))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return PolicyActuationResult(False, False, f"fail-safe failed: {exc}", tuple(results))

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
            if not self._guard_off():
                return await self.async_apply_fail_safe()
            if observation.health is not ControllerHealth.HEALTHY or observation.snapshot is None:
                return await self.async_apply_fail_safe()
            snapshot = observation.snapshot
            reserve_capability = snapshot.persistent.battery_reserve_soc
            reserve = max(Decimal(MINIMUM_SOC_PERCENT), reserve_soc_percent, reserve_capability.minimum)
            reserve = min(reserve, reserve_capability.maximum)
            reserve = (
                ((reserve - reserve_capability.minimum) / reserve_capability.step).to_integral_value(rounding=ROUND_CEILING)
                * reserve_capability.step
                + reserve_capability.minimum
            )
            results: list[WriteResult] = []
            try:
                persistent = self.config.persistent
                protection = self.config.protection
                async with self.writer.transaction() as transaction:
                    for request in (
                        SelectWriteRequest(self.writer.capture_precondition(persistent.storage_mode_entity_id), StorageMode.FEED_IN_PRIORITY.value),
                        SwitchWriteRequest(self.writer.capture_precondition(persistent.allow_grid_charging_entity_id), True),
                        SwitchWriteRequest(self.writer.capture_precondition(persistent.grid_peak_shaving_entity_id), True),
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
                    disabled = await self.slots.async_disable_all()
                    results.extend(disabled.results)
                    if not disabled.safe:
                        raise RuntimeError("not all Solis slots were proven disabled")
                    return PolicyActuationResult(True, False, "healthy baseline active; all slots off", tuple(results))
                slot_result = await self.slots.async_apply_intent(intent, observation, now=now)
                results.extend(slot_result.results)
                if slot_result.status is not SlotActuationStatus.APPLIED:
                    raise RuntimeError(slot_result.message or "slot actuation failed")
                return PolicyActuationResult(True, False, "healthy baseline and slot applied", tuple(results), slot_result)
            except asyncio.CancelledError:
                await asyncio.shield(self.async_apply_fail_safe())
                raise
            except Exception as exc:
                fallback = await self.async_apply_fail_safe()
                return PolicyActuationResult(False, fallback.safe, f"healthy actuation failed: {exc}", tuple(results) + fallback.results)

    async def async_disable_all_slots(self) -> DisableAllResult:
        return await self.slots.async_disable_all()

    def _guard_off(self) -> bool:
        try:
            return self.writer.capture_precondition(self.control_disable_guard_entity_id).state == "off"
        except Exception:
            return False


__all__ = ["PolicyActuationResult", "SolisPolicyActuator"]
