# T0030 — Event-driven battery controller

Status: Implemented

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

## Implementation decisions

- `Controller` uses one non-eager HA task for a dirty/coalescing worker. The
  non-eager creation is required because an event received during eager task
  startup could otherwise run before the task handle was assigned and create a
  second worker. Each pass performs at most one adapter write.
- Start retry state is keyed only by the desired plan, planning-source tokens,
  and the pending entity ID plus adapter-normalized target. HA/Solis observation
  revisions, inverter time, telemetry and the recaptured CAS precondition are
  deliberately excluded so polling churn cannot renew a failed start budget.
  Attempts occur at generation offsets 0, 15 and 60 seconds. An unchanged
  generation is suppressed after its third failure; a materially changed plan
  or pending entity/target is reevaluated from live state.
- `StopDebt` contains only a configured `SlotKey`, attempt, capped next retry and
  ambiguity. Cancellation makes debt ambiguous and immediately due before it
  propagates. Ambiguous optimistic `off` never retires debt.
- Two council-approved narrow Solis APIs were required by the controller:
  `conflicting_enabled_keys(state, intent)` keeps native split/allocation and
  exact field comparison inside `solis.py`; `stop(..., force=True)` requires a
  real blocking off service plus a newer matching revision for existing
  ambiguous debt. The adapter also retains an unfinished cancellation-ignoring
  service task so no later service call can overlap it.
- Runtime fail-safe attempts due important stops independently of unreliable
  Storage Mode reads/writes. Orderly shutdown is intentionally stricter:
  confirmed Self-Use first, then observed-on directions, with unknown directions
  reread and never written speculatively. Every shutdown retry/reread publishes
  a fresh heartbeat, preventing the stale-heartbeat sentinel from becoming a
  second writer during a prolonged orderly shutdown.
- Used-slot cleanup is memory-only and narrow: only a slot enabled by this
  controller may have its confirmed-off time/current reset, one field per pass.
  It is best effort and never affects health, stop progress or fail-safe.
- The sentinel has one minute trigger plus one HA-start trigger. `mode: single`
  holds the startup instance for ten minutes, suppressing minute triggers during
  grace; it then rechecks heartbeat and Storage Mode. Normal operation uses the
  three-minute stale threshold. This assumes automations receive the HA start
  event; an automation-only reload has no new startup grace and relies on the
  controller's current heartbeat.

## Local acceptance evidence

Behavior tests prove the T0030 acceptance surface:

1. an event during a write coalesces into a later pass under one worker;
2. one pass advances only one start, stop, mode or housekeeping change;
3. all planning and configured Solis entities are event sources;
4. tariff boundaries and retry deadlines pre-empt the one-minute backstop;
5. starts occur at 0/15/60 seconds and suppress the unchanged generation across
   inverter-time, telemetry and observation-revision churn, while a changed
   plan or pending entity/target creates a new generation;
6. stops retry without a terminal attempt and cap at 60 seconds;
7. service timeout and cancellation preserve ambiguous debt and require forced
   blocking proof even after optimistic `off`;
8. a cancellation-ignoring HA call cannot overlap its retry;
9. later observed-on drift recreates already-proved stop work;
10. absolute minimum SOC stops discharge with unavailable target and planning;
11. target-complete and owned-expired directions stop before planning;
12. conflicting direction stop is confirmed before any start call;
13. a planning issue preserves cycle state/deadline and makes no start call;
14. continuous degradation latches fail-safe at 15 minutes and recovered inputs
    cannot clear it; a fresh controller instance has no latch;
15. fail-safe retries only mode or known stops and does not invoke start writes;
16. diagnostic entities retain identities and expose degradation, fail-safe,
    pending operation, attempt and next-retry attributes;
17. concurrent teardown removes source/timer/task state exactly once;
18. shutdown confirms Self-Use before stopping only observed-on directions,
    rereads unknown directions without speculative writes, and refreshes its
    heartbeat throughout a simulated shutdown lasting longer than three minutes;
19. the sentinel provides startup grace, rechecks heartbeat/mode immediately
    before action and contains exactly one Self-Use service write; and
20. Solis conflict projection covers exact split, mismatch, extra, idle and
    unknown cases without exposing native schedule types.

Local gates: 94 component tests and 41 deployment tests pass. `compileall`,
absorbed-coordinator import search, single-writer search and `git diff --check`
pass. `controller.py` is 1,018 lines and `solis.py` is 1,275 lines at this staged
boundary. No 1Password, network, SSH, browser, live Home Assistant or deployment
access was used.

## Deferred

- T0031 owns deletion of the now-unreachable broad script, guard helper,
  remaining legacy modules/contracts and final repository/documentation audit.
- Ansible, live commissioning, restart recovery, shutdown, midnight split and
  24-hour behavioral evidence remain auth/live acceptance under T0026.

## Review

A small-model safety review initially blocked minimum-SOC ordering, fail-safe
stop progress, cancellation wakeup, startup grace and cancellation-ignoring
service overlap. Each received a focused regression and the re-review approved
the implementation. Final review then blocked stale sentinel ownership during a
prolonged shutdown and retry generations coupled to unrelated observation/CAS
revision churn. Shutdown heartbeat publication and the lean generation key now
have focused regressions; both blockers are resolved. Two independent final
reviewers approved commit `71d02dc` with no remaining blocker.
