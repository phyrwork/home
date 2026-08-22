# T0019 — Cut over to guarded dynamic coordination

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0009 — independent house-battery watchdog;
- T0016 — guarded dynamic-slot commissioning evidence;
- T0017 — pure strategy composer;
- T0018 — deterministic control simulation.

## Objective

Connect the real readers, pure T0017 composer and verified T0006/T0007 actuators
inside the T0008 coordinator without weakening heartbeat, watchdog, restart or
fail-safe behavior.

The implementation remains locally testable and deployment-disabled. It does not
authenticate, deploy, run commissioning or enable live dynamic control.

## Default-disabled production gate

Add typed configuration and IaC:

```text
dynamic_control_enabled: false
```

Dynamic strategy requires all of:

- exact configuration `true`;
- control-disable guard exact `off`;
- complete fresh sources and controller health;
- current runtime watchdog readiness;
- current configured T0007 persistent candidate authorization;
- exact T0007 manual-grid and capability records;
- exact T0016 `ProductionSlotAuthority`, operating points and timing evidence; and
- a fresh matching T0017 proof/state boundary.

Missing, false, unknown, stale or mismatched evidence keeps the coordinator in
observation/fail-safe mode. Startup never clears the guard or enables control.

## One coordinator and one action worker

Extend the existing T0008 coordinator; do not create a competing polling loop.

Each heartbeat, source event or deadline refresh constructs every T0017 input
from real readers, including:

- Solis telemetry and advanced state proof;
- prices, dispatch and forecasts;
- reserve and source-strategy decisions;
- cycle and composer state;
- runtime voltage/temperature observations;
- production authority and timing evidence; and
- runtime watchdog readiness.

Invoke the pure T0017 composer once per accepted snapshot.

Run at most one separate serialized action task. Interpret T0017's exact mutation
enum, never a nullable slot:

- `STOP_REQUIRED` — T0006 disable-all;
- `APPLY_FAIL_SAFE_POLICY` — T0007 fail-safe;
- `APPLY_CANDIDATE_POLICY` — T0007 candidate with current persistent, manual-grid,
  capability and reserve evidence;
- `APPLY_INTENT` — T0006 normal production gate;
- `KEEP_ACTIVE_NO_WRITE`, wait and idle — no write.

An HA-success result remains pending advanced device proof. Action completion
requests a refresh but never manufactures proof or advances the composer phase.

## Complete TOCTOU revalidation

Queued work contains only its transition ID, source generation, audit/stable
fingerprints, exact mutation and `fresh_until`.

After obtaining action-worker ownership, unconditionally re-read every material
external source:

- guard, dynamic config and all current authorizations;
- watchdog readiness;
- Solis/device state;
- tariff, dispatch and forecasts;
- runtime voltage/temperature;
- Store journal/state; and
- every current source revision.

Rebuild the complete snapshot and rerun T0017. Immediately before calling an
actuator require:

- unchanged source generation;
- same stable mutation identity;
- current audit evidence and authority;
- current time before every freshness/deadline; and
- enough remaining apply, proof, stop and cleanup budget.

Otherwise discard and reconcile.

Every subscribed external callback and internal config, authorization, watchdog
or Store update synchronously increments `source_generation`. A queued decision
carries its generation. A generation change during actuation sets an invalidation
obligation handled by the single cleanup sequence.

Do not hold a T0005 writer transaction or T0006/T0007 orchestration lock while
reading sources, running the composer, using Store, persisting a lease or
coordinating cancellation. Those locks exist only inside one bounded actuator
call after the generation check.

## Non-blocking heartbeat and invalidation

Coordinator read/heartbeat refresh never waits for a long action. Keep at most
one action task and deduplicate identical queued apply mutations. `KEEP`, wait and
idle never rewrite a slot.

Guard assertion, dispatch withdrawal, source expiry or unhealthy evidence signals
cancellation of a risk-increasing action. Await that action's own shielded
T0006/T0007 cleanup under one absolute deadline. Do not start a concurrent stop
against the same orchestration lock.

After it finishes, freshly prove state and run at most one follow-up fail-safe
cleanup with the remaining deadline when still unsafe. Diagnostics distinguish
initial cleanup, follow-up cleanup and deadline exhaustion.

## Runtime watchdog readiness

Re-read a non-mutating `WatchdogReadinessProof` before every admission and
continuation. It binds:

- exact T0009 automation entity, available and enabled;
- fail-safe script available;
- heartbeat freshness;
- guard semantics and required-entity resolvability;
- latest successful commissioned watchdog self-test revision, time and
  `fresh_until`;
- T0009 schema, script and automation fingerprints; and
- subscribed readiness source IDs and current source generation.

Never invoke a mutating watchdog self-test while the guard is off. Historical
T0016 commissioning evidence alone does not prove the current automation/script
is healthy.

## Bounded transition lease

T0009 must not mistake a legitimate HA-applied/device-proof-pending transition
for an arbitrary degraded integration state.

Publish an immutable `ActiveTransitionLease` containing:

- schema, monotonic transition ID and source generation;
- allowlisted mutation;
- stable and production-authority fingerprints;
- issued time and strict expiry;
- heartbeat revision;
- write-ahead journal checksum; and
- `HA_PENDING_DEVICE` status.

Create it only after the write-ahead journal is durably flushed and clear it
after proof or cleanup.

T0009 may accept `TRANSITION_PENDING` only when the heartbeat is fresh, guard is
exact `off`, every field/fingerprint matches and current time is strictly before
expiry. Any other non-healthy state invokes its normal guard/fail-safe behavior.

Require strictly:

```text
lease_expiry
+ measured_watchdog_trigger_cadence
+ watchdog_script_execution_bound
< min(
    intent_end,
    intent_expiry,
    every_evidence_fresh_until,
    mutation_cleanup_deadline,
)
```

Lease duration is also capped by the current commissioned transition/device-proof
budget. A lease is immutable and non-renewable. Another action requires a new
monotonic journal transition, full source reread/generation check and new lease.

## Crash-consistent state journal

Persist versioned composer/cycle/action state through Home Assistant Store, but
never treat it as current device authority.

Use a checksum chain with strictly monotonic transition ID and generation.
Before every risk-increasing policy or slot mutation, atomically persist and
flush `PendingTransition` containing:

- prior checksum/state;
- audit/stable/mutation fingerprints;
- consumed T0015 full revision and exact next cycle state;
- prior composer state;
- current authority and source revisions;
- creation time and every deadline.

If the pre-write flush fails, perform no action.

After an actuator result, append and flush its result/pending-device state. Only
after an advanced matching device proof may the journal commit next
`ComposerState`/`CycleState` and complete the transition. HA readback alone never
commits it.

Any append/flush failure after a possible physical mutation immediately:

1. preserves the original pending record;
2. enters a durable-recovery-required sentinel;
3. asserts and proves guard on;
4. performs the single bounded cleanup sequence;
5. proves fail-safe; and
6. blocks all dynamic continuation until later durable recovery.

Never persist tokens, API secrets or reusable authorization.

## Exact restart sentinel

Restart with a pending/incomplete journal, corrupt/missing state or unknown schema
uses an exact `RestartSentinel`:

1. guard assertion and advanced fail-safe/all-off proof;
2. candidate-policy application and later advanced candidate proof; and
3. a strictly post-restart authoritative fresh-full revision before any T0015
   admission.

Retain any pending consumed full revision as inhibited. Non-cycle strategies
still require their normal current proofs and policy convergence.

Restored state is diagnostic/inhibition history only; it never bypasses restart
reconciliation.

## Deadline scheduling

Retain heartbeat and source subscriptions. Maintain exactly one aware-UTC
next-transition timer at the earliest:

- composer `fresh_until`;
- intent end/expiry;
- final-charge cutoff;
- source/dispatch expiry;
- cycle or policy deadline; or
- reconciliation deadline.

Use a generation token so stale callbacks are no-ops. Reschedule on every refresh
and fire overdue deadlines immediately. Heartbeat and T0009 cover timer loss.
Withdrawal events enqueue stop priority before other work.

## Device-proof construction

Build T0017 `AppliedStateProof` only from:

- exact mapped slot configuration and all enable states;
- persistent settings, protections and capabilities;
- exact guard and HA revisions;
- advanced Solis source/device revision and timestamp strictly after mutation;
- exact current fingerprints; and
- commissioned cloud-latency/reconciliation semantics.

Never derive device proof, runtime voltage/temperature or a later-full revision
from SOC arithmetic or HA service response.

## Shutdown, unload and unexpected failure

Set an internal stopping flag so no new risk action can enqueue, then:

1. immediately turn the control-disable guard on through a verified built-in
   guard actuator and require fresh exact HA proof;
2. cancel the current risk task and await its own shielded cleanup under one
   absolute shutdown deadline;
3. freshly read all slots and persistent policy;
4. if still unsafe, run exactly one follow-up T0007 fail-safe using the remaining
   deadline;
5. return only with typed final safe proof or explicit unsafe result; and
6. cancel timer, unsubscribe listeners and stop the coordinator.

Generation/stopping flags make late callbacks no-ops. Reload follows the same
sequence.

Unexpected dynamic-control failure follows guard-on, existing-action cleanup,
fresh proof and at most one follow-up fail-safe. It still advances the T0008
heartbeat as degraded/fail-safe. Preserve and re-raise `CancelledError` only after
bounded cleanup.

T0009 remains an independent backup, not an ordering assumption.

## Diagnostics

Extend the coordinator snapshot with:

- desired status and mutation obligation;
- audit/stable fingerprints;
- composer and cycle phase;
- queued/applied transition and lease;
- journal transition/checksum/recovery state;
- every deadline and source generation;
- authority/source freshness;
- HA/device proof revisions;
- actuation results; and
- failure, unsafe, cancellation and reconciliation details.

Dedicated sensor cleanup belongs to a later diagnostics task.

## Compatibility boundaries

- Keep dynamic configuration and IaC disabled.
- Do not deploy, authenticate or run live commissioning.
- Do not remove legacy diagnostics/stubs until their later cleanup task.
- Preserve T0008 heartbeat and T0009 independence.

## Tests

Use fake HA, Store, clock, scheduler, readers, composer and actuators to cover:

- default-disabled config, guard and every authorization input;
- exact mutation routing and no-write keep/wait/idle;
- complete source-generation CAS and every TOCTOU mutation;
- one worker, deduplication and invalidation cancellation ordering;
- no external I/O under writer/orchestration locks;
- delayed/invalid advanced device proof;
- current watchdog readiness and non-mutating inspection;
- exact transition lease, watchdog acceptance and every rejection;
- nonrenewable lease recovery-margin inequality;
- write-ahead failure before action;
- every post-mutation journal append/flush failure and durable recovery;
- monotonic checksum chain and advanced-proof-only state commit;
- restart pending/corrupt/missing state and cycle inhibition;
- timer generation, overdue scheduling, withdrawal and DST instants;
- action failure, unsafe result, timeout and repeated cancellation;
- heartbeat continuing while action runs;
- guard-first shutdown/unload/reload and no leaked callback/task;
- T0009 lease/fail-safe interaction; and
- absence of legacy stub writes and live access.

Run T0018 scenarios plus the complete house-battery and deployment suites.

## Acceptance criteria

- Dynamic writes remain impossible by default.
- Every action is revalidated against all current sources at the actuator
  boundary.
- No valid keep decision causes a repeated slot rewrite.
- HA readback never advances device-proof state.
- Watchdog transition tolerance is narrow, fingerprinted and independently
  recoverable before every deadline.
- Crash windows cannot lose consumed cycle or mutation history.
- Any post-mutation persistence failure immediately enters durable fail-safe
  recovery.
- Heartbeat remains observable during action, failure and cleanup.
- Shutdown asserts the guard first and reports conclusive safe/unsafe state.
- Focused, simulation and full local tests pass.
- The implementation commit changes this card's status to `Implemented` while
  deployment remains disabled.

## Implementation ownership

- Branch: `codex/T0019-guarded-dynamic-coordinator-cutover`.
- Isolated worktree.
- Small-model implementation agent after T0018 is integrated.
- Coordinator/config hotspot: serialize with T0008/T0010/T0011 changes.
