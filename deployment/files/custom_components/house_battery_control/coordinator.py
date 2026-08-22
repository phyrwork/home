"""Single-path house-battery coordinator."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .config import Config
from .const import DOMAIN
from .contracts import ControllerHealth
from .ha_writer import HomeAssistantWriter
from .runtime_inputs import RuntimeInputs, async_read_runtime_inputs
from .solis_policy import PolicyActuationResult, SolisPolicyActuator
from .strategy import CycleState, StrategyAction, select_strategy

_LOGGER = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Snapshot:
    heartbeat_at: datetime
    health: ControllerHealth
    action: StrategyAction
    reason: str
    cycle_state: CycleState
    reserve_soc_percent: Decimal | None = None
    state_of_charge_percent: Decimal | None = None
    battery_power_kw: Decimal | None = None
    current_cheap_window: str | None = None
    next_cheap_window: str | None = None
    last_healthy_at: datetime | None = None
    last_error: str | None = None
    actuation_message: str | None = None


class Coordinator(DataUpdateCoordinator[Snapshot]):
    """Read, decide and apply exactly one action per evaluation."""

    def __init__(self, hass: HomeAssistant, config: Config) -> None:
        super().__init__(hass, _LOGGER, config_entry=None, name=DOMAIN, update_interval=HEARTBEAT_INTERVAL)
        self.config = config
        self._cycle_state = CycleState.IDLE
        self._last_healthy_at: datetime | None = None
        self._unsub_sources: CALLBACK_TYPE | None = None
        writer = HomeAssistantWriter.for_home_assistant(hass)
        zone = dt_util.get_time_zone(hass.config.time_zone)
        if zone is None:
            raise ValueError(f"Unknown Home Assistant timezone: {hass.config.time_zone}")
        self.policy_actuator = SolisPolicyActuator(
            config.solis,
            writer,
            control_disable_guard_entity_id=config.control_disable_guard_entity_id,
            inverter_timezone=zone,
        )

    @staticmethod
    def _now() -> datetime:
        try:
            return dt_util.now()
        except Exception:
            return datetime.now(timezone.utc)

    async def async_start(self) -> None:
        if self._unsub_sources is None:
            self._unsub_sources = async_track_state_change_event(
                self.hass,
                self._source_entity_ids(),
                self._async_source_changed,
            )
        await self.async_refresh()

    async def async_stop(self) -> None:
        if self._unsub_sources is not None:
            self._unsub_sources()
            self._unsub_sources = None
        await self.policy_actuator.async_apply_fail_safe()
        await self.async_shutdown()

    async def _async_update_data(self) -> Snapshot:
        now = self._now()
        guard = self.hass.states.get(self.config.control_disable_guard_entity_id)
        guard_off = guard is not None and guard.state == "off"
        if not self.config.dynamic_control_enabled:
            return await self._fail_safe_snapshot(now, "dynamic control is disabled")
        if not guard_off:
            return await self._fail_safe_snapshot(now, "control-disable guard is asserted or unavailable")

        runtime: RuntimeInputs | None = None
        try:
            runtime = await async_read_runtime_inputs(
                self.hass,
                self.config,
                now=now,
                cycle_state=self._cycle_state,
            )
            decision = select_strategy(runtime.strategy)
            if decision.action is StrategyAction.FAIL_SAFE:
                return await self._fail_safe_snapshot(now, decision.reason, runtime=runtime)
            intent = decision.slot if decision.action not in (StrategyAction.IDLE, StrategyAction.STOP) else None
            actuation = await self.policy_actuator.async_apply_healthy(
                observation=runtime.solis,
                reserve_soc_percent=runtime.strategy.reserve_soc_percent,
                intent=intent,
                now=now,
            )
            if not actuation.success:
                return self._snapshot(
                    now,
                    ControllerHealth.FAIL_SAFE if actuation.safe else ControllerHealth.DEGRADED,
                    StrategyAction.FAIL_SAFE,
                    decision.reason,
                    runtime,
                    last_error=actuation.message,
                    actuation=actuation,
                )
            self._cycle_state = decision.next_cycle_state
            self._last_healthy_at = now
            return self._snapshot(
                now,
                ControllerHealth.HEALTHY,
                decision.action,
                decision.reason,
                runtime,
                actuation=actuation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception("House battery evaluation failed")
            return await self._fail_safe_snapshot(
                now,
                "critical input or coordinator failure",
                runtime=runtime,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _fail_safe_snapshot(
        self,
        now: datetime,
        reason: str,
        *,
        runtime: RuntimeInputs | None = None,
        error: str | None = None,
    ) -> Snapshot:
        self._cycle_state = CycleState.IDLE
        actuation = await self.policy_actuator.async_apply_fail_safe()
        if not actuation.success and error is None:
            error = actuation.message
        return self._snapshot(
            now,
            ControllerHealth.FAIL_SAFE if actuation.safe else ControllerHealth.DEGRADED,
            StrategyAction.FAIL_SAFE,
            reason,
            runtime,
            last_error=error,
            actuation=actuation,
        )

    def _snapshot(
        self,
        now: datetime,
        health: ControllerHealth,
        action: StrategyAction,
        reason: str,
        runtime: RuntimeInputs | None,
        *,
        last_error: str | None = None,
        actuation: PolicyActuationResult | None = None,
    ) -> Snapshot:
        telemetry = None if runtime is None or runtime.solis.snapshot is None else runtime.solis.snapshot.telemetry
        return Snapshot(
            heartbeat_at=now,
            health=health,
            action=action,
            reason=reason,
            cycle_state=self._cycle_state,
            reserve_soc_percent=None if runtime is None else runtime.strategy.reserve_soc_percent,
            state_of_charge_percent=None if telemetry is None else telemetry.state_of_charge_percent,
            battery_power_kw=None if telemetry is None else telemetry.battery_power_kw,
            current_cheap_window=_window_text(None if runtime is None else runtime.current_window),
            next_cheap_window=_window_text(None if runtime is None else runtime.next_window),
            last_healthy_at=self._last_healthy_at,
            last_error=last_error,
            actuation_message=None if actuation is None else actuation.message,
        )

    async def _async_source_changed(self, _event: Event[EventStateChangedData]) -> None:
        await self.async_request_refresh()

    def _source_entity_ids(self) -> tuple[str, ...]:
        telemetry = self.config.solis.telemetry
        return (
            self.config.control_disable_guard_entity_id,
            self.config.tariff.import_rates_entity_id,
            self.config.tariff.export_rates_entity_id,
            telemetry.state_of_charge_entity_id,
            telemetry.battery_power_entity_id,
            telemetry.battery_voltage_entity_id,
            telemetry.device_timestamp_entity_id,
        )


def _window_text(window: object) -> str | None:
    start = getattr(window, "start", None)
    end = getattr(window, "end", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return f"{start.isoformat()}/{end.isoformat()}"


__all__ = ["Coordinator", "HEARTBEAT_INTERVAL", "Snapshot"]
