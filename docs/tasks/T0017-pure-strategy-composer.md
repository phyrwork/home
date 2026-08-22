# T0017 — Compose battery strategies into one desired action

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0013 — commissioned reserve planner;
- T0014 — pre-discharge headroom strategy;
- T0015 — full-SOC cheap-window cycling;
- T0016 — guarded dynamic-slot commissioning evidence.

## Objective

Add a pure, deterministic composer that selects at most one persistent-policy or
slot-control action from trusted reserve, cheap-charge, pre-discharge and full-SOC
cycling decisions.

The composer is an orchestration state machine only. It does not read Home
Assistant, call Solis, hold commissioning authority, schedule callbacks or mutate
the coordinator.

## Complete immutable input

Consume a `CompositeSnapshot` containing:

- aware current time;
- exact controller health and control-disable guard state;
- fresh T0004 SOC, stored energy and device-source revision;
- trusted T0012 import, export and dispatch views;
- a complete T0013 reserve trajectory;
- current T0014 decision and T0015 cycle state/result;
- current T0016 `ProductionSlotAuthority`, fixed direction operating points and
  `ControlTimingBudget`;
- an exact authoritative `AppliedStateProof`;
- versioned prior `ComposerState`;
- complete aligned load and external-PV/negative-load forecasts;
- battery capacity, minimum/protected floor and charge/discharge efficiencies;
- fresh runtime voltage and temperature conditions; and
- every relevant evidence expiry and fingerprint.

Any missing, stale, future, invalid or mismatched safety input fails closed. If an
intent is active, the result first requires stop and all-off proof. Otherwise the
result requires fail-safe policy convergence and emits no dynamic intent.

## Forecast and battery model input

Add immutable `ForecastModelInput` containing:

- ordered, contiguous load and external-PV/negative-load intervals covering every
  candidate, continuation and refill horizon exactly;
- battery capacity, minimum energy and protected floor;
- charge and discharge efficiencies;
- source IDs, retrieval/revision timestamps and `fresh_until`; and
- deterministic source-generation and content fingerprints.

Values use exact finite `Decimal` energy and efficiencies. Gaps, overlap, stale
sources, invalid values or mismatched horizons fail closed.

The audit and stable fingerprints both bind material forecast values and the
source generation. A compatible freshness-only revalidation may preserve stable
intent identity; changed values or generation may not.

## Runtime operating conditions

For battery voltage and every retained battery/device-temperature dimension in a
selected T0016 operating point, require an immutable
`RuntimeConditionObservation` containing:

- exact `Decimal` value and unit;
- authoritative source and device revision;
- aware observation time and `fresh_until`; and
- mapping/evidence fingerprint.

Admission and every continuation require inclusive operating-envelope validation.
Missing, stale, future, source-mismatched or out-of-envelope conditions require
immediate stop of an active intent followed by all-off cleanup.

## Versioned composer state

Persist a versioned immutable `ComposerState` containing:

- recovery or strategy phase;
- active owner, direction and physical slot;
- exact applied stable-intent fingerprint, interval and expiry;
- source-strategy decision or cycle identity;
- consumed evidence revisions;
- last reconciled advanced-device revision;
- policy convergence target/fingerprint; and
- minimum-dwell evidence.

State carries no actuation authorization. Missing, malformed, unknown-version or
fingerprint-mismatched state is a restart condition and enters the sequential
recovery protocol below.

## Audit and continuation fingerprints

Produce two canonical SHA-256 fingerprints.

The audit fingerprint includes current time, every observation/retrieval/device
revision, every material input and the complete result.

The stable continuation fingerprint excludes only poll time and freshness-only
revision changes that are still valid and semantically identical. It includes:

- owner, direction, physical slot and exact interval/expiry;
- strategy/window/cycle identity;
- target energy, SOC and fixed current;
- policy, map and every production-authority fingerprint;
- operating-point and timing fingerprints;
- material forecast generation and values; and
- semantic trusted tariff and dispatch content.

For Intelligent dispatch, semantic content is the exact ordered tuple of adjusted
interval start, end, classification and import price plus coverage horizon/status
and current dispatch interval identity. Retrieval/device revisions may change
without changing stable identity only when a new T0012 evaluation is fresh,
source IDs are unchanged and that complete semantic tuple is byte-identical.

Withdrawal, shortening, repricing, reclassification or another material change
changes stable identity and requires stop.

## Exact applied-state proof

`AppliedStateProof` contains:

- mapping, policy and stable-intent fingerprints;
- exact enable and configuration state for all 12 slot directions;
- relevant storage mode, permissions, Peak Shaving, reserve, protections and
  global capability values;
- exact guard state;
- Home Assistant revision/readback evidence;
- advanced Solis Cloud/device source revision and timestamp after the relevant
  mutation; and
- explicit fresh device/cloud reconciliation flags.

Home Assistant readback alone is never authoritative. A continuing intent must be
the sole exact active slot in this proof.

## Sequential restart and policy recovery

Restart or invalid state follows distinct phases:

1. `STOP_REQUIRED` if any slot may be active;
2. wait for advanced all-slots-off plus exact fail-safe proof:
   `Self-Use`, Peak Shaving on and Battery Reserve off;
3. request candidate/reserve policy application with no slot;
4. wait for a later advanced proof of Feed-In Priority, permissions, protections,
   dynamic reserve and capability settings; and
5. only then admit a strategy.

Fail-safe and candidate proof are never treated as simultaneous because their
persistent states intentionally differ.

Any reserve-target or candidate-policy change uses the same sequence: stop,
advanced all-off proof, policy application, advanced exact policy proof, then a
later slot decision. A result never contains both an unresolved policy mutation
and a slot intent.

## Deterministic precedence

Evaluate in this order:

1. unhealthy controller, asserted/invalid guard, invalid production authority,
   unsafe applied proof or restart recovery;
2. continue or abort every mandatory T0015 cycle obligation;
3. stop an expired, withdrawn, target-achieved or materially changed active
   intent;
4. enter `FINAL_CHARGE` at its conservative cutoff and immediately stop
   discretionary discharge;
5. charge during an active trusted standard or fresh bonus window when not full;
6. admit or continue T0015 cycling only when authoritatively full and final refill
   remains feasible;
7. apply T0014 pre-discharge only before its trusted standard window; and
8. remain healthy idle with candidate policy converged and all slots off.

Required stop, proof or recovery obligations always outrank a new strategy. Do
not rely on overlapping schedule priority.

## T0015 cycle ownership

Map T0015 `ALLOW_RECHARGE` only to a cycle-owned charge intent carrying the exact
cycle ID and next immutable `CycleState` fingerprint. It outranks generic cheap
charging and never becomes an ordinary charge phase.

Every composer result involving a T0015 phase carries zero or one exact
`next_cycle_state`. Generic charging is unavailable while any nonterminal,
`ABORTED_INCOMPLETE` or restart-inhibited cycle state exists.

T0015's stop/off proof, later-full-revision, completion and repeat-admission gates
remain authoritative through recharge and completion.

## Intent continuation and idempotency

The composer never blindly re-emits a slot on every heartbeat.

A final-charge, cheap-charge, pre-discharge or cycle intent returns
`KEEP_ACTIVE_NO_WRITE` only when:

- stable identity is unchanged;
- all refreshed evidence remains valid;
- exact applied proof shows it as the sole active slot;
- runtime conditions remain inside the operating envelope;
- reserve and strategy constraints still pass;
- current time remains before end, expiry and `fresh_until`;
- its target has not been achieved; and
- no final cutoff, withdrawal or higher-priority obligation applies.

Otherwise it returns `STOP_REQUIRED`. A new `APPLY_INTENT` is possible only after
advanced all-off and policy proof.

## Slot mapping and quantization

Return exactly zero or one already-active T0006-compatible `SlotIntent`:

```text
CHEAP_CHARGING -> charge, physical slot 1
FULL_SOC_CYCLING -> discharge, physical slot 1
PRE_DISCHARGE -> discharge, physical slot 2
```

Use only the exact fixed T0016 current for that direction. Never convert AC kW to
current or choose another point from generic metadata.

Charge target SOC is `FULL_SOC_PERCENT`. A discharge target rounds upward to the
exact fresh commissioned SOC step and must remain at or above the dynamic reserve
and `MINIMUM_SOC_PERCENT`. Reject zero or unrepresentable values.

After minute, current or SOC quantization, rerun the complete source-strategy
energy, reserve, economics, duration and final-charge simulation using:

- complete aligned forecasts;
- T0013 reserve trajectory;
- T0016 conservative minimum and maximum power bounds;
- exact efficiencies; and
- current timing and freshness evidence.

Direction or owner changes require advanced all-off proof. Every intent satisfies
T0006's active, minute, DST and expiry boundaries.

## Exact timing inequalities

For every admission and continuation, simulate sequentially:

- required T0016 direction-change or apply budget;
- effective energy time at the conservative minimum AC power after shared
  baseline load/PV, efficiency and quantization;
- advanced reconciliation;
- stop; and
- complete cleanup/all-off proof.

Require strictly:

```text
cleanup_complete_at
< min(window_end, intent_expiry, every_relevant_source_fresh_until)
```

A direction change includes the complete T0016 `DIRECTION_CHANGE_BUDGET`.
Continuation of the same applied direction still reserves remaining stop and
cleanup time.

Enter final charge when:

```text
remaining_trusted_time
<= direction_change_or_apply_budget
   + worst_case_remaining_refill_duration
   + device_reconciliation
   + stop
   + cleanup
```

Equality forbids discretionary discharge. Re-evaluate the exact inequalities
after quantization and on every poll. Without strict cleanup margin, stop or make
no admission.

## Infeasible final charge

Final-charge feasibility uses actual remaining stored deficit to full, shared AC
charge capacity after baseline flow, charge efficiency, T0016 conservative
minimum charge power, minute representation and every transition budget.

If full is infeasible, immediately stop discretionary discharge and report the
shortfall honestly. When trusted cheap import and a safe commissioned charge
intent remain available, return a best-effort `FINAL_CHARGE` intent with
`INFEASIBLE_FINAL_CHARGE_BEST_EFFORT`. Otherwise require cleanup only with
`INFEASIBLE_FINAL_CHARGE_NO_ACTION`. Never label either path full-feasible.

## Dwell and freshness

Derive the named minimum dwell from commissioned reconciliation and telemetry
evidence. It may suppress only optional new admissions.

Dwell never delays:

- fail-safe or guard response;
- dispatch/window withdrawal;
- reserve protection;
- expiry;
- required stop or off proof;
- policy convergence; or
- final-charge cutoff.

Set `fresh_until` to the earliest intent, strategy, evidence or policy deadline.

## Pure result contract

Separate a high-level result status from a mutation obligation.

Statuses distinguish at least:

- `STOP_REQUIRED`;
- `POLICY_CONVERGENCE`;
- `KEEP_ACTIVE`;
- `APPLY_INTENT`;
- `IDLE`;
- `FAIL_SAFE`;
- `INFEASIBLE_FINAL_CHARGE_BEST_EFFORT`;
- `INFEASIBLE_FINAL_CHARGE_NO_ACTION`;
- `UNAVAILABLE`; and
- `INVALID`.

Mutation obligations distinguish at least:

- `STOP_REQUIRED`;
- `WAIT_FOR_OFF_PROOF`;
- `APPLY_FAIL_SAFE_POLICY`;
- `WAIT_FOR_FAIL_SAFE_PROOF`;
- `APPLY_CANDIDATE_POLICY`;
- `WAIT_FOR_CANDIDATE_PROOF`;
- `APPLY_INTENT`;
- `KEEP_ACTIVE_NO_WRITE`; and
- `IDLE_NO_WRITE`.

The result contains persistent-policy obligation, cleanup/reconciliation
obligation, zero or one slot, zero or one next cycle state, next composer state,
audit and stable fingerprints, `fresh_until`, rejected candidates and stable
diagnostics. A coordinator must never infer mutation semantics from a nullable
slot.

## Compatibility boundaries

- Do not read or write Home Assistant or Solis.
- Do not create commissioning authority.
- Do not change coordinator, sensor or YAML.
- Preserve existing strategy and simulation entry points until later cutover.
- Do not deploy, authenticate or access live services.

## Tests

Use deterministic pure tests to cover at least:

- exhaustive pairwise precedence and mutual exclusion;
- restart and every fail-safe/candidate recovery phase;
- policy target changes and advanced reconciliation proof;
- exact applied-state proof and HA-only rejection;
- audit versus stable fingerprint mutation;
- semantically identical versus changed Intelligent dispatch retrieval;
- generic and cycle `KEEP_ACTIVE_NO_WRITE` behavior;
- expiry, target achieved, material change and withdrawal stop;
- every T0015 phase, cycle-owned recharge and next cycle state;
- aborted/restart-inhibited cycle blocking generic charge;
- standard versus bonus charging and dispatch freshness;
- pre-discharge standard-only behavior;
- final-cutoff equality and strict cleanup margin;
- feasible and both infeasible-final-charge outcomes;
- complete shared load/PV forecast simulation and every gap/staleness;
- capacity, floor and asymmetric efficiency;
- voltage/temperature source, freshness and envelope checks;
- fixed operating point minimum/maximum power semantics;
- upward discharge-SOC and minute quantization with full recalculation;
- active, expiry, cross-midnight and DST T0006 compatibility;
- direction-change and all-off proof;
- dwell suppression and every safety exception;
- every authority/fingerprint/freshness mutation; and
- absence of Home Assistant imports, writes or deployment changes.

Run the complete house-battery and deployment suites.

## Acceptance criteria

- At most one dynamic direction is representable.
- Policy convergence and all-off device proof always precede a new slot.
- Compatible intents remain idempotent without repeated slot rewrites.
- Material trust or plan changes stop active control immediately.
- Cycling ownership and later-full-revision gates survive recharge.
- Quantization always triggers a complete physical and economic recalculation.
- All actions retain strict end-to-end cleanup margin.
- The composer remains pure, deterministic and audit-fingerprint complete.
- Focused and full local tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0017-pure-strategy-composer`.
- Isolated worktree.
- Small-model implementation agent after T0014-T0016 are integrated.
- Prefer new pure modules; serialize any shared contract changes with T0015.
