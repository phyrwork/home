"""Fail-closed transactional actuator for the mapped Solis schedule slots.

This module is deliberately kept at the Home Assistant boundary.  It accepts
the immutable configuration and a verified writer, but it does not import
Home Assistant or make a device/cloud request.  A Home Assistant write is
treated as an acknowledgement only; device reconciliation is a separate
commissioning concern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re

from .contracts import SlotDirection, SlotIntent, SlotOwner
from .domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from .ha_writer import HomeAssistantWriter, WriteTransaction
from .solis_config import (
    BatteryPowerSign,
    SolisConfig,
    SolisSlotDirectionConfig,
    SolisSlotOwner,
)
from .solis_state import SolisStateSnapshot
from .write_contracts import (
    NumberWriteRequest,
    SwitchWriteRequest,
    TextWriteRequest,
    WriteOutcome,
    WriteResult,
)


MAXIMUM_INVERTER_CLOCK_SKEW = timedelta(minutes=1)
COMMISSIONING_SCHEMA_VERSION = 1
MAPPING_FINGERPRINT_SCHEMA_VERSION = 1

_TIME_TEXT = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
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
    status: str
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


def _state_text(state: object) -> str | None:
    if isinstance(state, Mapping):
        value = state.get("state")
    else:
        value = getattr(state, "state", None)
    return value if isinstance(value, str) else None


def _state_available_off(state: object) -> bool:
    return _state_text(state) == "off"


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
        commissioning: CommissioningRecord | None = None,
        inverter_timezone: tzinfo = timezone.utc,
    ) -> None:
        if not control_disable_guard_entity_id.startswith("input_boolean.") and not control_disable_guard_entity_id.startswith("switch."):
            raise ValueError("control-disable guard must be a switch-like entity")
        self.config = config
        self.writer = writer
        self.control_disable_guard_entity_id = control_disable_guard_entity_id
        self.commissioning = commissioning
        self.inverter_timezone = inverter_timezone
        self._construction_fingerprint = mapping_fingerprint(config)

    @property
    def mapping_fingerprint(self) -> str:
        return mapping_fingerprint(self.config)

    def _current_mapping_is_unchanged(self) -> bool:
        try:
            return self.mapping_fingerprint == self._construction_fingerprint
        except Exception:
            return False

    def _commissioned(self, now: datetime) -> bool:
        record = self.commissioning
        if not isinstance(record, CommissioningRecord):
            return False
        try:
            return (
                record.schema_version == COMMISSIONING_SCHEMA_VERSION
                and record.commissioned_at <= now
                and record.mapping_fingerprint == self.mapping_fingerprint
                and record.ha_readback_validated
                and record.device_reconciliation_validated
            )
        except Exception:
            return False

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
        self, intent: SlotIntent, snapshot: SolisStateSnapshot | None, now: datetime
    ) -> tuple[SolisSlotDirectionConfig, object, str] | str:
        if now.tzinfo is None or now.utcoffset() is None:
            return "now must be timezone-aware"
        if snapshot is None or snapshot.observed_at is None:
            return "healthy Solis snapshot is required"
        if not isinstance(snapshot, SolisStateSnapshot):
            return "healthy Solis snapshot is required"
        if snapshot.telemetry.device_timestamp is None:
            return "inverter device timestamp is unavailable"
        if snapshot.telemetry.device_timestamp.tzinfo is None or snapshot.telemetry.device_timestamp.utcoffset() is None:
            return "inverter device timestamp must be timezone-aware"
        if abs(now - snapshot.telemetry.device_timestamp) > MAXIMUM_INVERTER_CLOCK_SKEW:
            return "inverter clock exceeds allowed skew"
        if snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
            return "snapshot observation time must be timezone-aware"
        # Health is an explicit T0004 result, not inferred from partial fields.
        if snapshot is None or not isinstance(snapshot.telemetry, object):
            return "healthy Solis snapshot is required"
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
        if not intent.start <= now < intent.end:
            return "slot intent is not active"
        if now >= intent.expiry:
            return "slot intent has expired"
        if not self._guard_off():
            return "control-disable guard is asserted or unavailable"
        return direction_config, direction_state, schedule

    def _state(self, entity_id: str) -> object:
        return self.writer._get(entity_id)

    def _guard_off(self) -> bool:
        return _state_available_off(self._state(self.control_disable_guard_entity_id))

    def _request_switch(self, entity_id: str, target: bool) -> SwitchWriteRequest | None:
        try:
            return SwitchWriteRequest(self.writer.capture_precondition(entity_id), target)
        except Exception:
            return None

    async def _disable_all_locked(self, transaction: WriteTransaction) -> tuple[list[WriteResult], bool]:
        results: list[WriteResult] = []
        for direction, _physical_slot, _kind in self._directions():
            state = self._state(direction.enable_entity_id)
            raw = _state_text(state)
            if raw is None or raw in {"unknown", "unavailable"}:
                results.append(_result_failure(direction.enable_entity_id, "enable state unavailable"))
                continue
            request = self._request_switch(direction.enable_entity_id, False)
            if request is None:
                results.append(_result_failure(direction.enable_entity_id, "unable to capture switch precondition"))
                continue
            if raw == "on":
                results.append(await transaction.async_write(request))
            elif raw != "off":
                results.append(_result_failure(direction.enable_entity_id, "enable state is invalid"))
        proven = True
        for direction, _physical_slot, _kind in self._directions():
            if not _state_available_off(self._state(direction.enable_entity_id)):
                proven = False
        return results, proven

    async def _disable_all_once(self) -> DisableAllResult:
        results: list[WriteResult] = []
        proven = False
        try:
            async with self.writer.transaction() as transaction:
                results, proven = await self._disable_all_locked(transaction)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            results.append(_result_failure("solis.slot", f"disable-all transaction failed: {exc}"))
            proven = False
        return DisableAllResult(tuple(results), proven and all(result.success for result in results))

    async def async_disable_all(self) -> DisableAllResult:
        """Disable every direction, independently of health or commissioning."""

        try:
            return await self._disable_all_once()
        except asyncio.CancelledError:
            # Cancellation during cleanup starts a fresh transaction and waits
            # for it before propagating cancellation to the caller.
            cleanup = asyncio.create_task(self._disable_all_once())
            await _await_cleanup(cleanup)
            raise

    async def _cleanup_after_cancellation(self) -> None:
        cleanup = asyncio.create_task(self._disable_all_once())
        await _await_cleanup(cleanup)

    async def _cleanup_after_failure(self, results: list[WriteResult]) -> bool:
        cleanup = await self._disable_all_once()
        results.extend(cleanup.results)
        return cleanup.safe

    async def async_apply_intent(
        self,
        intent: SlotIntent,
        snapshot: SolisStateSnapshot | None,
        *,
        now: datetime,
    ) -> SlotActuationResult:
        """Apply one active intent or prove the system safe after failure."""

        results: list[WriteResult] = []
        try:
            if not self._current_mapping_is_unchanged() or not self._commissioned(now):
                cleanup = await self.async_disable_all()
                status = SlotActuationStatus.BLOCKED_UNCOMMISSIONED_SAFE if cleanup.safe else SlotActuationStatus.BLOCKED_UNCOMMISSIONED_UNSAFE
                return SlotActuationResult(status, cleanup.results, message="slot actuator is not commissioned for the current mapping")
            preflight = self._preflight(intent, snapshot, now)
            if isinstance(preflight, str):
                safe = await self._cleanup_after_failure(results)
                status = SlotActuationStatus.FAILED_SAFE if safe else SlotActuationStatus.FAILED_UNSAFE
                return SlotActuationResult(status, tuple(results), message=preflight)
            target_config, target_state, schedule = preflight
            deadline = min(intent.end, intent.expiry)
            async with self.writer.transaction() as transaction:
                # All twelve switch preconditions are captured immediately
                # before their individual CAS writes.
                disable_results, proven = await self._disable_all_locked(transaction)
                results.extend(disable_results)
                if not proven or not all(result.success for result in disable_results):
                    raise RuntimeError("all Solis slot directions were not proven disabled")
                time_request = TextWriteRequest(
                    self.writer.capture_precondition(target_config.time_entity_id),
                    schedule,
                    text_validator=lambda value: _TIME_TEXT.fullmatch(value) is not None and value.split("-")[0] != value.split("-")[1],
                )
                current_request = NumberWriteRequest(
                    self.writer.capture_precondition(target_config.current_entity_id),
                    intent.current,
                    capability=target_state.current,
                )
                soc_request = NumberWriteRequest(
                    self.writer.capture_precondition(target_config.target_soc_entity_id),
                    intent.target_soc,
                    capability=target_state.target_soc,
                )
                for request in (time_request, current_request, soc_request):
                    write_result = await transaction.async_write(request)
                    results.append(write_result)
                    if not write_result.success:
                        raise RuntimeError(f"configuration write failed: {write_result.message}")
                if not self._guard_off():
                    raise RuntimeError("control-disable guard asserted before slot enable")
                enable_request = SwitchWriteRequest(
                    self.writer.capture_precondition(target_config.enable_entity_id), True
                )
                write_result = await transaction.async_write(enable_request)
                results.append(write_result)
                if not write_result.success:
                    raise RuntimeError(f"slot enable failed: {write_result.message}")
                if not self._guard_off():
                    raise RuntimeError("control-disable guard asserted after slot enable")
                for direction, physical_slot, kind in self._directions():
                    enabled = _state_text(self._state(direction.enable_entity_id))
                    expected = direction.enable_entity_id == target_config.enable_entity_id
                    if enabled != ("on" if expected else "off"):
                        raise RuntimeError(f"final slot enable proof failed for {physical_slot} {kind.value}")
            return SlotActuationResult(
                SlotActuationStatus.APPLIED_HA_PENDING_DEVICE_RECONCILIATION,
                tuple(results),
                mandatory_disable_deadline=deadline,
                message="Home Assistant readback confirmed; device reconciliation remains pending",
            )
        except asyncio.CancelledError:
            await self._cleanup_after_cancellation()
            raise
        except Exception as exc:
            safe = await self._cleanup_after_failure(results)
            status = SlotActuationStatus.FAILED_SAFE if safe else SlotActuationStatus.FAILED_UNSAFE
            return SlotActuationResult(status, tuple(results), message=str(exc))


async def _await_cleanup(task: asyncio.Task[object]) -> object:
    """Shield cleanup through repeated cancellation delivery."""

    cancellation: asyncio.CancelledError | None = None
    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            if current is not None:
                current.uncancel()
            continue
    result = await task
    if cancellation is not None:
        raise cancellation
    return result


# Names used by later adapters and tests.
SolisSlotActuation = SolisSlotActuator
MappingCommissioningRecord = CommissioningRecord
canonical_mapping_bytes = canonical_mapping
compute_mapping_fingerprint = mapping_fingerprint


__all__ = [
    "COMMISSIONING_SCHEMA_VERSION",
    "CommissioningRecord",
    "DisableAllResult",
    "MAXIMUM_INVERTER_CLOCK_SKEW",
    "MAPPING_FINGERPRINT_SCHEMA_VERSION",
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
