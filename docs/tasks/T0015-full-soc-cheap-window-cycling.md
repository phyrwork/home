# T0015 — Cycle at full SOC during trusted cheap windows

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0012 — trusted Octopus cheap-window model;
- T0013 — commissioned reserve planner.

## Objective

Add a pure, recoverable state machine for the user's first headroom mechanism:
while the battery is authoritatively full during a trusted cheap window, create a
short discharge opportunity and then refill the energy before the trusted window
ends.

The result expresses phase and cleanup obligations only. It does not emit a
`SlotIntent`, call Home Assistant, write Solis controls, authorize actuation or
change the coordinator.

## Eligibility and trust

A new cycle may be considered only inside:

- a complete trusted `STANDARD_CHEAP` window; or
- a current `BONUS_DISPATCH` window with independently fresh dispatch evidence.

Require all of:

- an exact, fresh, authoritative `FULL_SOC` stored-energy/SOC observation with a
  concrete monotonically comparable device-source revision;
- a matching complete T0013 commissioned AC power envelope;
- a fingerprint-bound commissioned control-timing budget;
- complete aligned household-load and external-PV/negative-load forecasts;
- a complete feasible T0013 reserve trajectory;
- trusted import coverage across proposed refill;
- trusted export coverage across proposed discharge; and
- matching mapping, policy, commissioning, forecast, price, reserve and source
  fingerprints.

Any gap, invalid value, stale evidence or mismatch prevents a new cycle. When a
cycle already exists, it creates an explicit abort or cleanup obligation rather
than silently returning no decision.

## Immutable cycle state

Define a versioned immutable serialized `CycleState` containing at least:

- schema version and unique cycle ID;
- phase;
- consumed full-observation source and revision;
- planned, realized and remaining stored withdrawal;
- encoded discharge and recharge boundaries and phase deadlines;
- planned withdrawal rationale;
- creation time and `fresh_until`;
- mapping, policy and commissioned-power fingerprints;
- control-timing evidence fingerprint;
- import, export, dispatch, forecast and reserve source fingerprints; and
- the complete decision fingerprint.

Phases distinguish at least:

- `READY`;
- `DISCHARGING`;
- `WAITING_FOR_DISCHARGE_OFF_PROOF`;
- `RECHARGING`;
- `WAITING_FOR_ALL_OFF_PROOF`;
- `COMPLETE`;
- `ABORTING`; and
- `ABORTED_INCOMPLETE`.

Persisted state carries no commissioning authorization and cannot itself
authorize a Solis write. Missing, malformed, unknown-version or
fingerprint-mismatched state after restart fails closed and requires
authoritative all-slots-off proof before returning to `READY`.

## Baseline and candidate simulation

Authorize a cycle only after exact interval simulation against a no-cycle
baseline.

At every interval boundary:

- ordinary grid contribution is `MAXIMUM_GRID_IMPORT_POWER_KW`;
- baseline household and external-PV flow consume the shared commissioned AC
  discharge or charge capacity according to the T0013 physical model;
- discretionary export uses only remaining commissioned AC discharge output;
- stored energy remains at or above the dynamic reserve trajectory and at or
  below capacity;
- charge and discharge efficiencies are applied exactly once at their documented
  AC boundaries; and
- refill uses the actual planned stored withdrawal, remaining shared AC charge
  input and complete import intervals.

Use exact `Decimal` energy and UTC elapsed duration for complete and partial
intervals. Infeasible baseline load, reserve, capacity, discharge or refill
returns no cycle authorization.

## Conservative economics

Compare the cycle with the simulated no-cycle baseline. Authorize only from
incremental AC export value minus actual refill import cost and
`BATTERY_CYCLE_COST_PER_KWH`.

Avoided household import is diagnostic only unless a separate commissioned,
fresh and fingerprint-bound counterfactual proves that value is incremental
under Feed-In Priority and Peak Shaving.

Use actual discharge-time export and refill-time import components. Both the
worst-case relevant margin and total cycle value must be strictly greater than
zero.

## Ten-minute maximum and control timing

Define:

```python
MAXIMUM_FULL_SOC_DISCHARGE_SLOT_DURATION = timedelta(minutes=10)
```

This is the maximum encoded discharge-slot span, not a promise of ten minutes of
delivered discharge.

Consume an immutable commissioned `ControlTimingBudget` bound to current
mapping, policy, inverter and evidence fingerprints. Its conservative bound must
cover:

- every disable, configure and enable transaction;
- authoritative device reconciliation;
- direction-change proof;
- telemetry latency; and
- final cleanup.

Local Home Assistant timeout constants alone are not commissioning evidence for
Solis Cloud or inverter transition time.

T0006 accepts only already-active intents. Quantize an active local interval to
the exact Solis minute representation, cap its wall-clock span at the named ten
minutes and reject ambiguous/nonexistent wall times or any DST transition.
After quantization and current activation delay, recompute conservative remaining
effective on-time, shared power, stored withdrawal, reserve, economics, refill
and final-cleanup feasibility. Never infer delivered energy from wall-clock slot
span alone.

## Transition safety

Before every transition, revalidate:

- aware current time;
- fresh authoritative SOC and stored energy;
- trusted current rates and dispatch evidence;
- complete forecasts and reserve trajectory;
- current cycle state and phase deadline; and
- every authority and source fingerprint.

Set `fresh_until` to the earliest relevant evidence expiry or phase deadline.

Leaving `DISCHARGING` always first emits a `STOP_DISCHARGE` obligation. Recharging
may be allowed only after authoritative device proof that discharge is off.

Loss of trust, telemetry, state, fingerprints or a deadline enters `ABORTING`,
emits an all-slots-off proof obligation and authorizes no new action. A stale or
withdrawn bonus dispatch stops discharge and cannot force recharge at an
untrusted price. Standard-cheap recharge may continue only while its complete
import evidence remains trusted.

Reaching the cheap-window deadline below a fresh full observation is
`ABORTED_INCOMPLETE`, never `COMPLETE`.

## Completion and repeat gating

`COMPLETE` requires both:

- authoritative device proof that all slot directions are off; and
- a fresh exact full observation with a device-source revision strictly later
  than the revision consumed to start the cycle.

A later cycle requires another strictly later full-observation revision.
Intermediate SOC noise, duplicate observations, restart or a prior deadline does
not reopen the gate. `ABORTED_INCOMPLETE` remains blocked until all-off proof and
a later full revision are available.

## Pure result contract

Return an immutable result containing:

- phase and stable status/issues;
- zero or one next obligation such as `NONE`, `STOP_DISCHARGE`,
  `PROVE_ALL_OFF` or `ALLOW_RECHARGE`;
- phase boundaries and deadlines;
- planned/effective AC and stored energy;
- reserve proof and economic diagnostics;
- complete input fingerprint; and
- `fresh_until`.

Obligations are domain vocabulary, not entity IDs, service calls, write requests
or actuation authorization.

## Compatibility boundaries

- Do not emit `SlotIntent` or call T0006/T0007.
- Do not change coordinator, controller, sensor, Solis adapters or YAML.
- Do not authenticate, deploy or access live Home Assistant.
- Do not implement pre-discharge; that belongs to T0014.

## Tests

Use deterministic pure tests to cover at least:

- standard and fresh-bonus eligibility and every missing trust source;
- fresh, stale, future, duplicate and non-monotonic full observations;
- versioned state serialization and restart recovery;
- complete decision-fingerprint mutation;
- interval-by-interval baseline and candidate simulation;
- shared load/PV charge and discharge capacity;
- exact ordinary 0.1 kW grid contribution;
- reserve preservation at every boundary;
- asymmetric power and efficiency;
- exact planned withdrawal and refill;
- positive, zero and negative incremental economics;
- diagnostic-only household-import value;
- ten-minute maximum, minute quantization and activation delay;
- commissioned control-timing evidence and every mismatch;
- cross-midnight, partial intervals and DST rejection;
- every state transition and cleanup obligation;
- guard loss, stale telemetry and changed fingerprint during each phase;
- stale or withdrawn bonus dispatch;
- authoritative discharge-off and all-off proof gates;
- deadline below full producing `ABORTED_INCOMPLETE`; and
- strictly later full-revision completion and repeat gating.

Run the complete house-battery and deployment test suites.

## Acceptance criteria

- A cycle is never authorized from incomplete trust or uncommissioned timing.
- The simulation preserves shared physical limits and dynamic reserve everywhere.
- Economics includes only conservative incremental value.
- Ten minutes is an encoded maximum, not fictional delivered energy.
- Discharge is proven off before recharge can begin.
- Every uncertainty produces an explicit fail-closed cleanup obligation.
- Restart and duplicate observations cannot create a second cycle.
- The slice remains pure and locally testable.
- Focused and full local tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0015-full-soc-cheap-window-cycling`.
- Isolated worktree.
- Small-model implementation agent after T0012 and T0013 are integrated.
- New strategy/state files only; serialize with T0014 if their shared pure domain
  boundary would otherwise conflict.
