# T0030 — Event-driven battery controller

Status: Accepted

Depends on: T0029

## Objective

Cut over to one `controller.py` owning reconciliation, retries, health,
fail-safe, lifecycle and orderly shutdown. Preserve diagnostic entity identities
and replace the broad watchdog with the T0026 mode-only crash sentinel.

No other task may edit controller lifecycle, sensors or battery automation YAML
concurrently.

## Worker

Use one worker and the Solis adapter's one write lock:

- a trigger marks the controller dirty;
- one running worker clears dirty, performs one pass, and repeats if another
  event arrived during the pass;
- event bursts coalesce without losing an event received during a write; and
- one pass advances at most one Solis change.

Priority:

1. continue latched Self-Use fail-safe and known stop work;
2. add stops for conflicting, expired or target-complete enabled directions;
3. stop any enabled discharge observed at/below `MINIMUM_SOC_PERCENT` even
   without a plan;
4. execute the next due stop;
5. derive the current plan;
6. restore/confirm Feed-In Priority before a start;
7. advance one due best-effort start change; then
8. perform one due used-slot housekeeping change.

Planning and Feed-In validity never gate known stops. Unknown enable state blocks
starts and is never written speculatively.

## Retry and proof

Every call has a 30-second monotonic deadline.

Starts use `START_RETRY_DELAYS = (0s, 15s, 60s)`, keyed by exact intent plus a
relevant planning/control generation. Recheck eligibility every time. After
three failures, suppress that generation. A failed start is `DEGRADED` and never
causes cleanup or Self-Use.

Stops retain only a validated configured slot key, attempt count, next attempt
and ambiguity. Use
`min(5s × 2**attempt, 60s)` forever until provisionally proved off. Strategy
changes cannot cancel them. After timeout/error/cancellation, force a later
successfully completed blocking off call even if optimistic HA state says off.
Later drift to on recreates stop work.

## Events and timers

Subscribe to every planning input and every configured Solis telemetry/control
entity. Schedule exact aware wakeups for tariff, slot and cycle boundaries plus
retry/fail-safe deadlines. Retain one minute only as a missed-event backstop.

## Health and fail-safe

- Healthy: desired state reconciled and no stop/start failure is pending.
- Degraded: inputs, plan, start, stop or readback are insufficient.
- Fail-safe: continuous degradation reaches 15 minutes or a hard invariant fails.

Fail-safe is latched, writes/retries only Self-Use, suppresses starts and
continues known stops. It never writes reserve, grid charging, slot fields,
capabilities, Peak Shaving or feed limits. Recovered inputs do not clear it; only
a fresh integration setup/restart does.

## Startup and shutdown

`__init__.py` creates exactly one controller. Fresh setup reconstructs state from
live entities and restores Feed-In Priority only after valid prerequisites;
known stops remain higher priority.

Shutdown:

1. reject starts, cancel ordinary timers and publish a fresh shutdown heartbeat;
2. select and confirm Self-Use;
3. read all 12 enables;
4. switch off only directions observed on;
5. reread unknown directions with stop backoff; and
6. continue until all are off or HA forcibly terminates.

Teardown removes listeners, timers and tasks exactly once.

## Diagnostics

Preserve entity IDs/unique IDs for heartbeat, health, action, reserve SOC,
battery energy, reserve target and reserve balance. Publish last error,
degraded/fail-safe time, pending operation, attempt and next retry as attributes,
not new helpers.

## Crash sentinel

Replace the battery automation with one once-per-minute rule:

- ten-minute HA startup grace;
- heartbeat stale after three minutes;
- fresh heartbeat suppresses action regardless of health/action value;
- immediately recheck heartbeat and Storage Mode;
- select Self-Use only when still stale and not already Self-Use; and
- never call a script or write any other control/helper.

## Files and tests

Create/replace `controller.py`, `__init__.py`, `sensor.py`, controller/setup/sensor
tests, `automations/house_battery.yaml`, and a sentinel deployment test. Delete
the old coordinator and its tests. T0031 deletes remaining obsolete surfaces.

Prove priority, event coalescing, exact timers, minute backstop, bounded starts,
unbounded stops, timeout ambiguity, drift, minimum-SOC stop, direction gating,
15-minute latch, no auto-unlatch, fresh-setup recovery, diagnostics, teardown,
strict shutdown and sentinel write limits.

## Completion

- Only `controller.py` owns runtime state/retries.
- Only `solis.py` writes integration controls.
- Broad watchdog/script/guard behavior is unreachable.
- Focused and full local tests pass.
- Commit from an isolated worktree; do not deploy or access live HA.
