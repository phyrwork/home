# T0008 — Cut over to observation coordinator and heartbeat

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0007 — Implement candidate and fail-safe policy actuation

## Objective

Replace the coordinator's stub actuation path with real, read-only Solis
observation, a default-on control-disable guard, explicit fail-safe lifecycle and
an always-advancing heartbeat. Preserve the existing planner as diagnostic-only
until its later capability and strategy cutover.

This task changes the coordinator hotspot once. It does not apply the healthy
candidate, enable a slot, deploy or access live Home Assistant.

## Named timing limits

Define:

```python
HEARTBEAT_INTERVAL = timedelta(minutes=1)
HEARTBEAT_STALE_AFTER = timedelta(minutes=3)
FAIL_SAFE_ATTEMPT_BUDGET = timedelta(seconds=45)
SHUTDOWN_FAIL_SAFE_BUDGET = timedelta(seconds=45)
```

The stale threshold includes scheduling and normal fail-safe jitter. The
independent watchdog in T0009 uses the same named threshold.

Extend T0007 fail-safe orchestration with an absolute deadline. Before starting
each primitive, stop when the deadline is exhausted and report unsafe. A chain of
sequential entity readback timeouts must not block coordinator heartbeat or Home
Assistant shutdown indefinitely.

## Control-disable guard

Add:

```text
input_boolean.house_battery_control_disable
```

with a friendly name explaining that it disables automatic house battery control
and with `initial: on` so every startup is fail-closed. No startup or recovery path
may clear it automatically.

Interpret missing, `unknown`, `unavailable` or invalid guard state as asserted.
Only exact `off` means a user has opened the observation/commissioning window.

Add the helper entity ID to typed configuration. Do not remove the legacy helper
configuration yet.

## Stub-boundary cut

Remove every coordinator call to `stub_inverter.async_apply`. Retain the stub
module, helper definitions and legacy config fields for the later deployment
cleanup task, but stop mutating them.

Stop reading and subscribing to the stub SOC helper immediately. Use valid real
Solis SOC from T0004 for energy diagnostics and observation planning.

Retain the legacy shared power-limit helper temporarily only for planner
diagnostics. Mark every result derived from it with an explicit
`OBSERVATION_ONLY_LEGACY_POWER_LIMIT` source-quality flag. Such a result must never
be passed to T0007, T0006 or any candidate/slot actuator.

## Observation snapshot

Return an immutable coordinator snapshot for every completed cycle, including
degraded cycles and caught exceptions. It records at least:

- cycle/heartbeat timestamp;
- last completely healthy timestamp;
- complete controller health;
- fail-safe obligation and pending-attempt state;
- guard state and quality;
- full or partial T0004 Solis result;
- current fail-safe proof/result when available;
- diagnostic battery energy when SOC is valid;
- optional legacy planner recommendation and reserve;
- explicit source-quality/provenance flags;
- ordered issues and unexpected-error summary.

Expected bad external data does not raise `UpdateFailed`; it returns a degraded
snapshot so heartbeat and health remain observable. Unexpected exceptions are
caught, a fail-safe obligation is set, fail-safe is attempted and the heartbeat
snapshot still advances. Never swallow process cancellation.

`last_healthy_at` updates only when the complete observation is healthy, including
all required Solis, tariff, forecast and helper inputs.

## Complete health

Complete observation health requires:

- T0004 Solis health is `HEALTHY`;
- the guard state is syntactically valid, whether asserted or cleared;
- tariff rates and export price are valid and cover the required horizon;
- Forecast.Solar data and load forecast are valid;
- reserve and remaining diagnostic helper inputs are valid;
- no unexpected exception occurred.

Any missing or invalid planning input makes the observation degraded and removes
the legacy recommendation. Degradation sets a fail-safe obligation even though
the planner is not actuating.

## Coordinator scheduling

Use one `DataUpdateCoordinator` periodic `update_interval=HEARTBEAT_INTERVAL` plus
event-driven `async_request_refresh` for configured source changes.

Remove the old reserve-end timer. Do not retain two independent timer mechanisms.
Rely on DataUpdateCoordinator refresh coalescing for simultaneous entity changes.

Subscribe to:

- every configured real Solis telemetry/control entity;
- the guard helper;
- tariff/export and remaining diagnostic planner helpers;
- other existing event-backed sources as applicable.

Do not subscribe to stub SOC, target SOC or operating-mode helpers. T0004 slot and
control writes may request an event refresh, but refresh scheduling must not create
actuation recursion.

## Fail-safe obligation

Track an explicit `fail_safe_obligation`. Set it on:

- startup;
- orderly shutdown;
- asserted or invalid guard;
- any complete-observation degradation;
- any unexpected coordinator exception;
- an already-started fail-safe attempt until it completes and final proof is
  successful.

Healthy observations do not erase an outstanding obligation.

When the guard is exactly off, the complete observation is healthy and there is
no pending attempt or obligation, remain observation-only. Do not require or force
Self-Use in that state: this is the safe commissioning window for T0011.

T0011 must require:

- complete coordinator health;
- guard exactly off;
- no fail-safe obligation;
- no pending fail-safe task;
- all 12 slots currently proven disabled.

## Fresh fail-safe proof and bounded attempts

When an obligation exists, freshly read and prove:

- all 12 slot directions off;
- storage mode `Self-Use`;
- Grid Peak Shaving on;
- Battery Reserve off.

Do not rely on a cached earlier success. If proof is complete, clear the obligation
and avoid redundant writes. If incomplete and no task is pending, start at most
one T0007 fail-safe attempt bounded by `FAIL_SAFE_ATTEMPT_BUDGET`.

Do not await the background attempt inside the heartbeat cycle. Return a snapshot
immediately with pending/unsafe status. Never cancel a pending attempt merely
because the guard becomes off; commissioning waits for it.

The task completion callback must:

- consume its result or exception;
- clear the pending marker;
- retain ordered diagnostics;
- request a coordinator refresh even when no entity-state event arrived.

No fail-safe task may be leaked or left with an unconsumed exception.

## Startup and shutdown

Startup sets a fail-safe obligation before the first observation. The default-on
guard prevents candidate or slot application.

On shutdown:

1. unsubscribe state listeners;
2. stop DataUpdateCoordinator heartbeat scheduling;
3. prevent actuator state changes from scheduling another cycle;
4. set the fail-safe obligation;
5. invoke one fail-safe attempt bounded by `SHUTDOWN_FAIL_SAFE_BUDGET`;
6. record/log unsafe status when final proof is incomplete;
7. consume and close every task before returning.

Do not wait indefinitely for cloud control during Home Assistant shutdown.

## Diagnostic planner

Refactor the existing input boundary so a validated real Solis SOC is supplied to
the legacy planner instead of read from the stub helper. Continue using the legacy
power limit only to calculate a non-actuating recommendation.

The diagnostic planner may be absent while energy/SOC diagnostics remain present.
Its command must be labelled a recommendation, never an applied control.

No recommendation or planner-derived power limit can reach a real actuator in this
task, including after guard changes or exception recovery.

## Sensors

Expose at least:

- heartbeat timestamp sensor, available for every returned observation;
- controller health sensor, available for healthy, degraded and fail-safe
  observations;
- fail-safe obligation/pending state in attributes;
- current guard state/quality;
- source-quality flags and issue codes.

Update the existing control sensor so its old command/control data is clearly
labelled as an observation-only legacy recommendation, not an applied mode.

Energy sensor availability depends only on valid real SOC. Reserve target/balance
sensors require a valid diagnostic planner result. Preserve their stable unique
IDs where their meaning remains valid.

## Compatibility boundaries

- Do not apply the healthy candidate or a dynamic slot.
- Do not issue commissioning authorization.
- Do not add the independent automation watchdog yet.
- Do not delete stub files, legacy config fields or helper definitions.
- Do not deploy or access live Home Assistant.

## Tests

Cover:

- guard on, off, missing, unknown, unavailable and invalid states;
- proof no path clears the guard automatically;
- startup fail-safe obligation before observation;
- no stub SOC read or subscription;
- real SOC feeding only energy/diagnostic planner paths;
- explicit legacy-power-limit source quality and actuator isolation;
- complete health including planner inputs;
- expected degradation and unexpected exception snapshots advancing heartbeat;
- one-minute periodic update plus coalesced event refresh;
- removal of the old reserve timer;
- fail-safe obligation creation, persistence and fresh proof;
- healthy guard-off commissioning window with no forced fail-safe;
- no cancellation of pending fail-safe when the guard clears;
- one bounded background fail-safe task and no duplicates;
- consumed success/exception and completion-triggered refresh;
- retry after unsafe completion;
- deadline exhaustion without heartbeat blockage;
- shutdown unsubscribe/order/deadline and no refresh from shutdown writes;
- heartbeat/health sensor availability for every snapshot;
- energy versus reserve sensor partial availability;
- old recommendation explicitly not applied;
- `CancelledError` propagation and task/listener cleanup.

Run all existing integration tests when dependencies are available.

## Acceptance criteria

- The coordinator performs no stub or real candidate/slot actuation.
- Real SOC replaces stub SOC for all observations.
- The default-on guard is fail-closed and never auto-cleared.
- Heartbeat advances independently of complete input health or fail-safe duration.
- Every safety obligation is freshly proven or retried within finite budgets.
- A healthy guard-off commissioning window is possible only after pending cleanup
  completes.
- Planner output is explicitly diagnostic and cannot reach an actuator.
- Shutdown is bounded and schedules no new work.
- Focused offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0008-observation-coordinator-heartbeat`
- Isolated worktree.
- Small-model implementation agent after T0007 is integrated.
- This is the serialized coordinator/input/sensor/config hotspot task; do not run
  another implementation touching those files concurrently.
