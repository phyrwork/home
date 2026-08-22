"""Read-only observation coordinator and bounded fail-safe heartbeat."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import battery, controller, inputs, planner
from .config import Config
from .const import DOMAIN
from .contracts import ControllerHealth
from .dependencies import solis_cloud
from .ha_writer import HomeAssistantWriter
from .solis_policy import PolicyActuationResult, SolisPolicyActuator
from .solis_reader import read_solis_state
from .solis_state import SolisStateReadResult

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = timedelta(minutes=1)
HEARTBEAT_STALE_AFTER = timedelta(minutes=3)
FAIL_SAFE_ATTEMPT_BUDGET = timedelta(seconds=45)
SHUTDOWN_FAIL_SAFE_BUDGET = timedelta(seconds=45)
OBSERVATION_ONLY_LEGACY_POWER_LIMIT = "OBSERVATION_ONLY_LEGACY_POWER_LIMIT"


@dataclass(frozen=True, slots=True)
class Decision:
    """A planner recommendation; it is never an applied control."""

    reserve: planner.ReserveInterval
    command: controller.Command


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Immutable result of one heartbeat, including degraded observations."""

    heartbeat_at: datetime | None = None
    last_healthy_at: datetime | None = None
    health: ControllerHealth = ControllerHealth.DEGRADED
    fail_safe_obligation: bool = True
    fail_safe_pending: bool = False
    guard_state: str | None = None
    guard_quality: str = "invalid"
    solis: SolisStateReadResult | None = None
    diagnostic_energy_kwh: Decimal | None = None
    recommendation: Decision | None = None
    reserve: planner.ReserveInterval | None = None
    source_quality: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    unexpected_error: str | None = None
    # Stable legacy diagnostic fields.  They are never interpreted as applied
    # control and remain optional while the observation is degraded.
    decision: Decision | None = None
    battery_spec: battery.Spec | None = None
    battery_state: battery.State | None = None
    input_interval: planner.InputInterval | None = None
    control: solis_cloud.Control | None = None
    planning_horizon_end: datetime | None = None
    tariff_forecast_end: datetime | None = None
    load_forecast_end: datetime | None = None
    solar_forecast_end: datetime | None = None


class Coordinator(DataUpdateCoordinator[Snapshot]):
    """Advance an observable heartbeat and maintain a bounded safety duty."""

    def __init__(self, hass: HomeAssistant, config: Config) -> None:
        super().__init__(hass, _LOGGER, config_entry=None, name=DOMAIN, update_interval=HEARTBEAT_INTERVAL)
        self.config = config
        self._unsub_sources: CALLBACK_TYPE | None = None
        self._last_healthy_at: datetime | None = None
        self._fail_safe_obligation = True
        self._fail_safe_task: asyncio.Task[object] | None = None
        self._fail_safe_diagnostics: tuple[str, ...] = ()
        self._stopping = False
        self._policy: SolisPolicyActuator | None = None
        self._writer: HomeAssistantWriter | None = None

    async def async_start(self) -> None:
        self._stopping = False
        self._fail_safe_obligation = True
        if self._unsub_sources is None:
            self._unsub_sources = async_track_state_change_event(self.hass, self._source_entity_ids(), self._async_source_changed)
        await self.async_refresh()

    async def async_stop(self) -> None:
        self._stopping = True
        if self._unsub_sources is not None:
            self._unsub_sources()
            self._unsub_sources = None
        self._fail_safe_obligation = True
        await self.async_shutdown()
        if self._fail_safe_task is not None and not self._fail_safe_task.done():
            await self._await_task(self._fail_safe_task, SHUTDOWN_FAIL_SAFE_BUDGET)
        else:
            await self._start_fail_safe(SHUTDOWN_FAIL_SAFE_BUDGET, wait=True)
        if self._fail_safe_task is not None and not self._fail_safe_task.done():
            await self._await_task(self._fail_safe_task, SHUTDOWN_FAIL_SAFE_BUDGET)

    async def _async_update_data(self) -> Snapshot:
        """Read all sources and always return a heartbeat snapshot."""
        now = dt_util.now()
        if self._stopping:
            return self._snapshot(now, ())
        issues: list[str] = []
        quality: list[str] = ["SOLIS_READER"]
        unexpected: str | None = None
        guard_state, guard_quality = self._read_guard()
        guard_open = guard_quality == "valid" and guard_state == "off"
        if not guard_open:
            self._fail_safe_obligation = True
        solis = read_solis_state(self.config.solis, self.hass.states, now)
        issues.extend(issue.code for issue in solis.issues)
        energy = self._diagnostic_energy(solis)
        source: planner.Input | None = None
        recommendation: Decision | None = None
        try:
            source = await inputs.async_read_input(self.hass, self.config, now=now, solis_result=solis)
            intervals = planner.fuse_forecasts(now=source.now, tariff_forecast=source.tariff_forecast, load_forecast=source.load_forecast, solar_forecast=source.solar_forecast)
            if not intervals:
                raise ValueError("forecasts do not extend beyond planning time")
            reserves = planner.reserve_intervals(spec=source.battery_spec, intervals=intervals, reserve_margin_kwh=inputs.read_decimal(self.hass, self.config.policy.reserve_margin_entity_id))
            recommendation = Decision(reserves[0], controller.select_command(spec=source.battery_spec, state=source.battery_state, tariff=intervals[0].tariff, reserve=reserves[0], export_hysteresis_kwh=inputs.read_decimal(self.hass, self.config.policy.export_hysteresis_entity_id), previous_command=None))
            quality.append(OBSERVATION_ONLY_LEGACY_POWER_LIMIT)
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, KeyError, LookupError) as error:
            issues.append(f"planner_input_invalid:{error}")
            self._fail_safe_obligation = True
            source = None
        except Exception as error:
            unexpected = f"{type(error).__name__}: {error}"
            issues.append("unexpected_coordinator_exception")
            self._fail_safe_obligation = True
            _LOGGER.exception("Unexpected house battery observation failure")

        complete = solis.is_healthy and guard_quality == "valid" and source is not None and unexpected is None
        if not complete:
            recommendation = None
        if complete:
            self._last_healthy_at = now
        else:
            self._fail_safe_obligation = True
        proven = self._fresh_fail_safe_proof()
        if complete and guard_open and proven and self._fail_safe_task is None:
            self._fail_safe_obligation = False
        elif self._fail_safe_obligation and not proven:
            await self._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
        return self._snapshot(now, tuple(issues), guard_state=guard_state, guard_quality=guard_quality, solis=solis, energy=energy, recommendation=recommendation, source=source, quality=tuple(quality), unexpected_error=unexpected, complete=complete)

    def _snapshot(self, now: datetime, issues: tuple[str, ...], *, guard_state: str | None = None, guard_quality: str = "invalid", solis: SolisStateReadResult | None = None, energy: Decimal | None = None, recommendation: Decision | None = None, source: planner.Input | None = None, quality: tuple[str, ...] = (), unexpected_error: str | None = None, complete: bool = False) -> Snapshot:
        pending = self._fail_safe_task is not None and not self._fail_safe_task.done()
        health = ControllerHealth.FAIL_SAFE if self._fail_safe_obligation or pending else ControllerHealth.HEALTHY if complete else ControllerHealth.DEGRADED
        first = None
        if source is not None:
            intervals = planner.fuse_forecasts(now=source.now, tariff_forecast=source.tariff_forecast, load_forecast=source.load_forecast, solar_forecast=source.solar_forecast)
            first = intervals[0] if intervals else None
        control = None
        if recommendation is not None and source is not None:
            control = solis_cloud.to_control(recommendation.command, source.battery_spec)
        return Snapshot(heartbeat_at=now, last_healthy_at=self._last_healthy_at, health=health, fail_safe_obligation=self._fail_safe_obligation, fail_safe_pending=pending, guard_state=guard_state, guard_quality=guard_quality, solis=solis, diagnostic_energy_kwh=energy, recommendation=recommendation, reserve=None if recommendation is None else recommendation.reserve, source_quality=quality, issues=issues + self._fail_safe_diagnostics, unexpected_error=unexpected_error, decision=recommendation, battery_spec=None if source is None else source.battery_spec, battery_state=None if source is None else source.battery_state, input_interval=first, control=control, planning_horizon_end=None if source is None else max(item.interval.end for item in source.tariff_forecast), tariff_forecast_end=None if source is None else max(item.interval.end for item in source.tariff_forecast), load_forecast_end=None if source is None else max(item.interval.end for item in source.load_forecast), solar_forecast_end=None if source is None else max(item.interval.end for item in source.solar_forecast))

    def _read_guard(self) -> tuple[str | None, str]:
        state = self.hass.states.get(self.config.control_disable_guard_entity_id)
        value = getattr(state, "state", None) if state is not None else None
        return (value, "valid") if value in ("on", "off") else (value, "invalid")

    def _diagnostic_energy(self, result: SolisStateReadResult) -> Decimal | None:
        telemetry = result.telemetry
        if telemetry is None or not telemetry.state_of_charge_percent.is_finite():
            return None
        return self.config.battery.capacity_kwh * telemetry.state_of_charge_percent / Decimal(100)

    def _state_value(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        return getattr(state, "state", None) if state is not None else None

    def _fresh_fail_safe_proof(self) -> bool:
        solis = self.config.solis
        if solis is None:
            return False
        try:
            for slot in solis.slots:
                for direction in (slot.charge, slot.discharge):
                    if self._state_value(direction.enable_entity_id) != "off":
                        return False
            persistent = solis.persistent
            return self._state_value(persistent.storage_mode_entity_id) == "Self-Use" and self._state_value(persistent.grid_peak_shaving_entity_id) == "on" and self._state_value(solis.protection.battery_reserve_entity_id) == "off"
        except Exception:
            return False

    async def _start_fail_safe(self, budget: timedelta, *, wait: bool = False) -> None:
        if self._fail_safe_task is not None and not self._fail_safe_task.done():
            if wait:
                await self._await_task(self._fail_safe_task, budget)
            return
        if self._policy is None:
            try:
                if self.config.solis is None:
                    self._fail_safe_diagnostics = ("solis_config_missing",)
                    return
                self._writer = HomeAssistantWriter.for_home_assistant(self.hass)
                self._policy = SolisPolicyActuator(self.config.solis, self._writer, control_disable_guard_entity_id=self.config.control_disable_guard_entity_id, inverter_timezone=dt_util.get_time_zone(self.hass.config.time_zone))
            except Exception as error:
                self._fail_safe_diagnostics = (f"failsafe_setup:{error}",)
                return
        self._fail_safe_task = asyncio.create_task(self._run_fail_safe(budget))
        self._fail_safe_task.add_done_callback(self._fail_safe_done)
        if wait:
            await self._await_task(self._fail_safe_task, budget)

    async def _run_fail_safe(self, budget: timedelta) -> object:
        assert self._policy is not None
        deadline = dt_util.now() + budget
        try:
            result: PolicyActuationResult = await asyncio.wait_for(self._policy.async_apply_fail_safe(deadline=deadline), budget.total_seconds())
            self._fail_safe_obligation = not result.safe
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_safe_obligation = True
            return error

    def _fail_safe_done(self, task: asyncio.Task[object]) -> None:
        if self._fail_safe_task is task:
            self._fail_safe_task = None
        try:
            result = task.result()
        except asyncio.CancelledError:
            self._fail_safe_obligation = True
            self._fail_safe_diagnostics = ("failsafe_cancelled",)
        except Exception as error:
            self._fail_safe_obligation = True
            self._fail_safe_diagnostics = (f"failsafe_exception:{error}",)
        else:
            if isinstance(result, Exception):
                self._fail_safe_diagnostics = (f"failsafe_exception:{result}",)
            elif isinstance(result, PolicyActuationResult):
                self._fail_safe_diagnostics = tuple(result.issues)
        if not self._stopping:
            self.hass.async_create_task(self.async_request_refresh())

    async def _await_task(self, task: asyncio.Task[object], budget: timedelta) -> None:
        try:
            await asyncio.wait_for(task, budget.total_seconds())
        except asyncio.TimeoutError:
            self._fail_safe_obligation = True
            self._fail_safe_diagnostics = ("failsafe_deadline_exhausted",)
        except asyncio.CancelledError:
            raise

    async def _async_source_changed(self, _event: Event[EventStateChangedData]) -> None:
        if not self._stopping:
            await self.async_request_refresh()

    def _source_entity_ids(self) -> tuple[str, ...]:
        ids = {self.config.control_disable_guard_entity_id, self.config.battery.power_limit_entity_id, self.config.tariff.import_price_entity_id, self.config.tariff.export_price_entity_id, self.config.policy.reserve_margin_entity_id, self.config.policy.export_hysteresis_entity_id}
        solis = self.config.solis
        if solis is not None:
            for section in (solis.telemetry, solis.persistent, solis.protection, solis.capability):
                ids.update(value for field in fields(section) if isinstance(value := getattr(section, field.name), str) and "." in value)
            for slot in solis.slots:
                for direction in (slot.charge, slot.discharge):
                    ids.update(value for field in fields(direction) if isinstance(value := getattr(direction, field.name), str) and "." in value)
        return tuple(sorted(ids))


__all__ = ["Coordinator", "Decision", "FAIL_SAFE_ATTEMPT_BUDGET", "HEARTBEAT_INTERVAL", "HEARTBEAT_STALE_AFTER", "OBSERVATION_ONLY_LEGACY_POWER_LIMIT", "SHUTDOWN_FAIL_SAFE_BUDGET", "Snapshot"]
