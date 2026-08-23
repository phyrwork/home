"""Small, pure strategy selector for the house-battery controller."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from .contracts import ControllerHealth, SlotDirection, SlotIntent, SlotOwner
from .domain_constants import FULL_SOC_PERCENT, MINIMUM_SOC_PERCENT
from .octopus_windows import CheapWindow


class StrategyAction(str, Enum):
    FAIL_SAFE = "FAIL_SAFE"
    STOP = "STOP"
    CHEAP_CHARGE = "CHEAP_CHARGE"
    RESERVE_DISCHARGE = "RESERVE_DISCHARGE"
    CYCLE_DISCHARGE = "CYCLE_DISCHARGE"
    IDLE = "IDLE"


class CycleState(str, Enum):
    IDLE = "IDLE"
    RESERVE_DISCHARGING = "RESERVE_DISCHARGING"
    DISCHARGING = "DISCHARGING"
    CHARGING = "CHARGING"
    STOPPING = "STOPPING"


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    now: datetime
    health: ControllerHealth
    control_enabled: bool
    guard_off: bool
    soc_percent: Decimal
    reserve_soc_percent: Decimal
    cheap_window: CheapWindow | None = None
    cheap_charge_intent: SlotIntent | None = None
    reserve_discharge_intent: SlotIntent | None = None
    cycle_window: CheapWindow | None = None
    cycle_discharge_intent: SlotIntent | None = None
    recharge_duration: timedelta = timedelta(0)
    cycle_discharge_duration: timedelta = timedelta(0)
    cycle_state: CycleState = CycleState.IDLE
    cycle_deadline: datetime | None = None
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
        return self.slot


def _result(action: StrategyAction, reason: str, *, slot: SlotIntent | None = None, state: CycleState = CycleState.IDLE) -> StrategyResult:
    return StrategyResult(action, slot, state, reason)


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return value


def _aware(value: Any, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


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
    if intent.target_soc < minimum_soc:
        return replace(intent, target_soc=minimum_soc)
    return intent


def _effective_reserve(inputs: StrategyInputs, intent: SlotIntent | None) -> Decimal:
    target = inputs.reserve_soc_percent
    if intent is not None:
        target = max(target, intent.target_soc)
    return max(inputs.minimum_soc_percent, target)


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
    if not isinstance(inputs.cycle_discharge_duration, timedelta) or inputs.cycle_discharge_duration <= timedelta(0):
        raise ValueError("cycle discharge duration is invalid")
    if type(inputs.cycle_state) is not CycleState:
        raise ValueError("cycle state is invalid")
    if inputs.cycle_deadline is not None:
        _aware(inputs.cycle_deadline, "cycle deadline")
    _candidate(inputs.cheap_charge_intent, SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE)
    _candidate(inputs.reserve_discharge_intent, SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE)
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
    remaining = window.end - inputs.now
    duration = inputs.cycle_discharge_duration or (intent.end - intent.start)
    return remaining >= duration + inputs.recharge_duration


def _cycle_can_continue(inputs: StrategyInputs) -> bool:
    intent = inputs.cycle_discharge_intent
    window = inputs.cycle_window or inputs.cheap_window
    if intent is None or not _window_active(window, inputs.now) or not _positive_margin(window):
        return False
    deadline = min(
        value
        for value in (
            inputs.cycle_deadline or intent.end,
            intent.end,
            window.end,
        )
    )
    if inputs.now >= deadline or inputs.soc_percent <= max(inputs.minimum_soc_percent, intent.target_soc):
        return False
    remaining_discharge = deadline - inputs.now
    return window.end - inputs.now >= remaining_discharge + inputs.recharge_duration


def select_strategy(inputs: StrategyInputs | object) -> StrategyResult:
    """Select exactly one safe action from validated runtime facts."""

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

        if inputs.cycle_state is CycleState.STOPPING:
            return _result(StrategyAction.STOP, "complete direction change", state=CycleState.IDLE)

        charge = _candidate(inputs.cheap_charge_intent, SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE)
        reserve = _candidate(inputs.reserve_discharge_intent, SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE)
        cycle = _candidate(inputs.cycle_discharge_intent, SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE)
        cheap = _window_active(inputs.cheap_window, inputs.now) and _positive_margin(inputs.cheap_window)

        if inputs.cycle_state is CycleState.RESERVE_DISCHARGING:
            effective_reserve = _effective_reserve(inputs, reserve)
            if not cheap and reserve is not None and inputs.soc_percent > effective_reserve:
                return _result(
                    StrategyAction.RESERVE_DISCHARGE,
                    "continue export to the dynamic reserve",
                    slot=_safe_intent(reserve, effective_reserve),
                    state=CycleState.RESERVE_DISCHARGING,
                )
            return _result(StrategyAction.STOP, "reserve export ended before a new direction", state=CycleState.STOPPING)

        if inputs.cycle_state is CycleState.CHARGING:
            if cheap and charge is not None and inputs.soc_percent < inputs.maximum_soc_percent:
                return _result(StrategyAction.CHEAP_CHARGE, "recharge to full SOC after cycle", slot=charge, state=CycleState.CHARGING)
            return _result(StrategyAction.STOP, "recharge complete or cheap window ended", state=CycleState.STOPPING)

        if inputs.cycle_state is CycleState.DISCHARGING:
            if _cycle_can_continue(inputs):
                window = inputs.cycle_window or inputs.cheap_window
                assert window is not None
                deadline = min(
                    value
                    for value in (
                        inputs.cycle_deadline or cycle.end,
                        cycle.end,
                        window.end,
                    )
                )
                bounded_cycle = replace(cycle, end=deadline, expiry=min(cycle.expiry, deadline))
                return _result(
                    StrategyAction.CYCLE_DISCHARGE,
                    "continue bounded full-SOC cycle",
                    slot=_safe_intent(bounded_cycle, max(inputs.minimum_soc_percent, inputs.reserve_soc_percent)),
                    state=CycleState.DISCHARGING,
                )
            return _result(StrategyAction.STOP, "cycle deadline or reserve reached", state=CycleState.STOPPING)

        if cheap and charge is not None and inputs.soc_percent < inputs.maximum_soc_percent:
            return _result(StrategyAction.CHEAP_CHARGE, "charge during trusted positive-margin window", slot=charge, state=CycleState.CHARGING)

        if _cycle_can_start(inputs):
            assert cycle is not None
            return _result(
                StrategyAction.CYCLE_DISCHARGE,
                "create profitable headroom at full SOC",
                slot=_safe_intent(cycle, max(inputs.minimum_soc_percent, inputs.reserve_soc_percent)),
                state=CycleState.DISCHARGING,
            )

        effective_reserve = _effective_reserve(inputs, reserve)
        if not cheap and reserve is not None and inputs.soc_percent > effective_reserve:
            return _result(
                StrategyAction.RESERVE_DISCHARGE,
                "export immediately to the dynamic reserve",
                slot=_safe_intent(reserve, effective_reserve),
                state=CycleState.RESERVE_DISCHARGING,
            )

        return _result(StrategyAction.IDLE, "no eligible strategy action")
    except (AttributeError, TypeError, ValueError):
        return _result(StrategyAction.FAIL_SAFE, "invalid strategy input")


plan_strategy = select_strategy

__all__ = ["CycleState", "StrategyAction", "StrategyInputs", "StrategyResult", "plan_strategy", "select_strategy"]
