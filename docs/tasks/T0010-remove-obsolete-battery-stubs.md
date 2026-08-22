# T0010 — Remove obsolete battery SOC and actuator stubs

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- integrated and passing T0008 observation coordinator cutover;
- integrated and passing T0009 independent watchdog.

## Objective

Remove the now-unused stub SOC, target-SOC and operating-mode deployment/runtime
boundary in one atomic repository change. Retain only the explicitly
observation-only shared power-limit helper until T0013 replaces the legacy planner
capability input.

This task does not deploy or access live Home Assistant.

## Hard sequencing precondition

Before deletion, prove the integrated runtime contains no import or call of
`stub_inverter`. T0008 must already:

- read real SOC from the T0004 Solis observation;
- supply that SOC to the diagnostic planner;
- perform no stub write;
- label the remaining helper power limit observation-only;
- prevent that value or planner recommendation reaching T0006/T0007.

Do not merge helper deletion against an older coordinator revision.

## Remove deployment helpers

From `deployment/files/input_numbers/house_battery.yaml`, delete exactly:

- `house_battery_state_of_charge`;
- `house_battery_state_of_charge_target`.

Preserve in that file:

- `house_battery_power_limit`;
- `house_battery_reserve_margin`;
- `house_battery_export_hysteresis`.

Delete the obsolete input-select definition containing:

- `house_battery_operating_mode`.

If that file contains no other definitions, delete the file atomically.

## Remove runtime stub boundary

Delete:

- `dependencies/stub_inverter.py`;
- its focused test module;
- every runtime import/call/reference.

Do not yet delete the old abstract controller commands, Solis command mapper or
deterministic simulation cases: they remain observation/recommendation assets
until the strategy and simulation migration.

## Configuration cleanup

Remove:

- `BatteryConfig.state_of_charge_entity_id`;
- the complete legacy `InverterConfig` type;
- `Config.inverter`;
- legacy `battery.state_of_charge_entity_id` deployed YAML;
- the top-level legacy `inverter` YAML section;
- corresponding parser and tests.

After this change, configuration containing either removed key must fail as
unknown. This detects a partial or stale deployment rather than silently accepting
dead stub configuration.

Keep `BatteryConfig.power_limit_entity_id` and its deployed helper ID until T0013.
Its documentation and diagnostics must call it observation-only legacy input.

## Real-SOC planner proof

Add an integration-level offline test proving the T0008 diagnostic planner can
produce its observation-only recommendation with:

- real Solis SOC;
- the retained shared power-limit helper;
- no stub SOC entity or configuration field.

Assert that the resulting power limit and recommendation have no route to
candidate or slot actuation.

## Active-reference scan

Add or run a targeted scan proving removed IDs and `stub_inverter` are absent from:

- deployed active YAML;
- executable custom-integration runtime code;
- current runtime tests and fixtures, except explicit stale-config rejection
  fixtures.

Do not fail on:

- historical task documentation;
- the intentional `_LEGACY_STUB_IDS` rejection list in `solis_config.py`;
- a negative test that proves removed configuration is rejected.

## Documentation

Update current scratch/design notes that still describe the stub as active. Keep
historical task-card evidence intact. Check `TODO.md` and mark an item only if the
change fully resolves it.

## Deployment handoff

The later authenticated deployment must apply this as:

1. one integrated commit after T0008 and T0009 are installed;
2. one full Ansible run so custom component, config and helper deletion remain
   atomic;
3. Home Assistant configuration validation and required restart;
4. proof the three removed helper entities are absent;
5. proof all 12 slot switches, storage-mode select, Peak Shaving switch, Battery
   Reserve switch and watchdog guard still resolve.

This task records those checks but does not run them without user authentication.

## Compatibility boundaries

- Do not remove `house_battery_power_limit` yet.
- Do not remove reserve-margin or export-hysteresis helpers.
- Do not remove old abstract commands or simulation coverage.
- Do not change dynamic strategy or real actuation.
- Do not deploy or access live Home Assistant.

## Tests

Cover:

- config without removed fields parses;
- removed battery SOC and top-level inverter keys are rejected as unknown;
- diagnostic planner uses real SOC and retained power-limit input;
- retained power-limit provenance is observation-only;
- no path passes it to T0006/T0007;
- exact two input-number definitions removed and three preserved;
- obsolete input-select definition/file removed;
- stub module/test/import/call absent;
- active-reference scan with documented intentional exceptions;
- watchdog static entity mapping remains complete;
- all existing integration tests updated and passing when dependencies exist.

## Acceptance criteria

- No executable runtime path refers to stub SOC, target SOC, operating mode or
  `stub_inverter`.
- Real Solis SOC is the only runtime battery SOC source.
- The only remaining stub-like power helper is explicitly observation-only and
  cannot actuate.
- Stale legacy configuration fails closed.
- Deployment cleanup is one atomic commit ready for one full Ansible run.
- Offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0010-remove-obsolete-battery-stubs`
- Isolated worktree.
- Small-model implementation agent after T0008 and T0009 are integrated.
- This is a serialized config/helper cleanup task; do not overlap it with another
  config/coordinator/helper implementation.
