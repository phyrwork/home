# T0038 — Preserve active cheap-charge schedules

Status: Implemented — local verification complete

Depends on: T0035

## Problem and evidence

During a live Intelligent Octopus bonus dispatch on 2026-08-24, the controller
correctly stopped reserve discharge and enabled a charge slot for
`14:40–17:00`. On each subsequent minute the planner advanced the desired start
to the current minute, so the otherwise unchanged active slot was rewritten as
`14:41–17:00`, then `14:42–17:00`. Each rewrite caused needless Solis Cloud
writes and transient degraded health while charging continued.

T0035 already treats the equivalent minute roll-forward as continuous for an
active reserve-discharge slot. Cheap charging needs the same narrowly bounded
rule.

## Minimal decision

Extend the existing Solis direction-match continuity predicate to accept a
shifted start for an active configured `CHEAP_CHARGING` charge slot only when:

- the physical slot, owner and direction are unchanged;
- the slot is enabled;
- current and target SOC match exactly;
- the native end minute and midnight representation match exactly; and
- inverter-local `now` is inside both observed and desired half-open schedules.

Apply this comparison per native segment. The existing slot-key mapping must
prove the same physical slot and direction; both observed and desired owners
must be `CHEAP_CHARGING`, and the slot-key direction must be `CHARGE`.

The shifted start remains a conflict before the desired start, at or after
either schedule end, or when the end, current, target SOC, owner, direction or
physical slot changes. Removing the cheap-charge intent at the half-open cheap
window end bypasses continuity and retains the normal stop obligation.

Do not change planner scheduling, retry or timeout behavior, session handling,
full-SOC cycling, or cross-midnight slot allocation. Existing split-midnight
behavior remains unchanged.

## Files

- `deployment/files/custom_components/house_battery_control/solis.py`
- focused Solis adapter tests

## Local acceptance

Tests must prove:

- an active cheap-charge slot remains matched across at least three successive
  minute-shifted desired starts, producing no time change;
- before the desired start and at or after either half-open end, it remains a
  conflict;
- changing end, current, target SOC, owner, direction or slot remains a
  conflict;
- removing the intent after the cheap window ends produces the normal stop;
- native `24:00` and `23:59` split-midnight boundaries do not preserve a
  segment beyond its own half-open end;
  and
- existing reserve-continuity, split-midnight and full component tests pass.

## Live acceptance

During an active Intelligent bonus dispatch:

1. observe at least three minute boundaries without an active-slot time write;
2. confirm the charge slot remains enabled, controller health remains healthy,
   and site import or battery telemetry proves charging continues; and
3. at the dispatch half-open end, confirm the controller stops the slot and
   observes it off.

## Rollback

Revert this focused adapter change and perform one clean Home Assistant Core
restart. Do not manually reload the Solis telemetry integration.

## Implementation evidence

- Extended the existing `_direction_matches` predicate to preserve an active
  cheap-charge segment only for the same configured native key, owner and
  direction, with exact current/target values and matching native end
  representation while both half-open schedules contain inverter-local now.
- Added focused tests for three successive minute shifts, all identity/value
  mismatches, both `24:00` and `23:59` native midnight boundaries, and normal
  stop selection after the cheap interval ends.
- `PYTHONPATH=deployment/files /Users/connor/src/home/deployment/.venv/bin/pytest
  -q deployment/files/custom_components/house_battery_control/tests/test_solis.py`
  — 26 passed.
- `PYTHONPATH=deployment/files /Users/connor/src/home/deployment/.venv/bin/pytest
  -q deployment/files/custom_components/house_battery_control/tests`
  — 100 passed.
