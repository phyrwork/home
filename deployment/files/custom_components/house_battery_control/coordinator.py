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
from .contracts import ControllerHealth, StorageMode
from .domain_constants import FULL_SOC_PERCENT
from .ha_writer import HomeAssistantWriter
from .reserve_planner import ReservePlanResult
from .runtime_inputs import RuntimeInputs, async_read_runtime_inputs
from .solis_policy import PolicyActuationResult, SolisPolicyActuator
from .solis_reader import read_solis_state
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
    """Read, decide and apply exactly one action per evaluation."""

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
        # A disabled controller still establishes the safe inverter baseline
        # once at startup.  Remembering that result is important: the normal
        # heartbeat must not re-issue cloud writes every minute while the MVP
        # is intentionally disabled.
        self._safe_state_applied = False
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
        # Shutdown is an explicit safety boundary.  Do not use the cached
        # disabled result here: the independent watchdog may have observed a
        # state change since the last evaluation.
        result = await self.policy_actuator.async_apply_fail_safe()
        self._safe_state_applied = result.safe
        await self.async_shutdown()

    async def _async_update_data(self) -> Snapshot:
        now = self._now()
        if self._stopping:
            return self.data or Snapshot(
                heartbeat_at=now,
                health=ControllerHealth.FAIL_SAFE,
                action=StrategyAction.FAIL_SAFE,
                reason="controller is stopping",
                cycle_state=CycleState.IDLE,
            )
        guard = self.hass.states.get(self.config.control_disable_guard_entity_id)
        guard_off = guard is not None and guard.state == "off"
        if not self.config.dynamic_control_enabled:
            # Keep observing the native controls after the one-time startup
            # safe write.  An unavailable/stale/invalid Solis observation is a
            # real fault even when dynamic scheduling is disabled.
            if self._safe_state_applied:
                if guard is None or guard.state != "on":
                    self._safe_state_applied = False
                    return await self._fail_safe_snapshot(
                        now,
                        "control-disable guard is not asserted",
                    )
                observation = read_solis_state(self.config.solis, self.hass.states, now)
                if (
                    observation.health is not ControllerHealth.HEALTHY
                    or not self._is_safe_state_proven(observation)
                ):
                    self._safe_state_applied = False
                    return await self._fail_safe_snapshot(
                        now,
                        "Solis safe state is unavailable or not proven",
                        error=_issues_text(observation),
                    )
                telemetry = observation.telemetry
                reserve_soc, battery_energy, reserve_target, reserve_balance = (
                    await self._async_reserve_diagnostics(now, observation)
                )
                self._last_healthy_at = now
                return Snapshot(
                    heartbeat_at=now,
                    health=ControllerHealth.HEALTHY,
                    action=StrategyAction.STOP,
                    reason="dynamic control is disabled",
                    cycle_state=CycleState.IDLE,
                    reserve_soc_percent=reserve_soc,
                    battery_energy_kwh=battery_energy,
                    reserve_target_energy_kwh=reserve_target,
                    reserve_balance_kwh=reserve_balance,
                    state_of_charge_percent=None if telemetry is None else telemetry.state_of_charge_percent,
                    battery_power_kw=None if telemetry is None else telemetry.battery_power_kw,
                    last_healthy_at=self._last_healthy_at,
                )
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
                cycle_deadline=self._cycle_deadline,
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
            self._safe_state_applied = False
            previous_cycle_state = self._cycle_state
            self._cycle_state = decision.next_cycle_state
            if decision.action is StrategyAction.CYCLE_DISCHARGE and self._cycle_deadline is None and decision.slot is not None:
                self._cycle_deadline = decision.slot.end
            elif decision.action is StrategyAction.STOP and previous_cycle_state is CycleState.STOPPING:
                self._cycle_deadline = None
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
        self._cycle_deadline = None
        if self._safe_state_applied:
            actuation = PolicyActuationResult(True, True, "fail-safe already applied")
        else:
            actuation = await self.policy_actuator.async_apply_fail_safe()
            self._safe_state_applied = actuation.safe
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

    async def _async_reserve_diagnostics(
        self,
        now: datetime,
        observation: object,
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
        """Read planner diagnostics without making them a safety prerequisite."""

        try:
            runtime = await async_read_runtime_inputs(
                self.hass,
                self.config,
                now=now,
                cycle_state=self._cycle_state,
                cycle_deadline=self._cycle_deadline,
            )
        except Exception as exc:
            _LOGGER.debug("Reserve diagnostics unavailable: %s", exc)
            return self._reserve_diagnostic_values(observation, None, None)
        return self._reserve_diagnostic_values(
            observation,
            runtime.reserve,
            runtime.strategy.reserve_soc_percent,
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
        reserve_soc, battery_energy, reserve_target, reserve_balance = self._reserve_diagnostic_values(
            None if runtime is None else runtime.solis,
            None if runtime is None else runtime.reserve,
            None if runtime is None else runtime.strategy.reserve_soc_percent,
        )
        return Snapshot(
            heartbeat_at=now,
            health=health,
            action=action,
            reason=reason,
            cycle_state=self._cycle_state,
            cycle_deadline=self._cycle_deadline,
            reserve_soc_percent=reserve_soc,
            battery_energy_kwh=battery_energy,
            reserve_target_energy_kwh=reserve_target,
            reserve_balance_kwh=reserve_balance,
            state_of_charge_percent=None if telemetry is None else telemetry.state_of_charge_percent,
            battery_power_kw=None if telemetry is None else telemetry.battery_power_kw,
            current_cheap_window=_window_text(None if runtime is None else runtime.current_window),
            next_cheap_window=_window_text(None if runtime is None else runtime.next_window),
            last_healthy_at=self._last_healthy_at,
            last_error=last_error,
            actuation_message=None if actuation is None else actuation.message,
        )

    def _reserve_diagnostic_values(
        self,
        observation: object | None,
        reserve: ReservePlanResult | None,
        reserve_soc_percent: Decimal | None,
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
        """Return exact energy diagnostics derived from one observed SOC."""

        target = None if reserve is None else reserve.reserve_energy_kwh
        snapshot = None if observation is None else getattr(observation, "snapshot", None)
        telemetry = None if snapshot is None else getattr(snapshot, "telemetry", None)
        if telemetry is None and observation is not None:
            telemetry = getattr(observation, "telemetry", None)
        soc = None if telemetry is None else getattr(telemetry, "state_of_charge_percent", None)
        if not isinstance(soc, Decimal):
            return reserve_soc_percent, None, target, None
        actual = self.config.battery.capacity_kwh * soc / Decimal(FULL_SOC_PERCENT)
        if target is None:
            return reserve_soc_percent, actual, None, None
        return reserve_soc_percent, actual, target, actual - target

    async def _async_source_changed(self, _event: Event[EventStateChangedData]) -> None:
        if self._stopping:
            return
        await self.async_request_refresh()

    def _is_safe_state_proven(self, observation: object) -> bool:
        """Return whether a healthy snapshot proves the disabled baseline."""

        snapshot = getattr(observation, "snapshot", None)
        if snapshot is None:
            return False
        try:
            persistent = snapshot.persistent
            slots = snapshot.slots
            if (
                persistent.storage_mode != StorageMode.SELF_USE.value
                or persistent.battery_reserve is not False
                or len(slots) != len(self.config.solis.slots)
            ):
                return False
            return all(
                direction.enabled is False
                for slot in slots
                for direction in (slot.charge, slot.discharge)
            )
        except (AttributeError, TypeError):
            return False

    def _source_entity_ids(self) -> tuple[str, ...]:
        telemetry = self.config.solis.telemetry
        return (
            self.config.control_disable_guard_entity_id,
            self.config.tariff.import_rates_entity_id,
            self.config.tariff.export_rates_entity_id,
            self.config.cycle_discharge_duration_entity_id,
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


def _issues_text(observation: object) -> str | None:
    issues = getattr(observation, "issues", ())
    messages = [getattr(issue, "message", str(issue)) for issue in issues]
    return "; ".join(messages) or None
