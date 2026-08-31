"""Small real-world behavioural replay harness for the battery controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import yaml

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.model import (
    CycleState,
    SlotDirection,
    SlotOwner,
    StrategyAction,
)
from custom_components.house_battery_control.planner import (
    AdjustedRateInterval,
    CheapClassification,
    ExportRateInterval,
    Plan,
    RateSourceObservation,
    ReserveInputInterval,
    ReservePlanResult,
    TimeInterval,
    build_plan,
)
from custom_components.house_battery_control.solis import (
    SlotKey,
    SolisAdapter,
    SolisChange,
    read_state,
)
from custom_components.house_battery_control.tests.test_solis import (
    fixture as solis_fixture,
    revise,
)

UTC = timezone.utc
IMPORT_SOURCE = "sensor.replay_import_rates_retrieved"
EXPORT_SOURCE = "sensor.replay_export_rates_retrieved"
DISPATCH_SOURCE = "sensor.replay_dispatch_retrieved"


@dataclass(frozen=True, slots=True)
class ReplayOperation:
    entity_id: str
    target: object
    key: SlotKey | None


@dataclass(frozen=True, slots=True)
class ReplayStep:
    at: datetime
    plan: Plan
    operations: tuple[ReplayOperation, ...]


def load_scenario(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: scenario must be a mapping")
    required = {
        "id",
        "source",
        "purpose",
        "inverter_timezone",
        "cheap_window",
        "cycle_duration_minutes",
        "steps",
    }
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"{path.name}: missing {', '.join(sorted(missing))}")
    if not isinstance(raw["steps"], list) or not raw["steps"]:
        raise ValueError(f"{path.name}: steps must be a non-empty list")
    return raw


async def replay_scenario(hass: object, path: Path) -> tuple[ReplayStep, ...]:
    """Replay one scenario through the production planner and Solis adapter."""

    scenario = load_scenario(path)
    label = str(scenario["id"])
    cheap = _mapping(scenario["cheap_window"], f"{label}.cheap_window")
    cheap_start = _timestamp(cheap["start"])
    cheap_end = _timestamp(cheap["end"])
    duration = timedelta(minutes=int(scenario["cycle_duration_minutes"]))
    if cheap_start >= cheap_end or duration <= timedelta(0):
        raise ValueError(f"{label}: cheap window and cycle duration must be positive")

    config = _config()
    parsed, states = solis_fixture()
    if parsed != config.solis:
        raise AssertionError(f"{label}: replay Solis fixture does not match deployed config")
    adapter = SolisAdapter(states, parsed, timezone=ZoneInfo(str(scenario["inverter_timezone"])))
    imports, exports = _rates(cheap_start, cheap_end)
    horizon_end = imports[-1].end
    reserve_interval = ReserveInputInterval(
        TimeInterval(cheap_start, horizon_end),
        Decimal("0.2"),
        Decimal("0"),
        CheapClassification.STANDARD_CHEAP,
    )

    for entity_id in (
        config.tariff.import_rates_entity_id,
        config.tariff.export_rates_entity_id,
    ):
        hass.states.async_set(entity_id, "available", {"rates": ("replay",)})
    hass.states.async_set(
        config.cycle_discharge_duration_entity_id,
        str(scenario["cycle_duration_minutes"]),
    )

    cycle_state = CycleState.IDLE
    cycle_deadline: datetime | None = None
    cycle_gate: datetime | None = None
    previous_plan: Plan | None = None
    results: list[ReplayStep] = []

    for index, raw_step in enumerate(scenario["steps"]):
        step = _mapping(raw_step, f"{label}.steps[{index}]")
        at = _timestamp(step["at"])
        _update_telemetry(states, parsed, step, at)
        observed = read_state(states, parsed, now=at)
        with (
            patch(
                "custom_components.house_battery_control.planner.parse_fused_import_rates",
                return_value=imports,
            ),
            patch(
                "custom_components.house_battery_control.planner.parse_fused_export_rates",
                return_value=exports,
            ),
            patch(
                "custom_components.house_battery_control.planner._rate_source",
                side_effect=(
                    RateSourceObservation(cheap_start, IMPORT_SOURCE),
                    RateSourceObservation(cheap_start, EXPORT_SOURCE),
                ),
            ),
            patch(
                "custom_components.house_battery_control.planner._dispatch_source",
                return_value=None,
            ),
            patch(
                "custom_components.house_battery_control.planner._forecast_intervals",
                AsyncMock(return_value=(reserve_interval,)),
            ),
            patch(
                "custom_components.house_battery_control.planner.plan_reserve",
                return_value=ReservePlanResult(Decimal("5")),
            ),
        ):
            plan = await build_plan(
                hass,
                config,
                observed,
                now=at,
                cycle_state=cycle_state,
                cycle_deadline=cycle_deadline,
                cycle_observation_gate=cycle_gate,
            )

        context = f"{label} step {index} at {at.isoformat()}"
        _assert_plan(context, plan, step, cheap_start, cheap_end, duration, previous_plan)
        operations = _reconcile(adapter, states, plan, at)
        _assert_operations(context, operations, step, parsed)
        results.append(ReplayStep(at, plan, operations))

        cycle_state = plan.next_cycle_state
        cycle_deadline = plan.cycle_deadline
        cycle_gate = plan.cycle_observation_gate
        previous_plan = plan

    return tuple(results)


def _config() -> object:
    source = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    return integration_config.from_mapping(source)


def _rates(
    cheap_start: datetime,
    cheap_end: datetime,
) -> tuple[tuple[AdjustedRateInterval, ...], tuple[ExportRateInterval, ...]]:
    horizon_end = cheap_end + timedelta(minutes=30)
    common = {
        "source": "replay",
        "tariff": "REPLAY",
        "source_day": "current",
        "source_event": "real-world-replay",
        "source_revision_at": cheap_start,
        "retrieval_source_entity_id": IMPORT_SOURCE,
        "dispatch_source_entity_id": DISPATCH_SOURCE,
        "event_minimum": Decimal("0.07"),
        "event_unique_price_count": 2,
        "is_intelligent_adjusted": False,
        "is_capped": False,
    }
    imports = (
        AdjustedRateInterval(
            start=cheap_start,
            end=cheap_end,
            import_price=Decimal("0.07"),
            classification=CheapClassification.STANDARD_CHEAP,
            **common,
        ),
        AdjustedRateInterval(
            start=cheap_end,
            end=horizon_end,
            import_price=Decimal("0.30"),
            classification=CheapClassification.NOT_CHEAP,
            **common,
        ),
    )
    exports = (
        ExportRateInterval(
            start=cheap_start,
            end=horizon_end,
            export_price=Decimal("0.15"),
            source="replay",
            tariff="REPLAY-EXPORT",
            retrieved_at=cheap_start,
            source_day="current",
            source_event="real-world-replay",
            source_revision_at=cheap_start,
            retrieval_source_entity_id=EXPORT_SOURCE,
            is_capped=False,
        ),
    )
    return imports, exports


def _update_telemetry(
    states: dict[str, object],
    config: object,
    step: dict[str, object],
    at: datetime,
) -> None:
    telemetry = config.telemetry
    for entity_id in (
        telemetry.state_of_charge_entity_id,
        telemetry.battery_power_entity_id,
        telemetry.battery_voltage_entity_id,
        telemetry.device_timestamp_entity_id,
    ):
        state = states[entity_id]
        state["last_updated"] = at  # type: ignore[index]
    states[telemetry.state_of_charge_entity_id]["state"] = str(step["soc_percent"])  # type: ignore[index]
    device_timestamp = _timestamp(step.get("device_timestamp", at))
    states[telemetry.device_timestamp_entity_id]["state"] = str(device_timestamp.timestamp())  # type: ignore[index]
    inverter = config.persistent.inverter_time_entity_id
    states[inverter]["state"] = at.isoformat()  # type: ignore[index]
    states[inverter]["last_updated"] = at  # type: ignore[index]


def _reconcile(
    adapter: SolisAdapter,
    states: dict[str, object],
    plan: Plan,
    at: datetime,
) -> tuple[ReplayOperation, ...]:
    if plan.issue is not None or plan.reserve_soc_percent is None:
        return ()
    operations: list[ReplayOperation] = []
    observed = read_state(states, adapter.config, now=at)
    for key in adapter.conflicting_enabled_keys(observed, plan.intent):
        entity_id = adapter.config.direction(key).enable_entity_id
        _set_state(states, entity_id, "off", at)
        operations.append(ReplayOperation(entity_id, False, key))

    for _ in range(50):
        observed = read_state(states, adapter.config, now=at)
        change = adapter.next_start_change(
            observed,
            plan.intent,
            battery_reserve_soc_percent=Decimal("10"),
            peak_shaving=plan.action is StrategyAction.RESERVE_FOLLOW,
        )
        if change is None:
            assert adapter.intent_matches(
                observed,
                plan.intent,
                battery_reserve_soc_percent=Decimal("10"),
                peak_shaving=plan.action is StrategyAction.RESERVE_FOLLOW,
            )
            return tuple(operations)
        revise(states, change)
        operations.append(ReplayOperation(change.entity_id, change.target, _key_for_change(adapter, change)))
    raise AssertionError("Solis replay reconciliation did not converge")


def _assert_plan(
    context: str,
    plan: Plan,
    step: dict[str, object],
    cheap_start: datetime,
    cheap_end: datetime,
    duration: timedelta,
    previous: Plan | None,
) -> None:
    assert plan.issue is None, f"{context}: {plan.issue}"
    expected = _mapping(step["expect"], f"{context}.expect")
    assert plan.action is StrategyAction(str(expected["action"])), context
    assert plan.next_cycle_state is CycleState(str(expected["cycle_state"])), context
    assert plan.cycle_deadline == _optional_timestamp(expected.get("cycle_deadline")), context

    expected_segments = expected.get("segments", [])
    assert isinstance(expected_segments, list), f"{context}: segments must be a list"
    actual_segments = () if plan.intent is None else plan.intent.segments
    assert len(actual_segments) == len(expected_segments), context
    for actual, raw_expected in zip(actual_segments, expected_segments, strict=True):
        item = _mapping(raw_expected, f"{context}.segment")
        assert actual.owner is SlotOwner(str(item["owner"])), context
        assert actual.direction is SlotDirection(str(item["direction"])), context
        assert actual.start == _timestamp(item["start"]), context
        assert actual.end == _timestamp(item["end"]), context
        assert actual.start >= cheap_start and actual.end <= cheap_end, context
        assert actual.end - actual.start == duration, context
        expected_target = (
            Decimal("100")
            if actual.direction is SlotDirection.CHARGE
            else plan.control_reserve_soc_percent
        )
        assert actual.target_soc == expected_target, context
    for left, right in zip(actual_segments, actual_segments[1:]):
        assert left.end == right.start, f"{context}: schedule is not adjacent"
        assert left.end <= right.start, f"{context}: schedule overlaps"
    if actual_segments and actual_segments[-1].direction is SlotDirection.DISCHARGE:
        assert actual_segments[-1].end + duration <= cheap_end, (
            f"{context}: trailing discharge has no cheap authority for recharge"
        )

    retained = expected.get("retained_direction")
    if retained is not None:
        assert previous is not None and previous.intent is not None and plan.intent is not None, context
        direction = SlotDirection(str(retained))
        old = next(segment for segment in previous.intent.segments if segment.direction is direction)
        new = next(segment for segment in plan.intent.segments if segment.direction is direction)
        assert new == old, f"{context}: {direction.value} phase was rewritten"


def _assert_operations(
    context: str,
    operations: tuple[ReplayOperation, ...],
    step: dict[str, object],
    config: object,
) -> None:
    expected = _mapping(step["expect"], f"{context}.expect")
    if expected.get("no_slot_writes") is True:
        assert not [operation for operation in operations if operation.key is not None], context
    retained = expected.get("retained_direction")
    if retained is not None:
        direction = SlotDirection(str(retained))
        assert not [
            operation
            for operation in operations
            if operation.key is not None and operation.key.direction is direction
        ], f"{context}: retained {direction.value} direction was written"
    enable_order = expected.get("enable_order")
    if enable_order is not None:
        assert isinstance(enable_order, list), f"{context}: enable_order must be a list"
        actual = [
            operation.key.direction.value
            for operation in operations
            if operation.key is not None
            and operation.target is True
            and operation.entity_id == config.direction(operation.key).enable_entity_id
        ]
        assert actual == [str(value) for value in enable_order], context
    stopped = expected.get("stopped_directions")
    if stopped is not None:
        assert isinstance(stopped, list), f"{context}: stopped_directions must be a list"
        actual = [
            operation.key.direction.value
            for operation in operations
            if operation.key is not None and operation.target is False
        ]
        assert actual == [str(value) for value in stopped], context
    native_times = expected.get("native_times")
    if native_times is not None:
        values = _mapping(native_times, f"{context}.native_times")
        actual = {
            operation.key.direction.value: operation.target
            for operation in operations
            if operation.key is not None
            and operation.entity_id == config.direction(operation.key).time_entity_id
        }
        assert actual == {str(direction): value for direction, value in values.items()}, context


def _key_for_change(adapter: SolisAdapter, change: SolisChange) -> SlotKey | None:
    for slot in adapter.config.slots:
        for slot_direction, direction in (
            (SlotDirection.CHARGE, slot.charge),
            (SlotDirection.DISCHARGE, slot.discharge),
        ):
            if change.entity_id in {
                direction.enable_entity_id,
                direction.time_entity_id,
                direction.current_entity_id,
                direction.target_soc_entity_id,
            }:
                return SlotKey(slot.physical_slot, slot_direction)
    return None


def _set_state(states: dict[str, object], entity_id: str, value: str, at: datetime) -> None:
    state = states[entity_id]
    state["state"] = value  # type: ignore[index]
    state["last_updated"] = at  # type: ignore[index]
    state["context_id"] = f"replay-{at.timestamp()}"  # type: ignore[index]


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"invalid replay timestamp: {value!r}")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"replay timestamp must be timezone-aware: {value!r}")
    return result.astimezone(UTC)


def _optional_timestamp(value: object) -> datetime | None:
    return None if value is None else _timestamp(value)


__all__ = ["ReplayOperation", "ReplayStep", "load_scenario", "replay_scenario"]
