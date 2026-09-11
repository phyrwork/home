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

from .config import Config, DOMAIN
from .model import (
    ControllerHealth,
    CycleState,
    LogicalIntent,
    MINIMUM_SOC_PERCENT,
    SlotDirection,
    SlotOwner,
    StrategyAction,
)
from .planner import RESERVE_SOC_UNCERTAINTY_PERCENT, Plan, build_plan
from .solis import (
    SlotKey,
    SolisAdapter,
    SolisChange,
    SolisState,
    WriteOutcome,
    WriteResult,
    allocate_intent,
    read_state,
    telemetry_is_fresh,
    planning_observation,
)

_LOGGER = logging.getLogger(__name__)

BACKSTOP_INTERVAL = timedelta(minutes=1)
# Pinned Solis Cloud Control v2.21.0 may spend 30 seconds retrying and then
# another 30 seconds in its final request. Keep one outer bound with room for
# the adapter's 15-second readback, below the three-minute crash sentinel.
WRITE_DEADLINE = timedelta(seconds=80)
SHUTDOWN_TIMEOUT = timedelta(seconds=30)
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
    charge_lease_deadline: datetime | None = None
    reserve_soc_percent: Decimal | None = None
    battery_energy_kwh: Decimal | None = None
    reserve_target_energy_kwh: Decimal | None = None
    reserve_balance_kwh: Decimal | None = None
    control_reserve_soc_percent: Decimal | None = None
    control_reserve_energy_kwh: Decimal | None = None
    control_reserve_balance_kwh: Decimal | None = None
    state_of_charge_percent: Decimal | None = None
    battery_power_kw: Decimal | None = None
    current_cheap_window: str | None = None
    next_cheap_window: str | None = None
    last_healthy_at: datetime | None = None
    last_error: str | None = None
    actuation_message: str | None = None
    degraded_since: datetime | None = None
    telemetry_timestamp: datetime | None = None
    telemetry_stale: bool = True
    last_controls_read_at: datetime | None = None
    schedule_confirmed_at: datetime | None = None
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
        self._cycle_observation_gate: datetime | None = None
        self._charge_lease_deadline: datetime | None = None
        self._bonus_charge_keys: set[SlotKey] = set()
        self._awaiting_off_proof: set[SlotKey] = set()
        self._bonus_lease_fingerprint: Hashable | None = None
        self._last_plan: Plan | None = None
        self._last_healthy_at: datetime | None = None
        self._degraded_since: datetime | None = None
        self._planning_state: SolisState | None = None
        self._last_controls_read_at: datetime | None = None
        self._schedule_confirmed_at: datetime | None = None
        self._confirmed_plan: Plan | None = None
        self._stop_debts: dict[SlotKey, StopDebt] = {}
        self._owned_expiry: dict[SlotKey, datetime] = {}
        self._start_retry: StartRetry | None = None
        self._dirty = False
        self._started = False
        self._stopping = False
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
                    self._publish(
                        now,
                        ControllerHealth.DEGRADED,
                        None,
                        "hard controller invariant failed",
                        None,
                        last_error=f"{type(exc).__name__}: {exc}",
                    )
                    self._dirty = False
                self._schedule_wakeup()
        finally:
            self._worker_task = None
            if self._dirty and not self._stopping:
                self.trigger()

    async def _reconcile(self) -> None:
        await self.solis.refresh_pending(deadline=self._monotonic() + WRITE_DEADLINE.total_seconds())
        now = self._now()
        monotonic = self._monotonic()
        observation = read_state(self.hass, self.config.solis, now=now)
        self._discover_unconditional_stops(observation, now, monotonic)
        self._retire_proven_stops(observation)

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

        if self._awaiting_off_proof and any(
            observation.direction(key).enabled is not False
            for key in self._awaiting_off_proof
        ):
            self._degrade(
                now,
                None,
                "bonus charge stop is awaiting authoritative off proof",
                observation,
                pending_operation="await bonus charge off proof",
            )
            return

        if self._bonus_dispatch_is_off():
            for key in self._bonus_charge_keys:
                self._add_stop(key, now, monotonic)
            if debt := self._due_stop(monotonic):
                await self._attempt_stop(debt, observation, now, monotonic)
            return

        self._planning_state = planning_observation(observation, self._planning_state, self.config.solis)
        if not self.solis.control_issues(observation):
            self._last_controls_read_at = observation.controls_reported_at
        fresh_soc = telemetry_is_fresh(observation, self.config.solis)
        plan = await build_plan(
            self.hass,
            self.config,
            self._planning_state,
            now=now,
            cycle_state=self._cycle_state,
            cycle_deadline=self._cycle_deadline,
            cycle_observation_gate=self._cycle_observation_gate,
            charge_lease_deadline=self._charge_lease_deadline,
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
        if not fresh_soc:
            plan = self._retain_armed_schedule(plan, now)
        self._last_plan = plan

        fingerprint = _bonus_fingerprint(plan, now)
        preserve_standard_cheap_slot = _preserve_standard_cheap_slot(plan, now)
        if self._bonus_charge_keys and self._bonus_lease_fingerprint != fingerprint:
            for key in self._bonus_charge_keys:
                self._add_stop(key, now, monotonic)
            if debt := self._due_stop(monotonic):
                await self._attempt_stop(debt, observation, now, monotonic)
            return

        if control_issues := self.solis.control_issues(observation):
            self._degrade(now, plan, "inverter reads unavailable; retaining tariff schedule",
                          observation, last_error="; ".join(issue.message for issue in control_issues))
            return

        if not self.solis.prerequisites_match(observation, self.config.battery.minimum_soc_percent):
            self._degrade(now, plan, "commissioned settings require correction", observation,
                          last_error="Expected Feed-In Priority, Allow Grid Charging and Battery Reserve on, reserve at 10%")
            return

        for key in self.solis.conflicting_enabled_keys(
            observation,
            plan.intent,
            preserve_standard_cheap_slot=preserve_standard_cheap_slot,
        ):
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
            battery_reserve_soc_percent=self.config.battery.minimum_soc_percent,
            preserve_standard_cheap_slot=preserve_standard_cheap_slot,
        )
        if change is not None:
            generation = self._start_generation(
                plan,
                change,
                preserve_standard_cheap_slot=preserve_standard_cheap_slot,
            )
            await self._attempt_start(change, generation, plan, observation, now, monotonic)
            return
        if not self.solis.intent_matches(
            observation,
            plan.intent,
            battery_reserve_soc_percent=self.config.battery.minimum_soc_percent,
            preserve_standard_cheap_slot=preserve_standard_cheap_slot,
        ):
            self._start_retry = None
            self._degrade(
                now,
                plan,
                "Solis start is blocked by unknown control state",
                observation,
            )
            return

        self._reconstruct_bonus_lease(plan, observation, fingerprint)
        self._owned_expiry.update(self.solis.intent_expiries(
            observation, plan.intent, preserve_standard_cheap_slot=preserve_standard_cheap_slot
        ))
        self._start_retry = None
        self._cycle_state = plan.next_cycle_state
        self._cycle_deadline = plan.cycle_deadline
        self._cycle_observation_gate = plan.cycle_observation_gate
        if plan.action is StrategyAction.CHEAP_CHARGE and plan.charge_lease_deadline is not None:
            self._charge_lease_deadline = plan.charge_lease_deadline
        self._confirmed_plan = plan
        self._schedule_confirmed_at = now
        if fresh_soc:
            self._last_healthy_at = now
        else:
            self._degrade(now, plan, "schedule confirmed; telemetry is stale", observation,
                          last_error=_issues_text(observation))
            return
        self._publish(
            now,
            ControllerHealth.HEALTHY,
            plan,
            _plan_reason(plan),
            observation,
        )

    def _retain_armed_schedule(self, plan: Plan, now: datetime) -> Plan:
        """Retain authorized discharge legs without making new SOC decisions."""
        confirmed = self._confirmed_plan
        segments = () if plan.intent is None else plan.intent.segments
        previous = () if confirmed is None or confirmed.intent is None else confirmed.intent.segments
        cheap = plan.current_cheap_window
        retained = tuple(
            segment for segment in previous
            if _instant(segment.expiry) > _instant(now)
            and (
                (segment.owner is SlotOwner.RESERVE_EXPORT and cheap is None)
                or (segment.owner is not SlotOwner.RESERVE_EXPORT and cheap is not None
                    and _instant(segment.end) <= _instant(cheap.end))
            )
        )
        # A confirmed cycle pair hands over on the inverter clock. Do not
        # replace its armed recharge with a newly calculated immediate slot.
        if confirmed is not None and confirmed.action in (
            StrategyAction.CYCLE_DISCHARGE, StrategyAction.CYCLE_RECHARGE
        ) and retained:
            active = next((s for s in retained if _instant(s.start) <= _instant(now) < _instant(s.end)), retained[0])
            discharge = active.direction is SlotDirection.DISCHARGE
            return replace(plan, intent=LogicalIntent(retained),
                           action=StrategyAction.CYCLE_DISCHARGE if discharge else StrategyAction.CYCLE_RECHARGE,
                           next_cycle_state=CycleState.CYCLE_DISCHARGING if discharge else CycleState.CYCLE_RECHARGING,
                           cycle_deadline=active.end)
        charge = tuple(s for s in segments if s.direction is SlotDirection.CHARGE)
        discharge = tuple(s for s in retained if s.direction is SlotDirection.DISCHARGE)
        kept = charge if charge else discharge
        return replace(plan, intent=LogicalIntent(kept) if kept else None,
                       action=StrategyAction.CHEAP_CHARGE if charge else (
                           StrategyAction.RESERVE_DISCHARGE if discharge else StrategyAction.IDLE),
                       next_cycle_state=CycleState.CHARGING if charge else (
                           CycleState.RESERVE_DISCHARGING if discharge else CycleState.IDLE),
                       cycle_deadline=None)

    def _discover_unconditional_stops(
        self,
        observation: SolisState,
        now: datetime,
        monotonic: float,
    ) -> None:
        telemetry = observation.telemetry
        soc = telemetry.state_of_charge_percent if telemetry_is_fresh(observation, self.config.solis) else None
        for key, expiry in self._owned_expiry.items():
            if _instant(now) >= _instant(expiry):
                self._add_stop(key, now, monotonic)
        for slot in observation.slots:
            for direction in (slot.charge, slot.discharge):
                if direction.enabled is not True:
                    continue
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
                    if not self._is_planned_cycle_charge(direction.key):
                        self._add_stop(direction.key, now, monotonic)
                if direction.key.direction is SlotDirection.DISCHARGE:
                    boundary = target
                    if direction.owner is SlotOwner.RESERVE_EXPORT:
                        boundary += RESERVE_SOC_UNCERTAINTY_PERCENT
                    if soc <= boundary:
                        self._add_stop(direction.key, now, monotonic)

    def _is_planned_cycle_charge(self, key: SlotKey) -> bool:
        plan = self._last_plan
        if (
            plan is None
            or plan.action not in {
                StrategyAction.CYCLE_DISCHARGE,
                StrategyAction.CYCLE_RECHARGE,
            }
            or plan.intent is None
        ):
            return False
        try:
            return any(
                allocated_key == key
                and segment.direction is SlotDirection.CHARGE
                for allocated_key, segment in allocate_intent(
                    self.config.solis,
                    plan.intent,
                    timezone=self._zone,
                    midnight_end=self.config.solis.midnight_end,
                )
            )
        except ValueError:
            return False

    def _bonus_dispatch_is_off(self) -> bool:
        """Return true only for an explicitly reported dispatch ``off``."""

        if not self._bonus_charge_keys:
            return False
        state = self.hass.states.get(self.config.tariff.import_rates_entity_id)
        attributes = getattr(state, "attributes", {})
        source_id = attributes.get("dispatch_source_entity_id")
        source = self.hass.states.get(source_id) if isinstance(source_id, str) else None
        return getattr(source, "state", None) == "off"

    def _retire_proven_stops(self, observation: SolisState) -> None:
        for key, debt in tuple(self._stop_debts.items()):
            if observation.direction(key).enabled is False and not debt.ambiguous:
                self._stop_debts.pop(key, None)
                self._owned_expiry.pop(key, None)
                self._awaiting_off_proof.discard(key)
                self._bonus_charge_keys.discard(key)
        for key in tuple(self._awaiting_off_proof):
            if observation.direction(key).enabled is False:
                self._awaiting_off_proof.discard(key)
                self._bonus_charge_keys.discard(key)
                self._owned_expiry.pop(key, None)
        if not self._bonus_charge_keys:
            self._charge_lease_deadline = None
            self._bonus_lease_fingerprint = None

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
        if debt.next_attempt > monotonic:
            self._degrade(
                now,
                self._last_plan,
                "important slot stop is awaiting retry deadline",
                observation,
                pending_operation=_stop_text(debt.key),
                attempt=debt.attempt,
                next_retry_at=debt.next_retry_at,
            )
            return
        direction = observation.direction(debt.key)
        if direction.enabled is False and not debt.ambiguous:
            self._stop_debts.pop(debt.key, None)
            self._dirty = True
            return
        self._degrade(
            now,
            self._last_plan,
            "important slot stop is in progress",
            observation,
            pending_operation=_stop_text(debt.key),
            attempt=debt.attempt,
            next_retry_at=debt.next_retry_at,
        )
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
            bonus_still_enabled = (
                debt.key in self._bonus_charge_keys
                and observation.direction(debt.key).enabled is True
            )
            if bonus_still_enabled or observation.direction(debt.key).enabled is not False:
                delay = _stop_retry_delay(debt.attempt)
                retry_at = now + delay
                self._stop_debts[debt.key] = replace(
                    debt,
                    attempt=debt.attempt + 1,
                    next_attempt=monotonic + delay.total_seconds(),
                    next_retry_at=retry_at,
                )
                if debt.key in self._bonus_charge_keys:
                    self._awaiting_off_proof.add(debt.key)
            elif observation.direction(debt.key).enabled is False:
                self._stop_debts.pop(debt.key, None)
                self._owned_expiry.pop(debt.key, None)
            if debt.key in self._bonus_charge_keys:
                if observation.direction(debt.key).enabled is False:
                    self._bonus_charge_keys.discard(debt.key)
                    self._awaiting_off_proof.discard(debt.key)
                else:
                    self._awaiting_off_proof.add(debt.key)
            if not self._bonus_charge_keys:
                self._charge_lease_deadline = None
                self._bonus_lease_fingerprint = None
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
        self._stop_debts[debt.key] = replace(
            debt,
            attempt=attempt,
            next_attempt=next_attempt,
            next_retry_at=retry_at,
            ambiguous=ambiguous,
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
        # Record dated authority before dispatch: even a timed-out enable may
        # have reached the inverter and must be cleaned up at its expiry.
        self._remember_slot_attempt(
            change, plan.intent, charge_lease_deadline=plan.charge_lease_deadline,
            bonus_fingerprint=_bonus_fingerprint(plan, now),
        )
        result = await self.solis.apply(
            change,
            deadline=self._monotonic() + WRITE_DEADLINE.total_seconds(),
        )
        if result.success:
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
        if attempt < len(START_RETRY_DELAYS):
            next_attempt = max(monotonic, retry.origin + START_RETRY_DELAYS[attempt].total_seconds())
        else:
            next_attempt = monotonic + MAXIMUM_RETRY_DELAY.total_seconds()
        next_retry_at = now + timedelta(seconds=max(0.0, next_attempt - monotonic))
        self._start_retry = replace(retry, attempt=attempt, next_attempt=next_attempt,
                                    next_retry_at=next_retry_at)
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

    def _remember_slot_attempt(
        self,
        change: SolisChange,
        intent: LogicalIntent | None,
        *,
        charge_lease_deadline: datetime | None = None,
        bonus_fingerprint: Hashable | None = None,
    ) -> None:
        if change.target is not True or intent is None:
            return
        allocated = allocate_intent(
            self.config.solis,
            intent,
            timezone=self._zone,
            midnight_end=self.config.solis.midnight_end,
        )
        for key, segment in allocated:
            if self.config.solis.direction(key).enable_entity_id == change.entity_id:
                self._owned_expiry[key] = segment.expiry
                if (
                    charge_lease_deadline is not None
                    and segment.owner.value == "cheap_charging"
                ):
                    self._bonus_charge_keys.add(key)
                    self._charge_lease_deadline = charge_lease_deadline
                    self._bonus_lease_fingerprint = bonus_fingerprint
                return

    def _reconstruct_bonus_lease(
        self,
        plan: Plan,
        observation: SolisState,
        fingerprint: Hashable | None,
    ) -> None:
        """Rebuild ephemeral ownership from a matching external slot state."""

        if (
            fingerprint is None
            or plan.action is not StrategyAction.CHEAP_CHARGE
            or plan.intent is None
            or plan.charge_lease_deadline is None
        ):
            return
        allocated = allocate_intent(
            self.config.solis,
            plan.intent,
            timezone=self._zone,
            midnight_end=self.config.solis.midnight_end,
        )
        for key, segment in allocated:
            if segment.owner is not SlotOwner.CHEAP_CHARGING:
                continue
            direction = observation.direction(key)
            if direction.enabled is True and direction.owner is SlotOwner.CHEAP_CHARGING:
                self._bonus_charge_keys.add(key)
                self._owned_expiry[key] = segment.expiry
        if self._bonus_charge_keys:
            self._charge_lease_deadline = plan.charge_lease_deadline
            self._bonus_lease_fingerprint = fingerprint

    def _start_generation(
        self,
        plan: Plan,
        change: SolisChange,
        *,
        preserve_standard_cheap_slot: bool = False,
    ) -> Hashable:
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
        intent_token: Hashable = plan.intent
        change_target: Hashable = change.target
        if preserve_standard_cheap_slot and plan.intent is not None and len(plan.intent.segments) == 1:
            segment = plan.intent.segments[0]
            intent_token = (
                segment.owner,
                segment.direction,
                segment.end,
                segment.expiry,
                segment.current,
                segment.target_soc,
            )
            if change.entity_id.startswith("text.") and isinstance(change.target, str):
                change_target = ("standard-cheap-end", change.target.rsplit("-", 1)[-1])
        return (
            plan.action,
            intent_token,
            plan.reserve_soc_percent,
            plan.control_reserve_soc_percent,
            plan.cycle_deadline,
            plan.cycle_observation_gate,
            plan.charge_lease_deadline,
            tuple(source_tokens),
            change.entity_id,
            change_target,
        )

    def _degrade(
        self,
        now: datetime,
        plan: Plan | None,
        reason: str,
        observation: SolisState | None,
        **kwargs: object,
    ) -> None:
        if self._degraded_since is None:
            self._degraded_since = now
        self._publish(now, ControllerHealth.DEGRADED, plan, reason, observation, **kwargs)

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
        if health is ControllerHealth.HEALTHY:
            self._degraded_since = None
        fresh = observation is not None and telemetry_is_fresh(observation, self.config.solis)
        telemetry = observation.telemetry if fresh else (
            None if self._planning_state is None else self._planning_state.telemetry
        )
        if plan is None or plan.issue is not None:
            plan = self._last_plan
        actual = _actual_energy(self.config, telemetry)
        if actual is None and plan is not None:
            actual = plan.battery_energy_kwh
        snapshot = Snapshot(
            heartbeat_at=now,
            health=health,
            action=StrategyAction.IDLE if plan is None else plan.action,
            reason=reason,
            cycle_state=self._cycle_state,
            cycle_deadline=self._cycle_deadline,
            charge_lease_deadline=self._charge_lease_deadline,
            reserve_soc_percent=None if plan is None else plan.reserve_soc_percent,
            battery_energy_kwh=actual,
            reserve_target_energy_kwh=None if plan is None else plan.reserve_energy_kwh,
            reserve_balance_kwh=None if plan is None or actual is None or plan.reserve_energy_kwh is None else actual - plan.reserve_energy_kwh,
            control_reserve_soc_percent=None if plan is None else plan.control_reserve_soc_percent,
            control_reserve_energy_kwh=None if plan is None else plan.control_reserve_energy_kwh,
            control_reserve_balance_kwh=None if plan is None or actual is None or plan.control_reserve_energy_kwh is None else actual - plan.control_reserve_energy_kwh,
            state_of_charge_percent=None if telemetry is None else telemetry.state_of_charge_percent,
            battery_power_kw=None if telemetry is None else telemetry.battery_power_kw,
            current_cheap_window=_window_text(None if plan is None else plan.current_cheap_window),
            next_cheap_window=_window_text(None if plan is None else plan.next_cheap_window),
            last_healthy_at=self._last_healthy_at,
            last_error=last_error,
            actuation_message=None if actuation is None else actuation.message,
            degraded_since=self._degraded_since,
            telemetry_timestamp=None if telemetry is None else telemetry.device_timestamp,
            telemetry_stale=not fresh,
            last_controls_read_at=self._last_controls_read_at,
            schedule_confirmed_at=self._schedule_confirmed_at,
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
        candidates.extend(self._owned_expiry.values())
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
        if self._start_retry is not None:
            candidates.append(now + timedelta(seconds=max(0.0, self._start_retry.next_attempt - monotonic)))
        future = [_instant(candidate) for candidate in candidates if _instant(candidate) > _instant(now)]
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
        deadline = self._monotonic() + SHUTDOWN_TIMEOUT.total_seconds()
        try:
            worker = self._worker_task
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
                done, _ = await asyncio.wait((worker,), timeout=max(0, deadline - self._monotonic()))
                if not done:
                    raise TimeoutError
                if not worker.cancelled():
                    worker.result()
            async with asyncio.timeout_at(deadline):
                await self._shutdown_controls()
        except TimeoutError:
            _LOGGER.error("Battery slot cleanup exceeded %s; native schedules may remain armed",
                          SHUTDOWN_TIMEOUT)
            self._degrade(self._now(), None, "shutdown cleanup incomplete", None,
                          last_error="native slots may remain armed and repeat daily")
        finally:
            await self.async_shutdown()

    async def _shutdown_controls(self) -> None:
        """Best-effort slot-only cleanup within the caller's total budget."""
        deadline = self._monotonic() + SHUTDOWN_TIMEOUT.total_seconds()
        while True:
            await self.solis.refresh_pending(deadline=deadline)
            now = self._now()
            observation = read_state(self.hass, self.config.solis, now=now)
            directions = [direction for slot in observation.slots
                          for direction in (slot.charge, slot.discharge)]
            self._retire_proven_stops(observation)
            for direction in directions:
                if direction.enabled is True:
                    self._add_stop(direction.key, now, self._monotonic())
            if not self._stop_debts and all(d.enabled is False for d in directions):
                return
            self._degrade(now, None, "controller is shutting down", observation,
                          pending_operation="confirm managed slots disabled")
            for key, debt in tuple(self._stop_debts.items()):
                if observation.direction(key).enabled is None:
                    continue
                result = await self.solis.stop(key, deadline=deadline, force=debt.ambiguous)
                if result.success:
                    self._stop_debts.pop(key, None)
                else:
                    self._stop_debts[key] = replace(debt, ambiguous=True)
            await asyncio.sleep(1)


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


def _bonus_fingerprint(plan: Plan, now: datetime) -> Hashable | None:
    """Capture semantic bonus authority, excluding change-driven heartbeat time."""

    window = plan.current_cheap_window
    components = getattr(window, "components", ()) if window is not None else ()
    for component in components:
        interval = component.interval
        now_utc = now.astimezone(timezone.utc)
        if not (
            interval.start.astimezone(timezone.utc)
            <= now_utc
            < interval.end.astimezone(timezone.utc)
        ):
            continue
        rate = component.rate_interval
        if getattr(rate.classification, "value", rate.classification) != "BONUS_CHEAP":
            return None
        export = component.export_interval
        source_start = max((rate.start, export.start), key=_instant)
        source_end = min((rate.end, export.end), key=_instant)
        return (
            source_start.astimezone(timezone.utc),
            source_end.astimezone(timezone.utc),
            rate.import_price,
            rate.source,
            rate.source_event,
            rate.dispatch_source_entity_id,
            rate.source_revision_at.astimezone(timezone.utc),
            export.start.astimezone(timezone.utc),
            export.end.astimezone(timezone.utc),
            export.export_price,
        )
    return None


def _preserve_standard_cheap_slot(plan: Plan, now: datetime) -> bool:
    """Opt into rollover slot reuse only for an active standard-cheap plan."""

    if (
        plan.action is not StrategyAction.CHEAP_CHARGE
        or plan.intent is None
        or len(plan.intent.segments) != 1
        or plan.intent.segments[0].owner is not SlotOwner.CHEAP_CHARGING
        or plan.intent.segments[0].direction is not SlotDirection.CHARGE
    ):
        return False
    window = plan.current_cheap_window
    components = getattr(window, "components", ()) if window is not None else ()
    return any(
        component.rate_interval.classification.value == "STANDARD_CHEAP"
        and component.interval.start.astimezone(timezone.utc)
        <= now.astimezone(timezone.utc)
        < component.interval.end.astimezone(timezone.utc)
        for component in components
    )


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _plan_reason(plan: Plan) -> str:
    return {
        StrategyAction.IDLE: "normal inverter load following; no forced slot",
        StrategyAction.CHEAP_CHARGE: "charge during trusted cheap window",
        StrategyAction.RESERVE_DISCHARGE: "export toward dynamic reserve",
        StrategyAction.CYCLE_DISCHARGE: "create profitable full-SOC headroom",
        StrategyAction.CYCLE_RECHARGE: "restore full SOC after cycling",
    }[plan.action]


def _issues_text(observation: SolisState) -> str | None:
    return "; ".join(issue.message for issue in observation.issues) or None


def _stop_text(key: SlotKey) -> str:
    return f"stop slot {key.physical_slot} {key.direction.value}"


__all__ = [
    "BACKSTOP_INTERVAL",
    "Controller",
    "SHUTDOWN_TIMEOUT",
    "Snapshot",
    "START_RETRY_DELAYS",
    "StopDebt",
]
