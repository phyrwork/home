"""Small, pure strategy selector for the battery controller.

The coordinator supplies already validated inputs and ready-to-write slot
intents.  This module only decides which one action is allowed next; it does
not read Home Assistant, calculate power, or perform writes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from .contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from .domain_constants import FULL_SOC_PERCENT, MINIMUM_SOC_PERCENT
from .octopus_windows import CheapWindow
from .pre_discharge import PreDischargePlanResult, PreDischargePlanningStatus


class StrategyAction(str, Enum):
    FAIL_SAFE = "FAIL_SAFE"
    STOP = "STOP"
    CHEAP_CHARGE = "CHEAP_CHARGE"
    PRE_DISCHARGE = "PRE_DISCHARGE"
    CYCLE_DISCHARGE = "CYCLE_DISCHARGE"
    IDLE = "IDLE"


class CycleState(str, Enum):
    IDLE = "IDLE"
    DISCHARGING = "DISCHARGING"
    CHARGING = "CHARGING"
    STOPPING = "STOPPING"


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    """Validated facts and pre-built candidate intents for one evaluation."""

    now: datetime
    health: ControllerHealth
    control_enabled: bool
    guard_off: bool
    soc_percent: Decimal
    reserve_soc_percent: Decimal
    cheap_window: CheapWindow | None = None
    cheap_charge_intent: SlotIntent | None = None
    pre_discharge_plan: PreDischargePlanResult | None = None
    pre_discharge_intent: SlotIntent | None = None
    cycle_window: CheapWindow | None = None
    cycle_discharge_intent: SlotIntent | None = None
    recharge_duration: timedelta = timedelta(0)
    cycle_state: CycleState = CycleState.IDLE
    maximum_soc_percent: Decimal = Decimal(FULL_SOC_PERCENT)
    minimum_soc_percent: Decimal = Decimal(MINIMUM_SOC_PERCENT)


@dataclass(frozen=True, slots=True)
class StrategyResult:
    action: StrategyAction
    slot: SlotIntent | None
    next_cycle_state: CycleState
    reason: str

    @property
    def intent(self) -> SlotIntent | None:
        """Descriptive alias for callers that call slots intents."""

        return self.slot


def _result(action: StrategyAction, reason: str, *, slot: SlotIntent | None = None, state: CycleState = CycleState.IDLE) -> StrategyResult:
    return StrategyResult(action, slot, state, reason)


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return value


def _aware(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _window_active(window: CheapWindow | None, now: datetime) -> bool:
    return window is not None and window.start <= now < window.end


def _positive_margin(window: CheapWindow | None) -> bool:
    return window is not None and any(item.margin_per_stored_kwh > 0 for item in window.components)


def _candidate(intent: SlotIntent | None, owner: SlotOwner, direction: SlotDirection) -> SlotIntent | None:
    if intent is None:
        return None
    if type(intent) is not SlotIntent or intent.owner is not owner or intent.direction is not direction:
        raise ValueError("candidate intent does not match its strategy")
    return intent


def _safe_intent(intent: SlotIntent, minimum_soc: Decimal) -> SlotIntent:
    """Raise a discharge target to the active safety floor if necessary."""

    if intent.target_soc < minimum_soc:
        return replace(intent, target_soc=minimum_soc)
    return intent


def _valid_inputs(inputs: StrategyInputs) -> None:
    if type(inputs) is not StrategyInputs:
        raise ValueError("strategy input has an unexpected type")
    _aware(inputs.now, "now")
    if type(inputs.health) is not ControllerHealth:
        raise ValueError("health is invalid")
    if type(inputs.control_enabled) is not bool or type(inputs.guard_off) is not bool:
        raise ValueError("control flags are invalid")
    for name in ("soc_percent", "reserve_soc_percent", "maximum_soc_percent", "minimum_soc_percent"):
        value = _decimal(getattr(inputs, name), name)
        if not Decimal(0) <= value <= Decimal(100):
            raise ValueError(f"{name} is outside 0..100")
    if inputs.minimum_soc_percent < Decimal(MINIMUM_SOC_PERCENT):
        raise ValueError("minimum SOC is below the absolute safety floor")
    if inputs.reserve_soc_percent < inputs.minimum_soc_percent:
        raise ValueError("reserve SOC is below the minimum SOC")
    if inputs.maximum_soc_percent < inputs.reserve_soc_percent:
        raise ValueError("maximum SOC is below the reserve SOC")
    if not isinstance(inputs.recharge_duration, timedelta) or inputs.recharge_duration < timedelta(0):
        raise ValueError("recharge duration is invalid")
    if type(inputs.cycle_state) is not CycleState:
        raise ValueError("cycle state is invalid")
    _candidate(inputs.cheap_charge_intent, SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE)
    _candidate(inputs.pre_discharge_intent, SlotOwner.PRE_DISCHARGE, SlotDirection.DISCHARGE)
    _candidate(inputs.cycle_discharge_intent, SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE)


def _cycle_can_start(inputs: StrategyInputs) -> bool:
    window = inputs.cycle_window or inputs.cheap_window
    intent = inputs.cycle_discharge_intent
    charge = inputs.cheap_charge_intent
    if not _window_active(window, inputs.now) or not _positive_margin(window):
        return False
    if intent is None or charge is None or inputs.soc_percent < inputs.maximum_soc_percent:
        return False
    if intent.start > inputs.now or intent.end <= inputs.now:
        return False
    if charge.end > window.end or charge.start > charge.end:
        return False
    remaining = window.end - inputs.now
    return remaining >= (intent.end - max(intent.start, inputs.now)) + inputs.recharge_duration


def _cycle_can_continue(inputs: StrategyInputs) -> bool:
    intent = inputs.cycle_discharge_intent
    window = inputs.cycle_window or inputs.cheap_window
    if intent is None or not _window_active(window, inputs.now) or not _positive_margin(window):
        return False
    if inputs.soc_percent <= max(inputs.minimum_soc_percent, intent.target_soc):
        return False
    remaining = window.end - inputs.now
    return remaining >= inputs.recharge_duration


def select_strategy(inputs: StrategyInputs | object) -> StrategyResult:
    """Select one safe action, returning ``FAIL_SAFE`` for malformed input."""

    try:
        if not isinstance(inputs, StrategyInputs):
            return _result(StrategyAction.FAIL_SAFE, "invalid strategy input")
        _valid_inputs(inputs)
        if inputs.health is not ControllerHealth.HEALTHY:
            return _result(StrategyAction.FAIL_SAFE, "controller is not healthy")
        if not inputs.control_enabled:
            return _result(StrategyAction.FAIL_SAFE, "dynamic control is disabled")
        if not inputs.guard_off:
            return _result(StrategyAction.FAIL_SAFE, "control-disable guard is asserted")
        if inputs.soc_percent < inputs.minimum_soc_percent:
            return _result(StrategyAction.FAIL_SAFE, "SOC is below the safety floor")

        # A stop is deliberately a complete evaluation result.  The caller
        # must observe all slots off before asking for a new direction.
        if inputs.cycle_state is CycleState.STOPPING:
            return _result(StrategyAction.STOP, "complete direction change", state=CycleState.IDLE)

        charge = _candidate(inputs.cheap_charge_intent, SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE)
        pre = _candidate(inputs.pre_discharge_intent, SlotOwner.PRE_DISCHARGE, SlotDirection.DISCHARGE)
        cycle = _candidate(inputs.cycle_discharge_intent, SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE)

        if inputs.cycle_state is CycleState.CHARGING:
            if charge is not None and _window_active(inputs.cheap_window, inputs.now) and inputs.soc_percent < inputs.maximum_soc_percent:
                return _result(StrategyAction.CHEAP_CHARGE, "continue cheap-window charge", slot=charge, state=CycleState.CHARGING)
            return _result(StrategyAction.STOP, "charge window ended or SOC target reached", state=CycleState.STOPPING)

        if inputs.cycle_state is CycleState.DISCHARGING:
            if cycle is not None and _cycle_can_continue(inputs):
                return _result(StrategyAction.CYCLE_DISCHARGE, "continue profitable full-SOC cycle", slot=_safe_intent(cycle, max(inputs.minimum_soc_percent, inputs.reserve_soc_percent)), state=CycleState.DISCHARGING)
            return _result(StrategyAction.STOP, "cycle must stop before recharge or reserve breach", state=CycleState.STOPPING)

        if pre is not None and inputs.pre_discharge_plan is not None:
            plan = inputs.pre_discharge_plan
            if type(plan) is not PreDischargePlanResult:
                raise ValueError("pre-discharge plan has an unexpected type")
            if plan.status is PreDischargePlanningStatus.PLANNED and plan.proposed_start is not None and plan.proposed_end is not None and plan.proposed_start <= inputs.now < plan.proposed_end:
                # Pre-discharge is an independent one-shot intent, not part
                # of the full-SOC charge/discharge cycle state machine.  Keep
                # the cycle state idle so the next evaluation can continue
                # the still-active plan instead of treating it as a cycle
                # continuation and stopping it.
                return _result(StrategyAction.PRE_DISCHARGE, "create planned headroom before cheap window", slot=_safe_intent(pre, max(inputs.minimum_soc_percent, inputs.reserve_soc_percent)))

        if charge is not None and _window_active(inputs.cheap_window, inputs.now) and inputs.soc_percent < inputs.maximum_soc_percent:
            return _result(StrategyAction.CHEAP_CHARGE, "charge during trusted cheap window", slot=charge, state=CycleState.CHARGING)

        if _cycle_can_start(inputs):
            assert cycle is not None
            return _result(StrategyAction.CYCLE_DISCHARGE, "create profitable headroom at full SOC", slot=_safe_intent(cycle, max(inputs.minimum_soc_percent, inputs.reserve_soc_percent)), state=CycleState.DISCHARGING)

        return _result(StrategyAction.IDLE, "no eligible strategy action")
    except (AttributeError, TypeError, ValueError):
        return _result(StrategyAction.FAIL_SAFE, "invalid strategy input")


plan_strategy = select_strategy


__all__ = [
    "CycleState",
    "StrategyAction",
    "StrategyInputs",
    "StrategyResult",
    "plan_strategy",
    "select_strategy",
]
