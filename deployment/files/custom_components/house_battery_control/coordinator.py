"""Read-only observation coordinator and bounded fail-safe heartbeat."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
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
class FailSafeStateEvidence:
    """One revision-bearing HA state used in a fresh fail-safe proof."""

    entity_id: str
    expected_state: str
    observed_state: str | None
    last_updated: datetime | None
    revision: str | None
    matches: bool


@dataclass(frozen=True, slots=True)
class FailSafeProof:
    """Fresh, complete HA proof with device reconciliation kept explicit."""

    observed_at: datetime
    states: tuple[FailSafeStateEvidence, ...]
    complete: bool
    ha_safe: bool
    device_reconciliation_pending: bool


@dataclass(frozen=True, slots=True)
class FailSafeAttemptEvidence:
    """Lifecycle evidence for the latest bounded fail-safe attempt."""

    attempt_id: int
    started_at: datetime
    deadline: datetime
    completed_at: datetime | None
    status: str
    result: PolicyActuationResult | None
    proof: FailSafeProof | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Immutable result of one heartbeat, including degraded observations."""

    heartbeat_at: datetime | None = None
    last_healthy_at: datetime | None = None
    health: ControllerHealth = ControllerHealth.DEGRADED
    fail_safe_obligation: bool = True
    fail_safe_pending: bool = False
    fail_safe_proof: FailSafeProof | None = None
    fail_safe_attempt: FailSafeAttemptEvidence | None = None
    stale_fail_safe_attempts: tuple[FailSafeAttemptEvidence, ...] = ()
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
        self._last_fail_safe_proof: FailSafeProof | None = None
        self._last_fail_safe_attempt: FailSafeAttemptEvidence | None = None
        self._stale_fail_safe_attempts: tuple[FailSafeAttemptEvidence, ...] = ()
        self._attempt_sequence = 0
        self._attempts_by_task: dict[asyncio.Task[object], FailSafeAttemptEvidence] = {}
        self._proofs_by_attempt: dict[int, FailSafeProof] = {}

    @staticmethod
    def _now() -> datetime:
        try:
            return dt_util.now()
        except Exception:
            return datetime.now(timezone.utc)

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
        existing = self._fail_safe_task
        if existing is not None and not existing.done():
            await self._await_task(existing, SHUTDOWN_FAIL_SAFE_BUDGET)
        proof = self._fresh_fail_safe_proof(self._now())
        self._last_fail_safe_proof = proof
        if not proof.ha_safe:
            await self._start_fail_safe(SHUTDOWN_FAIL_SAFE_BUDGET, wait=True)
        final_proof = self._fresh_fail_safe_proof(self._now())
        self._last_fail_safe_proof = final_proof
        self._fail_safe_obligation = not final_proof.ha_safe
        if not final_proof.ha_safe:
            _LOGGER.error("House battery shutdown fail-safe proof is incomplete")

    async def _async_update_data(self) -> Snapshot:
        """Read all sources and always return a heartbeat snapshot."""
        now = self._now()
        try:
            return await self._async_observe(now)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_safe_obligation = True
            unexpected = f"{type(error).__name__}: {error}"
            _LOGGER.exception("Unexpected house battery observation-cycle failure")
            try:
                await self._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
            except asyncio.CancelledError:
                raise
            except Exception as fail_safe_error:
                self._fail_safe_diagnostics = (
                    f"failsafe_setup:{type(fail_safe_error).__name__}: {fail_safe_error}",
                )
            return Snapshot(
                heartbeat_at=now,
                last_healthy_at=self._last_healthy_at,
                health=ControllerHealth.FAIL_SAFE,
                fail_safe_obligation=True,
                fail_safe_pending=(
                    self._fail_safe_task is not None
                    and not self._fail_safe_task.done()
                ),
                fail_safe_proof=self._last_fail_safe_proof,
                fail_safe_attempt=self._last_fail_safe_attempt,
                stale_fail_safe_attempts=self._stale_fail_safe_attempts,
                issues=("unexpected_coordinator_exception",)
                + self._fail_safe_diagnostics,
                unexpected_error=unexpected,
            )

    async def _async_observe(self, now: datetime) -> Snapshot:
        """Perform one complete observation under the outer exception shell."""
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
        intervals: tuple[planner.InputInterval, ...] = ()
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

        common_horizon_end = intervals[-1].interval.end if intervals else None
        complete = solis.is_healthy and guard_quality == "valid" and source is not None and common_horizon_end is not None and common_horizon_end > now and unexpected is None
        if not complete:
            recommendation = None
        if complete:
            self._last_healthy_at = now
        else:
            self._fail_safe_obligation = True
        proof = self._fresh_fail_safe_proof(now)
        self._last_fail_safe_proof = proof
        if complete and guard_open and proof.ha_safe and self._fail_safe_task is None:
            self._fail_safe_obligation = False
        elif self._fail_safe_obligation and not proof.ha_safe:
            await self._start_fail_safe(FAIL_SAFE_ATTEMPT_BUDGET)
        return self._snapshot(now, tuple(issues), guard_state=guard_state, guard_quality=guard_quality, solis=solis, energy=energy, recommendation=recommendation, source=source, intervals=intervals, quality=tuple(quality), unexpected_error=unexpected, complete=complete)

    def _snapshot(self, now: datetime, issues: tuple[str, ...], *, guard_state: str | None = None, guard_quality: str = "invalid", solis: SolisStateReadResult | None = None, energy: Decimal | None = None, recommendation: Decision | None = None, source: planner.Input | None = None, intervals: tuple[planner.InputInterval, ...] | list[planner.InputInterval] = (), quality: tuple[str, ...] = (), unexpected_error: str | None = None, complete: bool = False) -> Snapshot:
        pending = self._fail_safe_task is not None and not self._fail_safe_task.done()
        health = ControllerHealth.FAIL_SAFE if self._fail_safe_obligation or pending else ControllerHealth.HEALTHY if complete else ControllerHealth.DEGRADED
        first = None
        if source is not None:
            first = intervals[0] if intervals else None
        control = None
        if recommendation is not None and source is not None:
            control = solis_cloud.to_control(recommendation.command, source.battery_spec)
        return Snapshot(heartbeat_at=now, last_healthy_at=self._last_healthy_at, health=health, fail_safe_obligation=self._fail_safe_obligation, fail_safe_pending=pending, fail_safe_proof=self._last_fail_safe_proof, fail_safe_attempt=self._last_fail_safe_attempt, stale_fail_safe_attempts=self._stale_fail_safe_attempts, guard_state=guard_state, guard_quality=guard_quality, solis=solis, diagnostic_energy_kwh=energy, recommendation=recommendation, reserve=None if recommendation is None else recommendation.reserve, source_quality=quality, issues=issues + self._fail_safe_diagnostics, unexpected_error=unexpected_error, decision=recommendation, battery_spec=None if source is None else source.battery_spec, battery_state=None if source is None else source.battery_state, input_interval=first, control=control, planning_horizon_end=None if not intervals else intervals[-1].interval.end, tariff_forecast_end=None if source is None else max(item.interval.end for item in source.tariff_forecast), load_forecast_end=None if source is None else max(item.interval.end for item in source.load_forecast), solar_forecast_end=None if source is None else max(item.interval.end for item in source.solar_forecast))

    def _read_guard(self) -> tuple[str | None, str]:
        state = self.hass.states.get(self.config.control_disable_guard_entity_id)
        value = getattr(state, "state", None) if state is not None else None
        return (value, "valid") if value in ("on", "off") else (value, "invalid")

    def _diagnostic_energy(self, result: SolisStateReadResult) -> Decimal | None:
        telemetry = result.telemetry
        if telemetry is None or not telemetry.state_of_charge_percent.is_finite():
            return None
        return self.config.battery.capacity_kwh * telemetry.state_of_charge_percent / Decimal(100)

    def _fresh_fail_safe_proof(self, observed_at: datetime) -> FailSafeProof:
        solis = self.config.solis
        if solis is None:
            return FailSafeProof(observed_at, (), False, False, False)
        required: list[tuple[str, str]] = []
        for slot in solis.slots:
            for direction in (slot.charge, slot.discharge):
                required.append((direction.enable_entity_id, "off"))
        required.extend(
            (
                (solis.persistent.storage_mode_entity_id, "Self-Use"),
                (solis.persistent.grid_peak_shaving_entity_id, "on"),
                (solis.protection.battery_reserve_entity_id, "off"),
            )
        )
        evidence: list[FailSafeStateEvidence] = []
        for entity_id, expected in required:
            state = self.hass.states.get(entity_id)
            observed = getattr(state, "state", None) if state is not None else None
            updated = getattr(state, "last_updated", None) if state is not None else None
            context = getattr(state, "context", None) if state is not None else None
            revision = getattr(context, "id", None) if context is not None else None
            valid_revision = isinstance(revision, str) and bool(revision)
            valid_time = isinstance(updated, datetime) and updated.tzinfo is not None and updated.utcoffset() is not None
            evidence.append(
                FailSafeStateEvidence(
                    entity_id,
                    expected,
                    observed if isinstance(observed, str) else None,
                    updated if valid_time else None,
                    revision if valid_revision else None,
                    observed == expected and valid_time and valid_revision,
                )
            )
        states = tuple(evidence)
        complete = len(states) == 15 and all(
            item.observed_state is not None
            and item.last_updated is not None
            and item.revision is not None
            for item in states
        )
        ha_safe = complete and all(item.matches for item in states)
        return FailSafeProof(
            observed_at,
            states,
            complete,
            ha_safe,
            device_reconciliation_pending=ha_safe,
        )

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
        started_at = self._now()
        deadline = started_at + budget
        self._attempt_sequence += 1
        attempt_id = self._attempt_sequence
        evidence = FailSafeAttemptEvidence(
            attempt_id,
            started_at,
            deadline,
            None,
            "pending",
            None,
            None,
        )
        task = asyncio.create_task(
            self._run_fail_safe(attempt_id, deadline, budget)
        )
        self._last_fail_safe_attempt = evidence
        self._fail_safe_task = task
        self._attempts_by_task[task] = evidence
        task.add_done_callback(self._fail_safe_done)
        if wait:
            await self._await_task(task, budget)

    async def _run_fail_safe(self, attempt_id: int, deadline: datetime, budget: timedelta) -> object:
        assert self._policy is not None
        try:
            result: PolicyActuationResult = await asyncio.wait_for(self._policy.async_apply_fail_safe(deadline=deadline), budget.total_seconds())
            proof = self._fresh_fail_safe_proof(self._now())
            self._proofs_by_attempt[attempt_id] = proof
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._proofs_by_attempt[attempt_id] = self._fresh_fail_safe_proof(
                self._now()
            )
            return error

    def _fail_safe_done(self, task: asyncio.Task[object]) -> None:
        prior = self._attempts_by_task.pop(task, None)
        if prior is None:
            return
        is_current = (
            self._last_fail_safe_attempt is not None
            and self._last_fail_safe_attempt.attempt_id == prior.attempt_id
        )
        if self._fail_safe_task is task and is_current:
            self._fail_safe_task = None
        result_value: PolicyActuationResult | None = None
        status = "failed"
        diagnostics: tuple[str, ...] = ()
        try:
            result = task.result()
        except asyncio.CancelledError:
            diagnostics = ("failsafe_cancelled",)
        except Exception as error:
            diagnostics = (f"failsafe_exception:{error}",)
        else:
            if isinstance(result, Exception):
                diagnostics = (f"failsafe_exception:{result}",)
            elif isinstance(result, PolicyActuationResult):
                result_value = result
                diagnostics = tuple(result.issues)
        proof = self._proofs_by_attempt.pop(prior.attempt_id, None)
        if proof is None:
            proof = self._fresh_fail_safe_proof(self._now())
        if proof.ha_safe:
            status = "ha_safe_device_reconciliation_pending"
        elif result_value is not None:
            status = "unsafe"
        completed = FailSafeAttemptEvidence(
                prior.attempt_id,
                prior.started_at,
                prior.deadline,
                self._now(),
                status,
                result_value,
                proof,
        )
        if is_current:
            self._last_fail_safe_attempt = completed
            self._last_fail_safe_proof = proof
            self._fail_safe_obligation = not proof.ha_safe
            self._fail_safe_diagnostics = diagnostics
        else:
            self._stale_fail_safe_attempts = (
                *self._stale_fail_safe_attempts,
                completed,
            )[-8:]
        if not self._stopping:
            try:
                self.hass.async_create_task(self.async_request_refresh())
            except Exception:
                _LOGGER.exception("Failed to schedule fail-safe completion refresh")

    async def _await_task(self, task: asyncio.Task[object], budget: timedelta) -> None:
        try:
            await asyncio.wait_for(task, budget.total_seconds())
        except asyncio.TimeoutError:
            self._fail_safe_obligation = True
            self._fail_safe_diagnostics = ("failsafe_deadline_exhausted",)
        except asyncio.CancelledError:
            raise
        finally:
            if task.done() and task in self._attempts_by_task:
                self._fail_safe_done(task)

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


__all__ = ["Coordinator", "Decision", "FailSafeAttemptEvidence", "FailSafeProof", "FailSafeStateEvidence", "FAIL_SAFE_ATTEMPT_BUDGET", "HEARTBEAT_INTERVAL", "HEARTBEAT_STALE_AFTER", "OBSERVATION_ONLY_LEGACY_POWER_LIMIT", "SHUTDOWN_FAIL_SAFE_BUDGET", "Snapshot"]
