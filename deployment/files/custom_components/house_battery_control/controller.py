"""Event-driven reconciliation for the commissioned house battery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Hashable

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .config import Config
from .const import DOMAIN
from .domain_constants import MINIMUM_SOC_PERCENT
from .model import (
    ControllerHealth,
    CycleState,
    LogicalIntent,
    SlotDirection,
    StorageMode,
    StrategyAction,
)
from .planner import Plan, build_plan
from .solis import (
    SlotKey,
    SolisAdapter,
    SolisChange,
    SolisState,
    WriteOutcome,
    WriteResult,
    read_state,
    split_intent,
)

_LOGGER = logging.getLogger(__name__)

BACKSTOP_INTERVAL = timedelta(minutes=1)
WRITE_DEADLINE = timedelta(seconds=30)
DEGRADED_FAILSAFE_TIMEOUT = timedelta(minutes=15)
START_RETRY_DELAYS = (timedelta(0), timedelta(seconds=15), timedelta(seconds=60))
MAXIMUM_RETRY_DELAY = timedelta(seconds=60)
AMBIGUOUS_OUTCOMES = frozenset(
    (
        WriteOutcome.SERVICE_ERROR,
        WriteOutcome.SERVICE_TIMEOUT,
        WriteOutcome.READBACK_TIMEOUT,
    )
)


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
    degraded_since: datetime | None = None
    fail_safe_since: datetime | None = None
    pending_operation: str | None = None
    attempt: int | None = None
    next_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StopDebt:
    key: SlotKey
    attempt: int
    next_attempt: float
    next_retry_at: datetime
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class StartRetry:
    generation: Hashable
    origin: float
    attempt: int
    next_attempt: float
    next_retry_at: datetime
    suppressed: bool = False


class Controller(DataUpdateCoordinator[Snapshot]):
    """One dirty/coalescing worker and one Solis change per pass."""

    def __init__(self, hass: HomeAssistant, config: Config) -> None:
        super().__init__(hass, _LOGGER, config_entry=None, name=DOMAIN)
        self.config = config
        zone = dt_util.get_time_zone(hass.config.time_zone)
        if zone is None:
            raise ValueError(f"Unknown Home Assistant timezone: {hass.config.time_zone}")
        self.solis = SolisAdapter(hass, config.solis, timezone=zone)
        self._zone = zone
        self._cycle_state = CycleState.IDLE
        self._cycle_deadline: datetime | None = None
        self._last_plan: Plan | None = None
        self._last_healthy_at: datetime | None = None
        self._degraded_since: datetime | None = None
        self._degraded_since_mono: float | None = None
        self._fail_safe_since: datetime | None = None
        self._fail_safe_latched = False
        self._mode_attempt = 0
        self._mode_next_attempt = 0.0
        self._mode_next_retry_at: datetime | None = None
        self._stop_debts: dict[SlotKey, StopDebt] = {}
        self._used_slots: set[SlotKey] = set()
        self._owned_expiry: dict[SlotKey, datetime] = {}
        self._start_retry: StartRetry | None = None
        self._dirty = False
        self._started = False
        self._stopping = False
        self._stopped = False
        self._worker_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._unsub_sources: CALLBACK_TYPE | None = None
        self._unsub_wakeup: CALLBACK_TYPE | None = None

    @staticmethod
    def _now() -> datetime:
        try:
            return dt_util.now().astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    @staticmethod
    def _monotonic() -> float:
        return asyncio.get_running_loop().time()

    async def async_start(self) -> None:
        if self._started or self._stopping:
            return
        self._started = True
        self._unsub_sources = async_track_state_change_event(
            self.hass,
            self.source_entity_ids(),
            self._source_changed,
        )
        self.trigger()
        task = self._worker_task
        if task is not None:
            await asyncio.shield(task)

    def trigger(self) -> None:
        """Mark dirty without creating a second reconciliation worker."""

        if self._stopping:
            return
        self._dirty = True
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = self.hass.async_create_task(
                self._worker(),
                f"{DOMAIN} reconciliation",
                eager_start=False,
            )

    async def async_reconcile_now(self) -> None:
        """Trigger reconciliation and wait until the current dirty burst drains."""

        self.trigger()
        task = self._worker_task
        if task is not None:
            await asyncio.shield(task)

    async def _worker(self) -> None:
        try:
            while self._dirty and not self._stopping:
                self._dirty = False
                try:
                    await self._reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOGGER.exception("House battery reconciliation invariant failed")
                    now = self._now()
                    self._latch_fail_safe(now)
                    self._publish(
                        now,
                        ControllerHealth.FAIL_SAFE,
                        None,
                        "hard controller invariant failed",
                        None,
                        last_error=f"{type(exc).__name__}: {exc}",
                        pending_operation="select Self-Use",
                    )
                    self._dirty = True
                self._schedule_wakeup()
        finally:
            self._worker_task = None
            if self._dirty and not self._stopping:
                self.trigger()

    async def _reconcile(self) -> None:
        now = self._now()
        monotonic = self._monotonic()
        if (
            not self._fail_safe_latched
            and self._degraded_since_mono is not None
            and monotonic - self._degraded_since_mono
            >= DEGRADED_FAILSAFE_TIMEOUT.total_seconds()
        ):
            self._latch_fail_safe(now)

        observation = read_state(self.hass, self.config.solis, now=now)
        self._discover_unconditional_stops(observation, now, monotonic)
        self._retire_proven_stops(observation)

        if self._fail_safe_latched:
            await self._reconcile_fail_safe(observation, now, monotonic)
            return

        if debt := self._due_stop(monotonic):
            await self._attempt_stop(debt, observation, now, monotonic)
            return
        if self._stop_debts:
            debt = min(self._stop_debts.values(), key=lambda item: item.next_attempt)
            self._degrade(
                now,
                None,
                "important slot stop is awaiting retry",
                observation,
                pending_operation=_stop_text(debt.key),
                attempt=debt.attempt,
                next_retry_at=debt.next_retry_at,
            )
            return

        if observation.health is not ControllerHealth.HEALTHY:
            self._degrade(
                now,
                None,
                "Solis telemetry or controls are temporarily unavailable",
                observation,
                last_error=_issues_text(observation),
            )
            return

        plan = await build_plan(
            self.hass,
            self.config,
            observation,
            now=now,
            cycle_state=self._cycle_state,
            cycle_deadline=self._cycle_deadline,
        )
        if plan.issue is not None:
            self._degrade(
                now,
                plan,
                "planning inputs are temporarily unavailable",
                observation,
                last_error=plan.issue,
            )
            return
        if plan.reserve_soc_percent is None:
            raise ValueError("valid plan has no reserve SOC")
        self._last_plan = plan

        for key in self.solis.conflicting_enabled_keys(observation, plan.intent):
            self._add_stop(key, now, monotonic)
        if debt := self._due_stop(monotonic):
            await self._attempt_stop(debt, observation, now, monotonic)
            return
        if self._stop_debts:
            debt = min(self._stop_debts.values(), key=lambda item: item.next_attempt)
            self._degrade(
                now,
                plan,
                "direction change is waiting for confirmed stop",
                observation,
                pending_operation=_stop_text(debt.key),
                attempt=debt.attempt,
                next_retry_at=debt.next_retry_at,
            )
            return

        change = self.solis.next_start_change(
            observation,
            plan.intent,
            reserve_soc_percent=plan.reserve_soc_percent,
        )
        generation = self._start_generation(plan, observation)
        if change is not None:
            await self._attempt_start(change, generation, plan, observation, now, monotonic)
            return
        if not self.solis.intent_matches(
            observation,
            plan.intent,
            reserve_soc_percent=plan.reserve_soc_percent,
        ):
            self._start_retry = None
            self._degrade(
                now,
                plan,
                "Solis start is blocked by unknown control state",
                observation,
            )
            return

        self._start_retry = None
        self._cycle_state = plan.next_cycle_state
        self._cycle_deadline = plan.cycle_deadline
        if await self._housekeep_one(observation, plan, now, monotonic):
            return
        self._last_healthy_at = now
        self._publish(
            now,
            ControllerHealth.HEALTHY,
            plan,
            _plan_reason(plan),
            observation,
        )

    def _discover_unconditional_stops(
        self,
        observation: SolisState,
        now: datetime,
        monotonic: float,
    ) -> None:
        telemetry = observation.telemetry
        soc = None if telemetry is None else telemetry.state_of_charge_percent
        for slot in observation.slots:
            for direction in (slot.charge, slot.discharge):
                if direction.enabled is not True:
                    continue
                expiry = self._owned_expiry.get(direction.key)
                if expiry is not None and now >= expiry:
                    self._add_stop(direction.key, now, monotonic)
                if (
                    soc is not None
                    and direction.key.direction is SlotDirection.DISCHARGE
                    and soc <= Decimal(MINIMUM_SOC_PERCENT)
                ):
                    self._add_stop(direction.key, now, monotonic)
                target = None if direction.target_soc is None else direction.target_soc.current_value
                if soc is None or target is None:
                    continue
                if direction.key.direction is SlotDirection.CHARGE and soc >= target:
                    self._add_stop(direction.key, now, monotonic)
                if direction.key.direction is SlotDirection.DISCHARGE and (
                    soc <= target
                ):
                    self._add_stop(direction.key, now, monotonic)

    def _retire_proven_stops(self, observation: SolisState) -> None:
        for key, debt in tuple(self._stop_debts.items()):
            if observation.direction(key).enabled is False and not debt.ambiguous:
                self._stop_debts.pop(key, None)
                self._owned_expiry.pop(key, None)

    def _add_stop(self, key: SlotKey, now: datetime, monotonic: float) -> None:
        self.config.solis.direction(key)
        if key not in self._stop_debts:
            self._stop_debts[key] = StopDebt(key, 0, monotonic, now)

    def _due_stop(self, monotonic: float) -> StopDebt | None:
        due = [debt for debt in self._stop_debts.values() if debt.next_attempt <= monotonic]
        return min(due, key=lambda item: (item.next_attempt, item.key.physical_slot, item.key.direction.value)) if due else None

    async def _attempt_stop(
        self,
        debt: StopDebt,
        observation: SolisState,
        now: datetime,
        monotonic: float,
    ) -> None:
        direction = observation.direction(debt.key)
        if direction.enabled is False and not debt.ambiguous:
            self._stop_debts.pop(debt.key, None)
            self._dirty = True
            return
        try:
            result = await self.solis.stop(
                debt.key,
                deadline=self._monotonic() + WRITE_DEADLINE.total_seconds(),
                force=debt.ambiguous,
            )
        except asyncio.CancelledError:
            self._stop_debts[debt.key] = replace(
                debt,
                next_attempt=self._monotonic(),
                next_retry_at=self._now(),
                ambiguous=True,
            )
            self._dirty = True
            raise
        if result.success:
            self._stop_debts.pop(debt.key, None)
            self._owned_expiry.pop(debt.key, None)
            self._dirty = True
            self._degrade(
                now,
                self._last_plan,
                "important slot stop received provisional proof",
                observation,
                actuation=result,
            )
            return
        attempt = debt.attempt + 1
        delay = _stop_retry_delay(debt.attempt)
        ambiguous = debt.ambiguous or result.outcome in AMBIGUOUS_OUTCOMES
        next_attempt = monotonic + delay.total_seconds()
        retry_at = now + delay
        self._stop_debts[debt.key] = StopDebt(
            debt.key,
            attempt,
            next_attempt,
            retry_at,
            ambiguous,
        )
        self._degrade(
            now,
            self._last_plan,
            "important slot stop did not complete and will retry",
            observation,
            last_error=result.message,
            actuation=result,
            pending_operation=_stop_text(debt.key),
            attempt=attempt,
            next_retry_at=retry_at,
        )

    async def _attempt_start(
        self,
        change: SolisChange,
        generation: Hashable,
        plan: Plan,
        observation: SolisState,
        now: datetime,
        monotonic: float,
    ) -> None:
        retry = self._start_retry
        if retry is None or retry.generation != generation:
            retry = StartRetry(generation, monotonic, 0, monotonic, now)
            self._start_retry = retry
        if retry.suppressed:
            self._degrade(
                now,
                plan,
                "best-effort start is suppressed for this unchanged generation",
                observation,
                pending_operation=f"start {change.entity_id}",
                attempt=retry.attempt,
            )
            return
        if retry.next_attempt > monotonic:
            self._degrade(
                now,
                plan,
                "best-effort start is awaiting retry",
                observation,
                pending_operation=f"start {change.entity_id}",
                attempt=retry.attempt,
                next_retry_at=retry.next_retry_at,
            )
            return
        result = await self.solis.apply(
            change,
            deadline=self._monotonic() + WRITE_DEADLINE.total_seconds(),
        )
        if result.success:
            self._remember_enabled_slot(change, plan.intent)
            self._start_retry = None
            self._dirty = True
            self._degrade(
                now,
                plan,
                "Solis reconciliation advanced one start change",
                observation,
                actuation=result,
            )
            return
        attempt = retry.attempt + 1
        if attempt >= len(START_RETRY_DELAYS):
            self._start_retry = replace(retry, attempt=attempt, suppressed=True)
            next_retry_at = None
        else:
            next_attempt = retry.origin + START_RETRY_DELAYS[attempt].total_seconds()
            next_retry_at = now + timedelta(seconds=max(0.0, next_attempt - monotonic))
            self._start_retry = replace(
                retry,
                attempt=attempt,
                next_attempt=next_attempt,
                next_retry_at=next_retry_at,
            )
        self._degrade(
            now,
            plan,
            "best-effort start did not complete",
            observation,
            last_error=result.message,
            actuation=result,
            pending_operation=f"start {change.entity_id}",
            attempt=attempt,
            next_retry_at=next_retry_at,
        )

    def _remember_enabled_slot(
        self,
        change: SolisChange,
        intent: LogicalIntent | None,
    ) -> None:
        if change.target is not True or intent is None:
            return
        split = split_intent(
            intent,
            timezone=self._zone,
            midnight_end=self.config.solis.midnight_end,
        )
        allocation = self.config.solis.allocation(split.segments[0].owner)
        for key, segment in zip(allocation, split.segments):
            if self.config.solis.direction(key).enable_entity_id == change.entity_id:
                self._used_slots.add(key)
                self._owned_expiry[key] = segment.expiry
                return

    async def _housekeep_one(
        self,
        observation: SolisState,
        plan: Plan,
        now: datetime,
        monotonic: float,
    ) -> bool:
        del monotonic
        for key in sorted(self._used_slots, key=lambda item: (item.physical_slot, item.direction.value)):
            if key in self._stop_debts:
                continue
            change = self.solis.next_housekeeping_change(observation, key)
            if change is None:
                if observation.direction(key).enabled is False:
                    self._used_slots.discard(key)
                continue
            result = await self.solis.apply(
                change,
                deadline=self._monotonic() + WRITE_DEADLINE.total_seconds(),
            )
            if result.success:
                self._dirty = True
            self._last_healthy_at = now
            self._publish(
                now,
                ControllerHealth.HEALTHY,
                plan,
                "best-effort used-slot housekeeping",
                observation,
                last_error=None if result.success else result.message,
                actuation=result,
                pending_operation=f"housekeep slot {key.physical_slot} {key.direction.value}",
            )
            return True
        return False

    async def _reconcile_fail_safe(
        self,
        observation: SolisState,
        now: datetime,
        monotonic: float,
    ) -> None:
        # Important stops make progress independently of mode read/write
        # reliability. Shutdown alone requires strict Self-Use-first ordering.
        if debt := self._due_stop(monotonic):
            await self._attempt_stop(debt, observation, now, monotonic)
            return
        mode = None if observation.persistent is None else observation.persistent.storage_mode
        if mode != StorageMode.SELF_USE.value:
            if monotonic < self._mode_next_attempt:
                self._publish(
                    now,
                    ControllerHealth.FAIL_SAFE,
                    None,
                    "latched fail-safe is awaiting Self-Use retry",
                    observation,
                    pending_operation="select Self-Use",
                    attempt=self._mode_attempt,
                    next_retry_at=self._mode_next_retry_at,
                )
                return
            result = await self.solis.set_mode(
                StorageMode.SELF_USE,
                deadline=self._monotonic() + WRITE_DEADLINE.total_seconds(),
            )
            if result.success:
                self._mode_attempt = 0
                self._mode_next_attempt = monotonic
                self._mode_next_retry_at = None
                self._dirty = True
            else:
                delay = _stop_retry_delay(self._mode_attempt)
                self._mode_attempt += 1
                self._mode_next_attempt = monotonic + delay.total_seconds()
                self._mode_next_retry_at = now + delay
            self._publish(
                now,
                ControllerHealth.FAIL_SAFE,
                None,
                "latched fail-safe is selecting Self-Use",
                observation,
                last_error=None if result.success else result.message,
                actuation=result,
                pending_operation="select Self-Use",
                attempt=self._mode_attempt,
                next_retry_at=self._mode_next_retry_at,
            )
            return

        debt = min(self._stop_debts.values(), key=lambda item: item.next_attempt) if self._stop_debts else None
        self._publish(
            now,
            ControllerHealth.FAIL_SAFE,
            None,
            "latched mode-only Self-Use fail-safe",
            observation,
            pending_operation=None if debt is None else _stop_text(debt.key),
            attempt=None if debt is None else debt.attempt,
            next_retry_at=None if debt is None else debt.next_retry_at,
        )

    def _start_generation(self, plan: Plan, observation: SolisState) -> Hashable:
        source_tokens: list[Hashable] = []
        for entity_id in (
            self.config.tariff.import_rates_entity_id,
            self.config.tariff.export_rates_entity_id,
            self.config.cycle_discharge_duration_entity_id,
        ):
            state = self.hass.states.get(entity_id)
            source_tokens.append(
                (
                    entity_id,
                    getattr(state, "state", None),
                    getattr(state, "last_updated", None),
                    getattr(getattr(state, "context", None), "id", None),
                )
            )
        control_tokens = tuple(
            (entity_id, revision.state, revision.last_updated, revision.context_id)
            for entity_id, revision in sorted(observation.revisions.items())
        )
        return (
            plan.action,
            plan.intent,
            plan.reserve_soc_percent,
            plan.cycle_deadline,
            tuple(source_tokens),
            control_tokens,
        )

    def _degrade(
        self,
        now: datetime,
        plan: Plan | None,
        reason: str,
        observation: SolisState | None,
        **kwargs: object,
    ) -> None:
        if self._degraded_since_mono is None:
            self._degraded_since_mono = self._monotonic()
            self._degraded_since = now
        if (
            self._monotonic() - self._degraded_since_mono
            >= DEGRADED_FAILSAFE_TIMEOUT.total_seconds()
        ):
            self._latch_fail_safe(now)
            self._dirty = True
        health = ControllerHealth.FAIL_SAFE if self._fail_safe_latched else ControllerHealth.DEGRADED
        self._publish(now, health, plan, reason, observation, **kwargs)

    def _latch_fail_safe(self, now: datetime) -> None:
        if not self._fail_safe_latched:
            self._fail_safe_latched = True
            self._fail_safe_since = now
            self._start_retry = None

    def _publish(
        self,
        now: datetime,
        health: ControllerHealth,
        plan: Plan | None,
        reason: str,
        observation: SolisState | None,
        *,
        last_error: str | None = None,
        actuation: WriteResult | None = None,
        pending_operation: str | None = None,
        attempt: int | None = None,
        next_retry_at: datetime | None = None,
    ) -> None:
        if health is ControllerHealth.HEALTHY and not self._fail_safe_latched:
            self._degraded_since = None
            self._degraded_since_mono = None
        telemetry = None if observation is None else observation.telemetry
        actual = None if plan is None else plan.battery_energy_kwh
        if actual is None:
            actual = _actual_energy(self.config, telemetry)
        snapshot = Snapshot(
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
            degraded_since=self._degraded_since,
            fail_safe_since=self._fail_safe_since,
            pending_operation=pending_operation,
            attempt=attempt,
            next_retry_at=next_retry_at,
        )
        self.async_set_updated_data(snapshot)

    def _schedule_wakeup(self) -> None:
        if self._unsub_wakeup is not None:
            self._unsub_wakeup()
            self._unsub_wakeup = None
        if self._stopping:
            return
        now = self._now()
        monotonic = self._monotonic()
        candidates = [_next_minute(now)]
        plan = self._last_plan
        if plan is not None:
            for window in (plan.current_cheap_window, plan.next_cheap_window):
                if window is not None:
                    candidates.extend((window.start, window.end))
            if plan.intent is not None:
                for segment in plan.intent.segments:
                    candidates.extend((segment.start, segment.end, segment.expiry))
            if plan.cycle_deadline is not None:
                candidates.append(plan.cycle_deadline)
        for debt in self._stop_debts.values():
            candidates.append(now + timedelta(seconds=max(0.0, debt.next_attempt - monotonic)))
        if self._start_retry is not None and not self._start_retry.suppressed:
            candidates.append(now + timedelta(seconds=max(0.0, self._start_retry.next_attempt - monotonic)))
        if self._mode_next_retry_at is not None:
            candidates.append(self._mode_next_retry_at)
        if self._degraded_since is not None and not self._fail_safe_latched:
            candidates.append(self._degraded_since + DEGRADED_FAILSAFE_TIMEOUT)
        future = [candidate.astimezone(timezone.utc) for candidate in candidates if candidate > now]
        wake_at = min(future) if future else now + timedelta(milliseconds=100)
        self._unsub_wakeup = async_track_point_in_utc_time(
            self.hass,
            self._wakeup,
            wake_at,
        )

    @callback
    def _source_changed(self, _event: Event) -> None:
        self.trigger()

    @callback
    def _wakeup(self, _now: datetime) -> None:
        self._unsub_wakeup = None
        self.trigger()

    def source_entity_ids(self) -> tuple[str, ...]:
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
                entity_ids.extend(
                    (
                        direction.enable_entity_id,
                        direction.time_entity_id,
                        direction.current_entity_id,
                        direction.target_soc_entity_id,
                    )
                )
        return tuple(dict.fromkeys(entity_ids))

    async def async_stop(self) -> None:
        if self._stop_task is None:
            self._stopping = True
            self._stop_task = self.hass.async_create_task(
                self._stop_once(),
                f"{DOMAIN} shutdown",
                eager_start=False,
            )
        await asyncio.shield(self._stop_task)

    async def _stop_once(self) -> None:
        if self._unsub_sources is not None:
            self._unsub_sources()
            self._unsub_sources = None
        if self._unsub_wakeup is not None:
            self._unsub_wakeup()
            self._unsub_wakeup = None
        worker = self._worker_task
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        now = self._now()
        current_health = ControllerHealth.FAIL_SAFE if self._fail_safe_latched else ControllerHealth.DEGRADED
        self._publish(
            now,
            current_health,
            None,
            "controller is shutting down",
            None,
            pending_operation="select Self-Use",
        )
        await self._shutdown_controls()
        self._stopped = True
        await self.async_shutdown()

    async def _shutdown_controls(self) -> None:
        attempt = 0
        while True:
            observation = read_state(self.hass, self.config.solis, now=self._now())
            mode = None if observation.persistent is None else observation.persistent.storage_mode
            if mode == StorageMode.SELF_USE.value:
                break
            result = await self.solis.set_mode(
                StorageMode.SELF_USE,
                deadline=self._monotonic() + WRITE_DEADLINE.total_seconds(),
            )
            if result.success:
                attempt = 0
                continue
            await asyncio.sleep(_stop_retry_delay(attempt).total_seconds())
            attempt += 1

        shutdown_debt = dict(self._stop_debts)
        attempt = 0
        while True:
            observation = read_state(self.hass, self.config.solis, now=self._now())
            directions = tuple(
                direction
                for slot in observation.slots
                for direction in (slot.charge, slot.discharge)
            )
            for direction in directions:
                if direction.enabled is True and direction.key not in shutdown_debt:
                    shutdown_debt[direction.key] = StopDebt(
                        direction.key,
                        0,
                        self._monotonic(),
                        self._now(),
                    )
            for key, debt in tuple(shutdown_debt.items()):
                enabled = observation.direction(key).enabled
                if enabled is False and not debt.ambiguous:
                    shutdown_debt.pop(key, None)
            unknown = any(direction.enabled is None for direction in directions)
            if not shutdown_debt and not unknown and all(direction.enabled is False for direction in directions):
                return
            debt = next(
                (
                    item
                    for item in sorted(
                        shutdown_debt.values(),
                        key=lambda value: (value.key.physical_slot, value.key.direction.value),
                    )
                    if observation.direction(item.key).enabled is not None
                ),
                None,
            )
            if debt is None:
                await asyncio.sleep(_stop_retry_delay(attempt).total_seconds())
                attempt += 1
                continue
            try:
                result = await self.solis.stop(
                    debt.key,
                    deadline=self._monotonic() + WRITE_DEADLINE.total_seconds(),
                    force=debt.ambiguous,
                )
            except asyncio.CancelledError:
                shutdown_debt[debt.key] = replace(debt, ambiguous=True)
                raise
            if result.success:
                shutdown_debt.pop(debt.key, None)
                attempt = 0
                continue
            shutdown_debt[debt.key] = replace(
                debt,
                attempt=debt.attempt + 1,
                ambiguous=debt.ambiguous or result.outcome in AMBIGUOUS_OUTCOMES,
            )
            await asyncio.sleep(_stop_retry_delay(debt.attempt).total_seconds())


def _stop_retry_delay(attempt: int) -> timedelta:
    return min(timedelta(seconds=5 * (2 ** min(attempt, 20))), MAXIMUM_RETRY_DELAY)


def _next_minute(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0) + BACKSTOP_INTERVAL


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


def _stop_text(key: SlotKey) -> str:
    return f"stop slot {key.physical_slot} {key.direction.value}"


__all__ = [
    "BACKSTOP_INTERVAL",
    "Controller",
    "DEGRADED_FAILSAFE_TIMEOUT",
    "Snapshot",
    "START_RETRY_DELAYS",
    "StopDebt",
]
