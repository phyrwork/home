"""Transitional single-path house-battery coordinator."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .config import Config
from .const import DOMAIN
from .contracts import SlotIntent as LegacySlotIntent
from .ha_writer import HomeAssistantWriter
from .model import (
    ControllerHealth,
    CycleState,
    LogicalIntent,
    SlotDirection,
    SlotOwner,
    StrategyAction,
)
from .planner import Plan, build_plan
from .solis_policy import PolicyActuationResult, SolisPolicyActuator
from .solis_reader import read_solis_state

_LOGGER = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Snapshot:
    heartbeat_at: datetime
    health: ControllerHealth
    action: StrategyAction
    reason: str
    cycle_state: CycleState
    cycle_deadline: datetime | None = None
    reserve_soc_percent: Decimal | None = None
    battery_energy_kwh: Decimal | None = None
    reserve_target_energy_kwh: Decimal | None = None
    reserve_balance_kwh: Decimal | None = None
    state_of_charge_percent: Decimal | None = None
    battery_power_kw: Decimal | None = None
    current_cheap_window: str | None = None
    next_cheap_window: str | None = None
    last_healthy_at: datetime | None = None
    last_error: str | None = None
    actuation_message: str | None = None


class Coordinator(DataUpdateCoordinator[Snapshot]):
    """Read Solis once, build one plan and apply one transitional action."""

    def __init__(self, hass: HomeAssistant, config: Config) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=None,
            name=DOMAIN,
            update_interval=HEARTBEAT_INTERVAL,
        )
        self.config = config
        self._cycle_state = CycleState.IDLE
        self._cycle_deadline: datetime | None = None
        self._last_healthy_at: datetime | None = None
        self._unsub_sources: CALLBACK_TYPE | None = None
        self._started = False
        self._stopping = False
        self._stop_task: asyncio.Task[None] | None = None
        self._safe_state_applied = False
        writer = HomeAssistantWriter.for_home_assistant(hass)
        zone = dt_util.get_time_zone(hass.config.time_zone)
        if zone is None:
            raise ValueError(f"Unknown Home Assistant timezone: {hass.config.time_zone}")
        self.policy_actuator = SolisPolicyActuator(
            config.solis,
            writer,
            inverter_timezone=zone,
        )

    @staticmethod
    def _now() -> datetime:
        try:
            return dt_util.now()
        except Exception:
            return datetime.now(timezone.utc)

    async def async_start(self) -> None:
        if self._stopping or self._started:
            return
        self._started = True
        if self._unsub_sources is None:
            self._unsub_sources = async_track_state_change_event(
                self.hass,
                self._source_entity_ids(),
                self._async_source_changed,
            )
        await self.async_refresh()

    async def async_stop(self) -> None:
        if self._stop_task is None:
            self._stopping = True
            self._stop_task = asyncio.create_task(self._async_stop_once())
        await asyncio.shield(self._stop_task)

    async def _async_stop_once(self) -> None:
        if self._unsub_sources is not None:
            self._unsub_sources()
            self._unsub_sources = None
        await self.policy_actuator.async_apply_safe_baseline()
        await self.async_shutdown()

    async def _async_update_data(self) -> Snapshot:
        now = self._now()
        if self._stopping:
            return self.data or Snapshot(
                heartbeat_at=now,
                health=ControllerHealth.FAIL_SAFE,
                action=StrategyAction.IDLE,
                reason="controller is stopping",
                cycle_state=self._cycle_state,
                cycle_deadline=self._cycle_deadline,
            )
        observation: object | None = None
        try:
            observation = read_solis_state(self.config.solis, self.hass.states, now)
            if (
                observation.health is not ControllerHealth.HEALTHY
                or observation.snapshot is None
            ):
                if _solis_failure_is_transient(self.hass, observation):
                    return await self._degraded_solis_snapshot(
                        now,
                        "Solis telemetry or control readback is temporarily unavailable",
                        observation=observation,
                        error=_issues_text(observation),
                    )
                self._safe_state_applied = False
                return await self._fail_safe_snapshot(
                    now,
                    "Solis state violates a controller safety invariant",
                    observation=observation,
                    error=_issues_text(observation),
                )

            plan = await build_plan(
                self.hass,
                self.config,
                observation.snapshot,
                now=now,
                cycle_state=self._cycle_state,
                cycle_deadline=self._cycle_deadline,
            )
            if plan.issue is not None:
                # Planning failure is observation-only. Preserve the bounded
                # native action and cycle state until planning recovers.
                return self._snapshot(
                    now,
                    ControllerHealth.DEGRADED,
                    plan,
                    "planning inputs are temporarily unavailable",
                    observation=observation,
                    last_error=plan.issue,
                )

            if plan.reserve_soc_percent is None:
                raise ValueError("valid plan has no reserve SOC")
            actuation = await self.policy_actuator.async_apply_healthy(
                observation=observation,
                reserve_soc_percent=plan.reserve_soc_percent,
                intent=_legacy_intent(plan.intent),
                now=now,
            )
            if not actuation.success:
                self._safe_state_applied = actuation.safe
                return await self._fail_safe_snapshot(
                    now,
                    "Solis actuation failed",
                    observation=observation,
                    plan=plan,
                    error=actuation.message,
                )
            self._safe_state_applied = False
            self._cycle_state = plan.next_cycle_state
            self._cycle_deadline = plan.cycle_deadline
            self._last_healthy_at = now
            return self._snapshot(
                now,
                ControllerHealth.HEALTHY,
                plan,
                _plan_reason(plan),
                observation=observation,
                actuation=actuation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception("House battery evaluation failed")
            self._safe_state_applied = False
            return await self._fail_safe_snapshot(
                now,
                "critical input or coordinator failure",
                observation=observation,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _degraded_solis_snapshot(
        self,
        now: datetime,
        reason: str,
        *,
        observation: object | None,
        error: str | None,
    ) -> Snapshot:
        """Retain the pre-T0030 baseline behavior for Solis read faults only."""

        self._cycle_state = CycleState.IDLE
        self._cycle_deadline = None
        actuation = await self.policy_actuator.async_apply_safe_baseline()
        self._safe_state_applied = actuation.safe
        if not actuation.safe:
            return self._snapshot(
                now,
                ControllerHealth.FAIL_SAFE,
                None,
                "safe baseline could not be proven during degraded operation",
                observation=observation,
                last_error=actuation.message if error is None else error,
                actuation=actuation,
            )
        return self._snapshot(
            now,
            ControllerHealth.DEGRADED,
            None,
            reason,
            observation=observation,
            last_error=error,
            actuation=actuation,
        )

    async def _fail_safe_snapshot(
        self,
        now: datetime,
        reason: str,
        *,
        observation: object | None = None,
        plan: Plan | None = None,
        error: str | None = None,
    ) -> Snapshot:
        self._cycle_state = CycleState.IDLE
        self._cycle_deadline = None
        if self._safe_state_applied:
            actuation = PolicyActuationResult(True, True, "fail-safe already applied")
        else:
            actuation = await self.policy_actuator.async_apply_safe_baseline()
            self._safe_state_applied = actuation.safe
        if not actuation.success and error is None:
            error = actuation.message
        return self._snapshot(
            now,
            ControllerHealth.FAIL_SAFE,
            plan,
            reason,
            observation=observation,
            last_error=error,
            actuation=actuation,
        )

    def _snapshot(
        self,
        now: datetime,
        health: ControllerHealth,
        plan: Plan | None,
        reason: str,
        *,
        observation: object | None,
        last_error: str | None = None,
        actuation: PolicyActuationResult | None = None,
    ) -> Snapshot:
        solis = getattr(observation, "snapshot", None)
        telemetry = getattr(solis, "telemetry", None)
        actual = None if plan is None else plan.battery_energy_kwh
        if actual is None:
            actual = _actual_energy(self.config, telemetry)
        return Snapshot(
            heartbeat_at=now,
            health=health,
            action=StrategyAction.IDLE if plan is None else plan.action,
            reason=reason,
            cycle_state=self._cycle_state,
            cycle_deadline=self._cycle_deadline,
            reserve_soc_percent=None if plan is None else plan.reserve_soc_percent,
            battery_energy_kwh=actual,
            reserve_target_energy_kwh=None if plan is None else plan.reserve_energy_kwh,
            reserve_balance_kwh=None if plan is None else plan.reserve_balance_kwh,
            state_of_charge_percent=None if telemetry is None else telemetry.state_of_charge_percent,
            battery_power_kw=None if telemetry is None else telemetry.battery_power_kw,
            current_cheap_window=_window_text(None if plan is None else plan.current_cheap_window),
            next_cheap_window=_window_text(None if plan is None else plan.next_cheap_window),
            last_healthy_at=self._last_healthy_at,
            last_error=last_error,
            actuation_message=None if actuation is None else actuation.message,
        )

    async def _async_source_changed(self, _event: Event[EventStateChangedData]) -> None:
        if not self._stopping:
            await self.async_request_refresh()

    def _source_entity_ids(self) -> tuple[str, ...]:
        telemetry = self.config.solis.telemetry
        persistent = self.config.solis.persistent
        protection = self.config.solis.protection
        capability = self.config.solis.capability
        entity_ids = [
            self.config.tariff.import_rates_entity_id,
            self.config.tariff.export_rates_entity_id,
            self.config.cycle_discharge_duration_entity_id,
            telemetry.state_of_charge_entity_id,
            telemetry.battery_power_entity_id,
            telemetry.battery_voltage_entity_id,
            telemetry.device_timestamp_entity_id,
            persistent.storage_mode_entity_id,
            persistent.allow_grid_charging_entity_id,
            persistent.inverter_time_entity_id,
            protection.battery_reserve_entity_id,
            protection.battery_reserve_soc_entity_id,
            capability.battery_max_charge_current_entity_id,
            capability.battery_max_discharge_current_entity_id,
        ]
        for slot in self.config.solis.slots:
            for direction in (slot.charge, slot.discharge):
                entity_ids.extend((
                    direction.enable_entity_id,
                    direction.time_entity_id,
                    direction.current_entity_id,
                    direction.target_soc_entity_id,
                ))
        return tuple(dict.fromkeys(entity_ids))


def _legacy_intent(intent: LogicalIntent | None) -> LegacySlotIntent | None:
    """Bridge one logical segment to the old actuator until T0029 deletes it."""

    if intent is None:
        return None
    if len(intent.segments) != 1:
        raise ValueError("transitional actuator cannot accept split logical intent")
    segment = intent.segments[0]
    physical_slot = {
        (SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE): 1,
        (SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE): 1,
        (SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE): 2,
    }.get((segment.owner, segment.direction))
    if physical_slot is None:
        raise ValueError("logical intent has no transitional physical allocation")
    return LegacySlotIntent(
        owner=segment.owner,
        physical_slot=physical_slot,
        direction=segment.direction,
        start=segment.start,
        end=segment.end,
        current=segment.current,
        target_soc=segment.target_soc,
        expiry=segment.expiry,
    )


def _solis_failure_is_transient(hass: HomeAssistant, result: object) -> bool:
    transient_codes = {"device_timestamp_stale", "state_access_failed", "state_access_unavailable"}
    issues = getattr(result, "issues", ())
    if not issues:
        return False
    for issue in issues:
        if issue.code in transient_codes:
            continue
        if issue.entity_id is None:
            return False
        state = hass.states.get(issue.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        return False
    return True


def _actual_energy(config: Config, telemetry: object | None) -> Decimal | None:
    soc = getattr(telemetry, "state_of_charge_percent", None)
    if not isinstance(soc, Decimal):
        return None
    return config.battery.capacity_kwh * soc / Decimal(100)


def _window_text(window: object | None) -> str | None:
    start = getattr(window, "start", None)
    end = getattr(window, "end", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return f"{start.isoformat()}/{end.isoformat()}"


def _plan_reason(plan: Plan) -> str:
    return {
        StrategyAction.IDLE: "no eligible strategy action",
        StrategyAction.CHEAP_CHARGE: "charge during trusted cheap window",
        StrategyAction.RESERVE_DISCHARGE: "export toward dynamic reserve",
        StrategyAction.CYCLE_DISCHARGE: "create profitable full-SOC headroom",
    }[plan.action]


def _issues_text(observation: object) -> str | None:
    messages = [getattr(issue, "message", str(issue)) for issue in getattr(observation, "issues", ())]
    return "; ".join(messages) or None


__all__ = ["Coordinator", "HEARTBEAT_INTERVAL", "Snapshot"]
