"""Transitional coordinator consuming the narrow Solis adapter."""

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
from .model import ControllerHealth, CycleState, StorageMode, StrategyAction
from .planner import Plan, build_plan
from .solis import SolisAdapter, SolisState, WriteResult, read_state

_LOGGER = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = timedelta(minutes=1)
WRITE_DEADLINE = timedelta(seconds=30)


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
    """Read once, plan once and advance at most one Solis change."""

    def __init__(self, hass: HomeAssistant, config: Config) -> None:
        super().__init__(hass, _LOGGER, config_entry=None, name=DOMAIN, update_interval=HEARTBEAT_INTERVAL)
        self.config = config
        self._cycle_state = CycleState.IDLE
        self._cycle_deadline: datetime | None = None
        self._last_healthy_at: datetime | None = None
        self._unsub_sources: CALLBACK_TYPE | None = None
        self._started = False
        self._stopping = False
        self._stop_task: asyncio.Task[None] | None = None
        self._safe_state_applied = False
        zone = dt_util.get_time_zone(hass.config.time_zone)
        if zone is None:
            raise ValueError(f"Unknown Home Assistant timezone: {hass.config.time_zone}")
        self.solis_adapter = SolisAdapter(hass, config.solis, timezone=zone)

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
                self.hass, self._source_entity_ids(), self._async_source_changed
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
        # T0030 owns the final Self-Use/observed-on shutdown ordering. This
        # staged adapter slice deliberately does not invent a partial sequence.
        await self.async_shutdown()

    async def _async_update_data(self) -> Snapshot:
        now = self._now()
        if self._stopping:
            return self.data or Snapshot(
                now, ControllerHealth.FAIL_SAFE, StrategyAction.IDLE,
                "controller is stopping", self._cycle_state, self._cycle_deadline,
            )
        observation: SolisState | None = None
        try:
            observation = read_state(self.hass, self.config.solis, now=now)
            if observation.health is not ControllerHealth.HEALTHY:
                if _solis_failure_is_transient(self.hass, observation):
                    return self._snapshot(
                        now, ControllerHealth.DEGRADED, None,
                        "Solis telemetry or control readback is temporarily unavailable",
                        observation=observation, last_error=_issues_text(observation),
                    )
                return await self._fail_safe_snapshot(
                    now, "Solis state violates a controller safety invariant",
                    observation=observation, error=_issues_text(observation),
                )

            plan = await build_plan(
                self.hass, self.config, observation, now=now,
                cycle_state=self._cycle_state, cycle_deadline=self._cycle_deadline,
            )
            if plan.issue is not None:
                return self._snapshot(
                    now, ControllerHealth.DEGRADED, plan,
                    "planning inputs are temporarily unavailable",
                    observation=observation, last_error=plan.issue,
                )
            if plan.reserve_soc_percent is None:
                raise ValueError("valid plan has no reserve SOC")

            change = self.solis_adapter.next_start_change(
                observation, plan.intent,
                reserve_soc_percent=plan.reserve_soc_percent,
            )
            if change is not None:
                result = await self.solis_adapter.apply(
                    change,
                    deadline=asyncio.get_running_loop().time() + WRITE_DEADLINE.total_seconds(),
                )
                if not result.success:
                    return self._snapshot(
                        now, ControllerHealth.DEGRADED, plan,
                        "best-effort Solis reconciliation did not complete",
                        observation=observation, last_error=result.message,
                        actuation=result,
                    )
                return self._snapshot(
                    now, ControllerHealth.DEGRADED, plan,
                    "Solis reconciliation advanced one change",
                    observation=observation, actuation=result,
                )

            if not self.solis_adapter.intent_matches(
                observation, plan.intent,
                reserve_soc_percent=plan.reserve_soc_percent,
            ):
                return self._snapshot(
                    now, ControllerHealth.DEGRADED, plan,
                    "Solis start is blocked by conflicting or unknown state",
                    observation=observation,
                )

            self._safe_state_applied = False
            self._cycle_state = plan.next_cycle_state
            self._cycle_deadline = plan.cycle_deadline
            self._last_healthy_at = now
            return self._snapshot(
                now, ControllerHealth.HEALTHY, plan, _plan_reason(plan),
                observation=observation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception("House battery evaluation failed")
            return await self._fail_safe_snapshot(
                now, "critical input or coordinator failure",
                observation=observation, error=f"{type(exc).__name__}: {exc}",
            )

    async def _fail_safe_snapshot(
        self,
        now: datetime,
        reason: str,
        *,
        observation: SolisState | None = None,
        plan: Plan | None = None,
        error: str | None = None,
    ) -> Snapshot:
        self._cycle_state = CycleState.IDLE
        self._cycle_deadline = None
        result: WriteResult | None = None
        if not self._safe_state_applied:
            result = await self.solis_adapter.set_mode(
                StorageMode.SELF_USE,
                deadline=asyncio.get_running_loop().time() + WRITE_DEADLINE.total_seconds(),
            )
            self._safe_state_applied = result.success
        if result is not None and not result.success and error is None:
            error = result.message
        return self._snapshot(
            now, ControllerHealth.FAIL_SAFE, plan, reason,
            observation=observation, last_error=error, actuation=result,
        )

    def _snapshot(
        self,
        now: datetime,
        health: ControllerHealth,
        plan: Plan | None,
        reason: str,
        *,
        observation: SolisState | None,
        last_error: str | None = None,
        actuation: WriteResult | None = None,
    ) -> Snapshot:
        telemetry = None if observation is None else observation.telemetry
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
        solis = self.config.solis
        entity_ids = [
            self.config.tariff.import_rates_entity_id,
            self.config.tariff.export_rates_entity_id,
            self.config.cycle_discharge_duration_entity_id,
            solis.telemetry.state_of_charge_entity_id,
            solis.telemetry.battery_power_entity_id,
            solis.telemetry.battery_voltage_entity_id,
            solis.telemetry.device_timestamp_entity_id,
            solis.persistent.storage_mode_entity_id,
            solis.persistent.allow_grid_charging_entity_id,
            solis.persistent.inverter_time_entity_id,
            solis.protection.battery_reserve_entity_id,
            solis.protection.battery_reserve_soc_entity_id,
            solis.capability.battery_max_charge_current_entity_id,
            solis.capability.battery_max_discharge_current_entity_id,
        ]
        for slot in solis.slots:
            for direction in (slot.charge, slot.discharge):
                entity_ids.extend((direction.enable_entity_id, direction.time_entity_id,
                                   direction.current_entity_id, direction.target_soc_entity_id))
        return tuple(dict.fromkeys(entity_ids))


def _solis_failure_is_transient(hass: HomeAssistant, result: SolisState) -> bool:
    transient = {"device_timestamp_stale", "state_access_failed", "state_access_unavailable", "state_revision_invalid"}
    if not result.issues:
        return False
    for issue in result.issues:
        if issue.code in transient:
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
    return None if not isinstance(soc, Decimal) else config.battery.capacity_kwh * soc / Decimal(100)


def _window_text(window: object | None) -> str | None:
    start, end = getattr(window, "start", None), getattr(window, "end", None)
    return f"{start.isoformat()}/{end.isoformat()}" if isinstance(start, datetime) and isinstance(end, datetime) else None


def _plan_reason(plan: Plan) -> str:
    return {
        StrategyAction.IDLE: "no eligible strategy action",
        StrategyAction.CHEAP_CHARGE: "charge during trusted cheap window",
        StrategyAction.RESERVE_DISCHARGE: "export toward dynamic reserve",
        StrategyAction.CYCLE_DISCHARGE: "create profitable full-SOC headroom",
    }[plan.action]


def _issues_text(observation: SolisState) -> str | None:
    return "; ".join(issue.message for issue in observation.issues) or None


__all__ = ["Coordinator", "HEARTBEAT_INTERVAL", "Snapshot"]
