"""Behavior tests for the narrow commissioned Solis boundary."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.model import (
    ControllerHealth, LogicalIntent, SlotDirection, SlotIntent, SlotOwner, StorageMode,
)
from custom_components.house_battery_control.solis import (
    MAXIMUM_TELEMETRY_AGE,
    BatteryPowerSign,
    SlotKey,
    SolisAdapter,
    SolisChange,
    WriteOutcome,
    config_from_mapping,
    read_state,
    split_intent,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
LONDON = ZoneInfo("Europe/London")


def deployed() -> dict[str, object]:
    value = yaml.safe_load((Path(__file__).parents[3] / "house_battery_control.yaml").read_text())
    assert isinstance(value, dict)
    return value


def state(value: object, *, unit: str | None = None, updated: datetime = NOW,
          context: str = "initial") -> dict[str, object]:
    attributes: dict[str, object] = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return {
        "state": str(value), "attributes": attributes, "last_updated": updated,
        "context_id": context,
    }


def capability(value: str, unit: str, *, minimum: str = "0", maximum: str = "100",
               step: str = "1") -> dict[str, object]:
    result = state(value, unit=unit)
    result["attributes"] = {
        "min": minimum, "max": maximum, "step": step, "unit_of_measurement": unit,
    }
    return result


def fixture():
    parsed = integration_config.from_mapping(deployed()).solis
    states: dict[str, object] = {}
    telemetry = parsed.telemetry
    states[telemetry.state_of_charge_entity_id] = state("55", unit="%")
    states[telemetry.battery_power_entity_id] = state("-288", unit="W")
    states[telemetry.battery_voltage_entity_id] = state("51.2", unit="V")
    states[telemetry.device_timestamp_entity_id] = state(str(NOW.timestamp()))
    persistent = parsed.persistent
    mode = state("Feed-In Priority")
    mode["attributes"] = {"options": ["Self-Use", "Feed-In Priority", "Off-Grid"]}
    states[persistent.storage_mode_entity_id] = mode
    states[persistent.allow_grid_charging_entity_id] = state("on")
    states[persistent.grid_peak_shaving_entity_id] = state("on")
    states[persistent.inverter_time_entity_id] = state(NOW.isoformat())
    protection = parsed.protection
    states[protection.battery_reserve_entity_id] = state("on")
    states[protection.battery_reserve_soc_entity_id] = capability("10", "%")
    caps = parsed.capability
    states[caps.battery_max_charge_current_entity_id] = capability("100", "A")
    states[caps.battery_max_discharge_current_entity_id] = capability("100", "A")
    for slot in parsed.slots:
        for direction in (slot.charge, slot.discharge):
            states[direction.enable_entity_id] = state("off")
            states[direction.time_entity_id] = state("00:00-00:00")
            states[direction.current_entity_id] = capability("100", "A")
            states[direction.target_soc_entity_id] = capability("50", "%")
    return parsed, states


def intent(
    owner: SlotOwner = SlotOwner.CHEAP_CHARGING,
    direction: SlotDirection = SlotDirection.CHARGE,
    *,
    start: datetime = datetime(2026, 8, 22, 21, tzinfo=UTC),
    end: datetime = datetime(2026, 8, 22, 22, tzinfo=UTC),
    current: str = "100",
    target: str = "100",
) -> LogicalIntent:
    return LogicalIntent((SlotIntent(
        owner, direction, start, end, Decimal(current), Decimal(target), end,
    ),))


def revise(states: dict[str, object], change: SolisChange) -> None:
    item = states[change.entity_id]
    assert isinstance(item, dict)
    target = "on" if change.target is True else "off" if change.target is False else str(change.target)
    item["state"] = target
    item["last_updated"] = change.precondition.last_updated + timedelta(seconds=1)
    item["context_id"] = "changed"


def advance(adapter: SolisAdapter, states: dict[str, object], desired: LogicalIntent,
            *, reserve: Decimal = Decimal("10")) -> list[SolisChange]:
    changes: list[SolisChange] = []
    for _ in range(20):
        observed = read_state(states, adapter.config, now=NOW)
        change = adapter.next_start_change(observed, desired, reserve_soc_percent=reserve, peak_shaving=False)
        if change is None:
            return changes
        changes.append(change)
        revise(states, change)
    raise AssertionError("Solis reconciliation did not converge")


def test_compact_config_generates_exact_six_slot_map_and_allocations() -> None:
    parsed, _ = fixture()
    assert parsed.telemetry.battery_power_sign is BatteryPowerSign.POSITIVE_MEANS_CHARGING
    assert parsed.midnight_end == "23:59"
    assert len(parsed.slots) == 6
    assert parsed.allocation(SlotOwner.CHEAP_CHARGING) == (
        SlotKey(1, SlotDirection.CHARGE), SlotKey(2, SlotDirection.CHARGE),
    )
    assert parsed.allocation(SlotOwner.FULL_SOC_CYCLING) == (
        SlotKey(1, SlotDirection.DISCHARGE), SlotKey(3, SlotDirection.DISCHARGE),
    )
    assert parsed.allocation(SlotOwner.RESERVE_EXPORT) == (
        SlotKey(2, SlotDirection.DISCHARGE), SlotKey(4, SlotDirection.DISCHARGE),
    )
    for physical in range(1, 7):
        for direction in (SlotDirection.CHARGE, SlotDirection.DISCHARGE):
            item = getattr(parsed.slots[physical - 1], direction.value)
            base = f"garage_inverter_control_slot{physical}_{direction.value}"
            assert (item.enable_entity_id, item.time_entity_id, item.current_entity_id, item.target_soc_entity_id) == (
                f"switch.{base}", f"text.{base}_time", f"number.{base}_current", f"number.{base}_soc",
            )


def test_config_rejects_unknown_mapping_duplicate_entities_and_midnight() -> None:
    source = deployed()["solis"]
    assert isinstance(source, dict)
    missing = deepcopy(source)
    missing["slot_allocations"].pop("reserve_export")
    with pytest.raises(ValueError, match="exactly the three owners"):
        config_from_mapping(missing)
    duplicate = deepcopy(source)
    duplicate["capability"]["battery_max_discharge_current_entity_id"] = duplicate["capability"]["battery_max_charge_current_entity_id"]
    with pytest.raises(ValueError, match="globally unique"):
        config_from_mapping(duplicate)
    invalid = deepcopy(source)
    invalid["midnight_end"] = "00:00"
    with pytest.raises(ValueError, match="24:00 or 23:59"):
        config_from_mapping(invalid)
    missing_peak = deepcopy(source)
    missing_peak["persistent"].pop("grid_peak_shaving_entity_id")
    with pytest.raises(ValueError, match="missing"):
        config_from_mapping(missing_peak)


def test_read_state_normalizes_sign_freshness_clock_capabilities_and_all_enables() -> None:
    parsed, states = fixture()
    sampled = NOW - timedelta(minutes=15)
    states[parsed.persistent.inverter_time_entity_id] = state(
        (sampled + timedelta(seconds=30)).isoformat(), updated=sampled,
    )
    observed = read_state(states, parsed, now=NOW)
    assert observed.health is ControllerHealth.HEALTHY
    assert observed.telemetry is not None
    assert observed.telemetry.battery_power_kw == Decimal("-0.288")
    assert observed.telemetry.device_timestamp == NOW
    assert observed.persistent is not None
    assert observed.persistent.inverter_time == NOW + timedelta(seconds=30)
    assert observed.capabilities is not None
    assert observed.capabilities.maximum_charge_current.maximum == Decimal("100")
    assert len(tuple(direction for slot in observed.slots for direction in (slot.charge, slot.discharge))) == 12
    assert all(direction.enabled is False for slot in observed.slots for direction in (slot.charge, slot.discharge))


@pytest.mark.parametrize(
    ("mutate", "code"),
    (
        (lambda config, states: states.__setitem__(config.telemetry.device_timestamp_entity_id, state(str((NOW - MAXIMUM_TELEMETRY_AGE - timedelta(seconds=1)).timestamp()))), "device_timestamp_stale"),
        (lambda config, states: states.__setitem__(config.telemetry.battery_power_entity_id, state("1", unit="MW")), "battery_power_unit_unknown"),
        (lambda config, states: states[config.slots[0].charge.enable_entity_id].__setitem__("state", "unavailable"), "state_revision_invalid"),
        (lambda config, states: states[config.slots[0].charge.current_entity_id]["attributes"].__setitem__("step", "0"), "capability_invalid"),
    ),
)
def test_read_state_reports_stale_malformed_or_unavailable_inputs(mutate, code) -> None:
    parsed, states = fixture()
    mutate(parsed, states)
    observed = read_state(states, parsed, now=NOW)
    assert observed.health is ControllerHealth.DEGRADED
    assert any(issue.code == code for issue in observed.issues)


def test_unknown_peak_shaving_does_not_hide_mode_but_blocks_start() -> None:
    parsed, states = fixture()
    states[parsed.persistent.grid_peak_shaving_entity_id]["state"] = "unknown"
    observed = read_state(states, parsed, now=NOW)
    assert observed.persistent is not None
    assert observed.persistent.storage_mode == StorageMode.FEED_IN_PRIORITY.value
    assert observed.grid_peak_shaving is None
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    assert adapter.next_start_change(observed, intent(), reserve_soc_percent=Decimal("10"), peak_shaving=False) is None


@pytest.mark.parametrize(
    ("midnight_end", "expected_first"),
    (("23:59", "22:00-23:59"), ("24:00", "22:00-24:00")),
)
def test_split_midnight_encodes_configured_boundary(
    midnight_end: str, expected_first: str,
) -> None:
    parsed, states = fixture()
    parsed = replace(parsed, midnight_end=midnight_end)
    for owner, direction in (
        (SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE),
        (SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE),
    ):
        desired = intent(
            owner, direction,
            start=datetime(2026, 12, 1, 22, tzinfo=UTC),
            end=datetime(2026, 12, 2, 1, tzinfo=UTC),
            target="100" if direction is SlotDirection.CHARGE else "20",
        )
        split = split_intent(desired, timezone=LONDON, midnight_end=midnight_end)
        assert len(split.segments) == 2
        assert split.segments[0].end == split.segments[1].start
        local_states = deepcopy(states)
        adapter = SolisAdapter(local_states, parsed, timezone=LONDON)
        changes = advance(adapter, local_states, desired, reserve=Decimal("10"))
        times = [change.target for change in changes if change.entity_id.startswith("text.")]
        assert times == [expected_first, "00:00-01:00"]
        final = read_state(local_states, parsed, now=NOW)
        assert adapter.intent_matches(final, desired, reserve_soc_percent=Decimal("10"), peak_shaving=False)


def test_2359_representation_keeps_explicit_native_one_minute_gap() -> None:
    parsed, states = fixture()
    parsed = replace(parsed, midnight_end="23:59")
    desired = intent(
        start=datetime(2026, 12, 1, 22, tzinfo=UTC),
        end=datetime(2026, 12, 2, 1, tzinfo=UTC),
    )
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    changes = advance(adapter, states, desired)
    times = [change.target for change in changes if change.entity_id.startswith("text.")]
    assert times == ["22:00-23:59", "00:00-01:00"]
    observed = read_state(states, parsed, now=NOW)
    first, second = parsed.allocation(SlotOwner.CHEAP_CHARGING)
    assert observed.direction(first).schedule.ranges() == ((22 * 60, 23 * 60 + 59),)  # type: ignore[union-attr]
    assert observed.direction(second).schedule.ranges() == ((0, 60),)  # type: ignore[union-attr]


def test_ordered_start_changes_enable_last_and_quantize_dynamic_reserve() -> None:
    parsed, states = fixture()
    states[parsed.persistent.storage_mode_entity_id]["state"] = "Self-Use"
    states[parsed.persistent.allow_grid_charging_entity_id]["state"] = "off"
    states[parsed.protection.battery_reserve_soc_entity_id] = capability("10", "%", step="1")
    states[parsed.protection.battery_reserve_entity_id]["state"] = "off"
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    changes = advance(adapter, states, intent(), reserve=Decimal("10.1"))
    assert [change.entity_id for change in changes[:4]] == [
        parsed.persistent.storage_mode_entity_id,
        parsed.persistent.allow_grid_charging_entity_id,
        parsed.protection.battery_reserve_soc_entity_id,
        parsed.protection.battery_reserve_entity_id,
    ]
    assert changes[2].target == Decimal("11")
    slot = parsed.slots[0].charge
    slot_changes = [change for change in changes if change.entity_id in {
        slot.time_entity_id, slot.current_entity_id, slot.target_soc_entity_id, slot.enable_entity_id,
    }]
    assert [change.entity_id for change in slot_changes] == [
        slot.time_entity_id, slot.target_soc_entity_id, slot.enable_entity_id,
    ]
    assert slot_changes[-1].target is True


def test_forced_entry_reconciles_policy_before_peak_off() -> None:
    parsed, states = fixture()
    desired = intent()
    adapter = SolisAdapter(states, parsed, timezone=LONDON)

    # Establish a fully armed forced slot, then reproduce the handover state:
    # Peak Shaving is still on while the persistent policy has drifted to
    # Self-Use.  The first correction must be the policy, never Peak off.
    advance(adapter, states, desired)
    states[parsed.persistent.grid_peak_shaving_entity_id]["state"] = "on"
    states[parsed.persistent.storage_mode_entity_id]["state"] = "Self-Use"
    observed = read_state(states, parsed, now=NOW)
    first = adapter.next_start_change(
        observed, desired, reserve_soc_percent=Decimal("10"), peak_shaving=False
    )
    assert first is not None
    assert first.entity_id == parsed.persistent.storage_mode_entity_id

    changes = advance(adapter, states, desired)
    mode_index = next(
        index for index, change in enumerate(changes)
        if change.entity_id == parsed.persistent.storage_mode_entity_id
    )
    peak_off_index = next(
        index for index, change in enumerate(changes)
        if change.entity_id == parsed.persistent.grid_peak_shaving_entity_id
        and change.target is False
    )
    assert mode_index < peak_off_index


def test_forced_entry_full_sequence_proves_peak_then_arms_slot_then_releases() -> None:
    parsed, states = fixture()
    peak_entity = parsed.persistent.grid_peak_shaving_entity_id
    states[peak_entity]["state"] = "off"
    adapter = SolisAdapter(states, parsed, timezone=LONDON)

    changes = advance(adapter, states, intent())
    slot_entity = parsed.direction(SlotKey(1, SlotDirection.CHARGE)).enable_entity_id
    peak_on_index = next(
        index for index, change in enumerate(changes)
        if change.entity_id == peak_entity and change.target is True
    )
    slot_enable_index = next(
        index for index, change in enumerate(changes)
        if change.entity_id == slot_entity and change.target is True
    )
    peak_off_index = next(
        index for index, change in enumerate(changes)
        if change.entity_id == peak_entity and change.target is False
    )
    assert peak_on_index == 0
    assert peak_on_index < slot_enable_index < peak_off_index


def test_adapter_does_not_reround_common_quantized_full_cycle_target() -> None:
    parsed, states = fixture()
    cycle_key = parsed.allocation(SlotOwner.FULL_SOC_CYCLING)[0]
    target_entity = parsed.direction(cycle_key).target_soc_entity_id
    states[target_entity]["attributes"]["step"] = "2"
    adapter = SolisAdapter(states, parsed, timezone=LONDON)

    changes = advance(
        adapter,
        states,
        intent(SlotOwner.FULL_SOC_CYCLING, SlotDirection.DISCHARGE, target="18"),
        reserve=Decimal("18"),
    )

    target_changes = [change for change in changes if change.entity_id == target_entity]
    assert target_changes
    assert target_changes[0].target == Decimal("18")


@pytest.mark.asyncio
async def test_forced_entry_peak_off_failure_retains_peak_and_slot() -> None:
    parsed, states = fixture()
    desired = intent()
    setup_adapter = SolisAdapter(states, parsed, timezone=LONDON)
    advance(setup_adapter, states, desired)
    peak_entity = parsed.persistent.grid_peak_shaving_entity_id
    slot_entity = parsed.direction(SlotKey(1, SlotDirection.CHARGE)).enable_entity_id
    states[peak_entity]["state"] = "on"

    fake = FakeHA(states, "no_readback")
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)
    observed = read_state(states, parsed, now=NOW)
    change = adapter.next_start_change(
        observed, desired, reserve_soc_percent=Decimal("10"), peak_shaving=False
    )
    assert change is not None
    assert change.entity_id == peak_entity

    result = await adapter.apply(change, deadline=asyncio.get_running_loop().time() + 1)
    assert result.outcome is WriteOutcome.READBACK_TIMEOUT
    assert states[peak_entity]["state"] == "on"
    assert states[slot_entity]["state"] == "on"


def test_active_half_open_adjacency_is_accepted_but_conflict_or_unknown_blocks_start() -> None:
    parsed, states = fixture()
    desired = intent(
        start=datetime(2026, 12, 1, 22, tzinfo=UTC),
        end=datetime(2026, 12, 2, 1, tzinfo=UTC),
    )
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    split = split_intent(desired, timezone=LONDON, midnight_end=parsed.midnight_end)
    first = parsed.slots[0].charge
    states[first.time_entity_id]["state"] = "22:00-23:59"
    states[first.target_soc_entity_id]["state"] = "100"
    states[first.enable_entity_id]["state"] = "on"
    observed = read_state(states, parsed, now=NOW)
    change = adapter.next_start_change(observed, desired, reserve_soc_percent=Decimal("10"), peak_shaving=False)
    assert change is not None and change.entity_id == parsed.slots[1].charge.time_entity_id
    assert split.segments[0].end == split.segments[1].start

    conflict = parsed.slots[0].discharge
    states[conflict.time_entity_id]["state"] = "22:30-23:30"
    states[conflict.enable_entity_id]["state"] = "on"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(observed, desired, reserve_soc_percent=Decimal("10"), peak_shaving=False) is None
    states[conflict.enable_entity_id]["state"] = "unavailable"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(observed, desired, reserve_soc_percent=Decimal("10"), peak_shaving=False) is None


def test_conflicting_or_unknown_enable_blocks_persistent_start_preparation() -> None:
    parsed, states = fixture()
    states[parsed.persistent.storage_mode_entity_id]["state"] = "Self-Use"
    conflicting = parsed.slots[0].discharge
    states[conflicting.enable_entity_id]["state"] = "on"
    adapter = SolisAdapter(states, parsed, timezone=LONDON)

    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(
        observed, intent(), reserve_soc_percent=Decimal("10"), peak_shaving=False
    ) is None

    states[conflicting.enable_entity_id]["state"] = "unavailable"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(
        observed, intent(), reserve_soc_percent=Decimal("10"), peak_shaving=False
    ) is None


def test_active_or_unknown_enable_blocks_idle_persistent_reconciliation() -> None:
    parsed, states = fixture()
    states[parsed.persistent.storage_mode_entity_id]["state"] = "Self-Use"
    active = parsed.slots[0].charge
    states[active.enable_entity_id]["state"] = "on"
    adapter = SolisAdapter(states, parsed, timezone=LONDON)

    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(
        observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=True
    ) is None

    states[active.enable_entity_id]["state"] = "unavailable"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(
        observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=True
    ) is None


@pytest.mark.parametrize(
    ("initial_peak", "desired_peak"),
    (("off", True), ("on", False)),
)
def test_idle_reconciles_peak_shaving_and_matches_after_readback(
    initial_peak: str, desired_peak: bool,
) -> None:
    parsed, states = fixture()
    peak_entity = parsed.persistent.grid_peak_shaving_entity_id
    states[peak_entity]["state"] = initial_peak
    adapter = SolisAdapter(states, parsed, timezone=LONDON)

    observed = read_state(states, parsed, now=NOW)
    assert not adapter.intent_matches(
        observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=desired_peak
    )
    change = adapter.next_start_change(
        observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=desired_peak
    )
    assert change is not None
    assert change.entity_id == peak_entity
    assert change.target is desired_peak

    revise(states, change)
    readback = read_state(states, parsed, now=NOW)
    assert adapter.intent_matches(
        readback, None, reserve_soc_percent=Decimal("10"), peak_shaving=desired_peak
    )


def test_disabled_stored_overlap_is_ignored_and_idle_observes_without_cleaning_slots() -> None:
    parsed, states = fixture()
    disabled = parsed.slots[0].discharge
    states[disabled.time_entity_id]["state"] = "21:30-22:30"
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(observed, intent(), reserve_soc_percent=Decimal("10"), peak_shaving=False) is not None
    states[disabled.enable_entity_id]["state"] = "on"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_start_change(observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=True) is None
    assert not adapter.intent_matches(observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=True)

    states[disabled.enable_entity_id]["state"] = "unavailable"
    observed = read_state(states, parsed, now=NOW)
    assert not adapter.intent_matches(observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=True)

    states[disabled.enable_entity_id]["state"] = "off"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.intent_matches(observed, None, reserve_soc_percent=Decimal("10"), peak_shaving=True)


def test_conflict_projection_is_exact_read_only_and_omits_unknown() -> None:
    parsed, states = fixture()
    desired = intent(
        start=datetime(2026, 12, 1, 22, tzinfo=UTC),
        end=datetime(2026, 12, 2, 1, tzinfo=UTC),
    )
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    first, second = parsed.allocation(SlotOwner.CHEAP_CHARGING)
    for key, time_text in ((first, "22:00-23:59"), (second, "00:00-01:00")):
        direction = parsed.direction(key)
        states[direction.time_entity_id]["state"] = time_text
        states[direction.target_soc_entity_id]["state"] = "100"
        states[direction.enable_entity_id]["state"] = "on"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.conflicting_enabled_keys(observed, desired) == ()

    mismatch = parsed.direction(first)
    states[mismatch.time_entity_id]["state"] = "21:59-23:59"
    extra = parsed.slots[0].discharge
    states[extra.enable_entity_id]["state"] = "on"
    unknown = parsed.slots[2].charge
    states[unknown.enable_entity_id]["state"] = "unavailable"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.conflicting_enabled_keys(observed, desired) == (
        first,
        SlotKey(1, SlotDirection.DISCHARGE),
    )
    assert adapter.conflicting_enabled_keys(observed, None) == (
        first,
        SlotKey(1, SlotDirection.DISCHARGE),
        second,
    )


def test_active_reserve_discharge_survives_minute_start_shift_only_while_exact() -> None:
    parsed, states = fixture()
    active = intent(
        SlotOwner.RESERVE_EXPORT,
        SlotDirection.DISCHARGE,
        start=NOW,
        end=NOW + timedelta(hours=2),
        current="100",
        target="20",
    )
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    advance(adapter, states, active)
    key = parsed.allocation(SlotOwner.RESERVE_EXPORT)[0]

    next_minute = NOW + timedelta(minutes=1)
    continued = intent(
        SlotOwner.RESERVE_EXPORT,
        SlotDirection.DISCHARGE,
        start=next_minute,
        end=active.end,
        current="100",
        target="20",
    )
    observed = read_state(states, parsed, now=next_minute)
    assert adapter.conflicting_enabled_keys(observed, continued) == ()
    assert adapter.intent_matches(
        observed, continued, reserve_soc_percent=Decimal("10"), peak_shaving=False
    )

    before_desired_start = read_state(states, parsed, now=NOW)
    assert adapter.conflicting_enabled_keys(
        before_desired_start, continued
    ) == (key,)
    for outside in (active.end, active.end + timedelta(minutes=1)):
        outside_observation = read_state(states, parsed, now=outside)
        assert adapter.conflicting_enabled_keys(
            outside_observation, continued
        ) == (key,)

    changed_end = intent(
        SlotOwner.RESERVE_EXPORT,
        SlotDirection.DISCHARGE,
        start=next_minute,
        end=active.end + timedelta(minutes=1),
        current="100",
        target="20",
    )
    assert adapter.conflicting_enabled_keys(observed, changed_end) == (key,)
    changed_target = intent(
        SlotOwner.RESERVE_EXPORT,
        SlotDirection.DISCHARGE,
        start=next_minute,
        end=active.end,
        current="100",
        target="21",
    )
    assert adapter.conflicting_enabled_keys(observed, changed_target) == (key,)
    changed_current = intent(
        SlotOwner.RESERVE_EXPORT,
        SlotDirection.DISCHARGE,
        start=next_minute,
        end=active.end,
        current="99",
        target="20",
    )
    assert adapter.conflicting_enabled_keys(observed, changed_current) == (key,)


def test_active_cheap_charge_survives_three_minute_start_shifts_only_while_exact() -> None:
    parsed, states = fixture()
    active = intent(
        SlotOwner.CHEAP_CHARGING,
        SlotDirection.CHARGE,
        start=NOW,
        end=NOW + timedelta(hours=2),
        current="100",
        target="100",
    )
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    advance(adapter, states, active)
    key = parsed.allocation(SlotOwner.CHEAP_CHARGING)[0]

    for minutes in (1, 2, 3):
        shifted = intent(
            SlotOwner.CHEAP_CHARGING,
            SlotDirection.CHARGE,
            start=NOW + timedelta(minutes=minutes),
            end=active.end,
            current="100",
            target="100",
        )
        observed = read_state(states, parsed, now=NOW + timedelta(minutes=minutes))
        assert adapter.conflicting_enabled_keys(observed, shifted) == ()
        assert adapter.intent_matches(
            observed, shifted, reserve_soc_percent=Decimal("10"), peak_shaving=False
        )

    continued = intent(
        SlotOwner.CHEAP_CHARGING,
        SlotDirection.CHARGE,
        start=NOW + timedelta(minutes=1),
        end=active.end,
        current="100",
        target="100",
    )
    before_desired_start = read_state(states, parsed, now=NOW)
    assert adapter.conflicting_enabled_keys(before_desired_start, continued) == (key,)
    for outside in (active.end, active.end + timedelta(minutes=1)):
        outside_observation = read_state(states, parsed, now=outside)
        assert adapter.conflicting_enabled_keys(outside_observation, continued) == (key,)

    for changed in (
        intent(SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE,
               start=NOW + timedelta(minutes=1), end=active.end + timedelta(minutes=1),
               current="100", target="100"),
        intent(SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE,
               start=NOW + timedelta(minutes=1), end=active.end,
               current="99", target="100"),
        intent(SlotOwner.CHEAP_CHARGING, SlotDirection.CHARGE,
               start=NOW + timedelta(minutes=1), end=active.end,
               current="100", target="99"),
        intent(SlotOwner.CHEAP_CHARGING, SlotDirection.DISCHARGE,
               start=NOW + timedelta(minutes=1), end=active.end,
               current="100", target="100"),
        intent(SlotOwner.RESERVE_EXPORT, SlotDirection.DISCHARGE,
               start=NOW + timedelta(minutes=1), end=active.end,
               current="100", target="20"),
    ):
        observed = read_state(states, parsed, now=NOW + timedelta(minutes=1))
        assert adapter.conflicting_enabled_keys(observed, changed) == (key,)

    remapped = deepcopy(deployed())
    assert isinstance(remapped["solis"], dict)
    assert isinstance(remapped["solis"]["slot_allocations"], dict)
    remapped["solis"]["slot_allocations"]["cheap_charging"] = [2, 3]
    remapped_config = integration_config.from_mapping(remapped).solis
    remapped_adapter = SolisAdapter(states, remapped_config, timezone=LONDON)
    observed = read_state(states, parsed, now=NOW + timedelta(minutes=1))
    assert remapped_adapter.conflicting_enabled_keys(observed, continued) == (key,)


@pytest.mark.parametrize("midnight_end", ("24:00", "23:59"))
def test_active_cheap_charge_never_preserves_past_native_midnight_end(midnight_end: str) -> None:
    parsed, states = fixture()
    parsed = replace(parsed, midnight_end=midnight_end)
    active = intent(
        SlotOwner.CHEAP_CHARGING,
        SlotDirection.CHARGE,
        start=datetime(2026, 8, 22, 21, tzinfo=UTC),
        end=datetime(2026, 8, 22, 23, tzinfo=UTC),
        current="100",
        target="100",
    )
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    advance(adapter, states, active)
    key = parsed.allocation(SlotOwner.CHEAP_CHARGING)[0]
    shifted = intent(
        SlotOwner.CHEAP_CHARGING,
        SlotDirection.CHARGE,
        start=datetime(2026, 8, 22, 21, 1, tzinfo=UTC),
        end=active.end,
        current="100",
        target="100",
    )
    before_native_end = read_state(states, parsed, now=datetime(2026, 8, 22, 22, 58, tzinfo=UTC))
    assert adapter.conflicting_enabled_keys(before_native_end, shifted) == ()
    at_native_end = read_state(states, parsed, now=active.end)
    assert adapter.conflicting_enabled_keys(at_native_end, shifted) == (key,)
    assert adapter.conflicting_enabled_keys(at_native_end, None) == (key,)
    assert not adapter.intent_matches(at_native_end, None, reserve_soc_percent=Decimal("10"), peak_shaving=False)


class FakeHA:
    def __init__(self, states: dict[str, object], behavior: str = "success") -> None:
        self.states = SimpleNamespace(get=states.get)
        self._states = states
        self.services = self
        self.behavior = behavior
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.listeners: dict[str, list] = {}
        self.block = asyncio.Event()

    def async_listen_state_change(self, entity_id, callback):
        self.listeners.setdefault(entity_id, []).append(callback)
        return lambda: self.listeners[entity_id].remove(callback)

    async def async_call(self, domain, service, data, *, blocking=True):
        self.calls.append((domain, service, dict(data)))
        if self.behavior in {
            "success",
            "error",
            "block",
            "ignore_cancel",
            "idempotent_off",
        }:
            entity_id = data["entity_id"]
            target = (
                "on" if service == "turn_on" else "off" if service == "turn_off"
                else data.get("option", data.get("value", data.get("datetime")))
            )
            item = self._states[entity_id]
            if not (
                self.behavior == "idempotent_off"
                and item["state"] == "off"
                and target == "off"
            ):
                item["state"] = str(target)
                item["last_updated"] = item["last_updated"] + timedelta(seconds=1)
                item["context_id"] = "service"
                for callback in tuple(self.listeners.get(entity_id, ())):
                    callback({"state": item["state"], "last_updated": item["last_updated"],
                              "context_id": item["context_id"], "attributes": item.get("attributes", {})})
        if self.behavior == "error":
            raise RuntimeError("boom")
        if self.behavior in {"block", "ignore_cancel"}:
            try:
                await self.block.wait()
            except asyncio.CancelledError:
                if self.behavior != "ignore_cancel":
                    raise
                await self.block.wait()


def first_slot_change(adapter: SolisAdapter, states: dict[str, object]) -> SolisChange:
    observed = read_state(states, adapter.config, now=NOW)
    result = adapter.next_start_change(observed, intent(), reserve_soc_percent=Decimal("10"), peak_shaving=False)
    assert result is not None
    return result


@pytest.mark.asyncio
async def test_apply_success_conflict_timeout_error_and_later_drift() -> None:
    parsed, states = fixture()
    success_ha = FakeHA(states)
    adapter = SolisAdapter(success_ha, parsed, timezone=LONDON)
    change = first_slot_change(adapter, states)
    applied = await adapter.apply(change, deadline=asyncio.get_running_loop().time() + 1)
    assert applied.outcome is WriteOutcome.APPLIED

    stale = await adapter.apply(change, deadline=asyncio.get_running_loop().time() + 1)
    assert stale.outcome is WriteOutcome.CONFLICT
    # A later contradictory refresh is normal drift and produces a new change.
    states[change.entity_id]["state"] = "00:00-00:00"
    states[change.entity_id]["last_updated"] += timedelta(seconds=1)
    assert first_slot_change(adapter, states).entity_id == change.entity_id

    for behavior, expected in (("error", WriteOutcome.SERVICE_ERROR), ("block", WriteOutcome.SERVICE_TIMEOUT)):
        parsed, states = fixture()
        fake = FakeHA(states, behavior)
        adapter = SolisAdapter(fake, parsed, timezone=LONDON)
        change = first_slot_change(adapter, states)
        result = await adapter.apply(change, deadline=asyncio.get_running_loop().time() + 0.01)
        assert result.outcome is expected

    parsed, states = fixture()
    fake = FakeHA(states, "no_readback")
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)
    result = await adapter.apply(
        first_slot_change(adapter, states),
        deadline=asyncio.get_running_loop().time() + 0.01,
    )
    assert result.outcome is WriteOutcome.READBACK_TIMEOUT


@pytest.mark.asyncio
async def test_optimistic_match_requires_successful_blocking_completion_and_cancellation_propagates() -> None:
    parsed, states = fixture()
    fake = FakeHA(states, "block")
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)
    change = first_slot_change(adapter, states)
    task = asyncio.create_task(adapter.apply(change, deadline=asyncio.get_running_loop().time() + 5))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_narrow_stop_and_mode_write_only_the_resolved_entities() -> None:
    parsed, states = fixture()
    enabled = parsed.slots[1].discharge.enable_entity_id
    states[enabled]["state"] = "on"
    fake = FakeHA(states)
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)
    stopped = await adapter.stop(
        SlotKey(2, SlotDirection.DISCHARGE),
        deadline=asyncio.get_running_loop().time() + 1,
    )
    assert stopped.success
    mode = await adapter.set_mode(
        StorageMode.SELF_USE,
        deadline=asyncio.get_running_loop().time() + 1,
    )
    assert mode.success
    assert [call[2]["entity_id"] for call in fake.calls] == [
        enabled, parsed.persistent.storage_mode_entity_id,
    ]
    assert not any("peak" in str(call).lower() or "feed_in_power" in str(call).lower() for call in fake.calls)


@pytest.mark.asyncio
async def test_forced_ambiguous_stop_requires_blocking_call_and_newer_readback() -> None:
    parsed, states = fixture()
    key = SlotKey(2, SlotDirection.DISCHARGE)
    enabled = parsed.direction(key).enable_entity_id
    fake = FakeHA(states)
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)

    already_off = await adapter.stop(
        key,
        deadline=asyncio.get_running_loop().time() + 1,
    )
    assert already_off.outcome is WriteOutcome.NO_CHANGE
    assert fake.calls == []

    forced = await adapter.stop(
        key,
        deadline=asyncio.get_running_loop().time() + 1,
        force=True,
    )
    assert forced.outcome is WriteOutcome.APPLIED
    assert [call[2]["entity_id"] for call in fake.calls] == [enabled]

    parsed, states = fixture()
    unchanged_at = states[enabled]["last_updated"]
    fake = FakeHA(states, "idempotent_off")
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)
    idempotent = await adapter.stop(
        key,
        deadline=asyncio.get_running_loop().time() + 1,
        force=True,
    )
    assert idempotent.outcome is WriteOutcome.APPLIED
    assert states[enabled]["last_updated"] == unchanged_at

    for behavior, outcome in (
        ("error", WriteOutcome.SERVICE_ERROR),
        ("block", WriteOutcome.SERVICE_TIMEOUT),
    ):
        parsed, states = fixture()
        fake = FakeHA(states, behavior)
        adapter = SolisAdapter(fake, parsed, timezone=LONDON)
        result = await adapter.stop(
            key,
            deadline=asyncio.get_running_loop().time() + 0.01,
            force=True,
        )
        assert result.outcome is outcome


@pytest.mark.asyncio
async def test_cancellation_ignoring_service_never_overlaps_forced_stop_retry() -> None:
    parsed, states = fixture()
    key = SlotKey(2, SlotDirection.DISCHARGE)
    fake = FakeHA(states, "ignore_cancel")
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)

    first = await adapter.stop(
        key,
        deadline=asyncio.get_running_loop().time() + 0.01,
        force=True,
    )
    second = await adapter.stop(
        key,
        deadline=asyncio.get_running_loop().time() + 0.01,
        force=True,
    )
    assert first.outcome is WriteOutcome.SERVICE_TIMEOUT
    assert second.outcome is WriteOutcome.SERVICE_TIMEOUT
    assert len(fake.calls) == 1

    fake.block.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    third = await adapter.stop(
        key,
        deadline=asyncio.get_running_loop().time() + 1,
        force=True,
    )
    assert third.outcome is WriteOutcome.APPLIED
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_pinned_retry_can_finish_beyond_old_ten_second_cutoff_without_sleep() -> None:
    parsed, states = fixture()
    key = SlotKey(2, SlotDirection.DISCHARGE)
    fake = FakeHA(states, "block")
    adapter = SolisAdapter(fake, parsed, timezone=LONDON)
    real_wait = asyncio.wait
    observed_timeouts: list[float | None] = []

    async def complete_within_outer_bound(tasks, *, timeout):
        observed_timeouts.append(timeout)
        fake.block.set()
        await asyncio.sleep(0)
        return await real_wait(tasks, timeout=0.1)

    with patch(
        "custom_components.house_battery_control.solis.asyncio.wait",
        new=complete_within_outer_bound,
    ):
        result = await adapter.stop(
            key,
            deadline=asyncio.get_running_loop().time() + 80,
            force=True,
        )

    assert result.outcome is WriteOutcome.APPLIED
    assert observed_timeouts and observed_timeouts[0] is not None
    assert observed_timeouts[0] > 60
    assert len(fake.calls) == 1


def test_housekeeping_targets_only_one_confirmed_off_used_slot() -> None:
    parsed, states = fixture()
    key = SlotKey(2, SlotDirection.DISCHARGE)
    configured = parsed.direction(key)
    states[configured.time_entity_id]["state"] = "09:00-10:00"
    states[configured.current_entity_id]["state"] = "25"
    adapter = SolisAdapter(states, parsed, timezone=LONDON)
    observed = read_state(states, parsed, now=NOW)

    first = adapter.next_housekeeping_change(observed, key)
    assert first is not None
    assert (first.entity_id, first.target) == (
        configured.time_entity_id,
        "00:00-00:00",
    )

    states[configured.time_entity_id]["state"] = "00:00-00:00"
    observed = read_state(states, parsed, now=NOW)
    second = adapter.next_housekeeping_change(observed, key)
    assert second is not None
    assert (second.entity_id, second.target) == (
        configured.current_entity_id,
        Decimal("0"),
    )

    states[configured.enable_entity_id]["state"] = "on"
    observed = read_state(states, parsed, now=NOW)
    assert adapter.next_housekeeping_change(observed, key) is None
