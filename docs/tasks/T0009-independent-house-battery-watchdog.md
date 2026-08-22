# T0009 — Add an independent house battery watchdog

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0008 — Cut over to observation coordinator and heartbeat

## Objective

Add a Home Assistant script and automation that establish the same guarded
fail-safe state using built-in entity services only. They must continue to work
when the custom integration, coordinator or Python actuator is unavailable.

This task is static deployment code with offline tests. It does not deploy or
access live Home Assistant.

## Independent fail-safe state

The watchdog proves all of these exact states:

- `input_boolean.house_battery_control_disable` is `on`;
- every configured charge and discharge slot switch 1 through 6 is `off`;
- `select.garage_inverter_control_storage_mode` is `Self-Use`;
- `switch.garage_inverter_control_grid_peak_shaving` is `on`;
- `switch.garage_inverter_control_battery_reserve` is `off`.

Missing, unknown, unavailable or any other state is unsafe.

## Independent fail-safe script

Add `script.house_battery_fail_safe` using only built-in Home Assistant services
and exact entity IDs from the deployed T0003 mapping.

The first action always turns on
`input_boolean.house_battery_control_disable`. The script never clears it.

After asserting the guard, evaluate a fresh exact proof. When the complete
fail-safe state already holds:

- make no Solis service calls;
- dismiss a stale watchdog notification if present;
- finish successfully.

Otherwise perform at most two complete reconciliation passes within a total
30-second bound. Each pass:

1. turns off each of the 12 slot switches as a separate action;
2. selects `Self-Use`;
3. turns on Grid Peak Shaving;
4. turns off Battery Reserve;
5. waits a bounded time for the complete exact proof;
6. re-evaluates fresh state before deciding whether another pass is required.

Every mutation action has `continue_on_error: true` so one entity failure cannot
prevent attempts on later entities or final proof. The script is idempotent and
uses a single-run mode so minute triggers do not overlap.

The second pass closes the race with an in-flight custom-component write that
started before the watchdog asserted the guard. T0006 and T0007 provide the
complementary post-write guard checks.

## Notification

After the final pass, create or update one persistent notification with a stable
ID only when final proof remains unsafe and either:

- no notification exists; or
- the calculated failure reason changed.

Do not create or update the same notification on every one-minute retry. Dismiss
it when a later run proves the complete fail-safe state.

Notification during Home Assistant shutdown is best effort. The T0008 direct
shutdown fail-safe remains primary.

## Watchdog automation

Add an automation invoking the script on:

- Home Assistant start;
- Home Assistant shutdown;
- every minute;
- heartbeat state changes;
- controller-health state changes;
- control-disable guard state changes.

Start and shutdown invoke the script unconditionally.

For normal triggers, invoke it when any condition holds:

- guard is not exact `off`;
- health is not exact `healthy`;
- heartbeat is missing, `unknown`, `unavailable` or unparseable;
- heartbeat is older than `HEARTBEAT_STALE_AFTER`;
- heartbeat is more than `MAXIMUM_FUTURE_CLOCK_SKEW` in the future.

A healthy, current, guard-off observation does nothing. This preserves the T0011
commissioning window and later healthy dynamic operation.

The automation must not call a custom integration service. It may read the custom
heartbeat and health sensors only as evidence.

## Race contract with custom actuation

T0006 must read the guard exact `off` immediately before slot enable and again
after enable readback. A changed or invalid guard invokes all-slot cleanup.

T0007 must re-read the guard immediately before and after every candidate
mutation. A changed or invalid guard invokes fail-safe before any further
candidate write.

The watchdog's second reconciliation pass handles custom-component death between
a write and its post-write guard check.

## Static mapping integrity

The script intentionally embeds exact entity IDs so it is independent of the
custom integration runtime. Offline tests must cross-check its:

- 12 slot switches;
- storage-mode select;
- Peak Shaving switch;
- Battery Reserve switch;
- disable guard;

against the deployed T0003/T0008 YAML mapping. Mapping drift must fail tests and
invalidate the commissioning fingerprint before deployment.

## Compatibility boundaries

- Do not use T0005, T0006 or T0007 services from the automation/script.
- Do not enable a slot or select Feed-In Priority.
- Do not alter physical protection settings, permissions or inverter on/off.
- Do not deploy or access live Home Assistant.

## Tests

Parse and inspect the static YAML to cover:

- exact cross-check of all entity IDs against deployed configuration;
- guard-on is the first mutation and no action clears it;
- start and shutdown unconditional invocation;
- one-minute and state-change triggers;
- exact healthy-state predicate;
- stale, invalid and materially future heartbeat predicates;
- every slot-off action and every policy action has `continue_on_error`;
- initial proof skips every Solis action when already safe;
- no unknown/unavailable state counts as safe;
- no more than two reconciliation passes and a 30-second total bound;
- final proof occurs after the last mutation pass;
- stable notification only on unsafe transition/reason change;
- notification dismissal after recovery;
- no candidate mode, slot-enable or custom integration service action;
- script and automation single-run/non-overlap semantics.

Live template evaluation and Solis service behaviour remain T0011 commissioning
evidence.

## Acceptance criteria

- Custom-component failure cannot prevent built-in fail-safe attempts.
- The guard is asserted before every independent Solis mutation.
- A fully safe guarded state generates zero repeated Solis traffic.
- Late in-flight writes receive one bounded second reconciliation pass.
- Unknown, stale, future or degraded controller evidence is fail-closed.
- Unsafe proof generates one maintained notification and recovery dismisses it.
- Static entity mappings cannot drift silently.
- Offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0009-independent-house-battery-watchdog`
- Isolated worktree.
- Small-model implementation agent after T0008 is integrated.
- Expected changes are limited to one script, one automation, mapping-integrity
  tests, required deployment sync metadata and this card.
