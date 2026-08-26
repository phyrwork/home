# T0047 — Simplify the scheduler and investigate paired native cycling

Status: Accepted — audit complete; live capability test pending

Depends on: T0039, T0040, T0041, T0042, T0043, T0046

## Objective

Reduce the house-battery scheduler to the smallest coherent model that retains
only reliability and safety behaviour justified by requirements or live
evidence. Before choosing the full-SOC-cycle implementation, prove whether the
inverter can execute one pre-armed, adjacent discharge/recharge schedule without
a cloud write at the phase boundary.

This task is a decision gate. Do not implement a composite scheduler before the
native capability is physically proven.

## Current evidence

The overnight full-SOC observation proved that four sequential cycles discharged
and recharged correctly. One later cycle performed two consecutive discharges.
At the first discharge deadline, the latest valid Solis device observation was
still the sample that reported 100% before discharge. The planner returned
through generic `STOPPING` to `IDLE`, reused that stale 100% observation, and
authorized another discharge instead of recharge.

This proves:

- native charge and discharge slots work independently;
- the working charge controls are Feed-In Priority, Allow Grid Charging on and
  Grid Peak Shaving off;
- controller reconciliation successfully proves one direction off before
  enabling the other;
- stale device telemetry, rather than Solis charge configuration, caused the
  missing recharge; and
- simultaneously enabled adjacent charge/discharge schedules have not yet been
  physically proven.

The local baseline is green: 144 house-battery component tests pass.

## Retain

Keep the following evidence-backed boundaries:

- one event-driven controller and one serialized Solis writer;
- one control change with revision/readback proof per reconciliation pass;
- bounded best-effort start retries;
- unbounded important-stop retries with fail-safe escalation after 15 minutes;
- recoverable planning, tariff and telemetry degradation;
- trusted tariff and dispatch provenance;
- the bounded bonus-dispatch lease;
- half-open UTC intents and commissioned native local-time encoding;
- split-midnight support and the accepted `23:59-00:00` gap;
- mode-only Self-Use fail-safe and crash sentinel;
- strict shutdown: prove Self-Use, then disable every observed-on direction; and
- the one-minute reconciliation backstop and heartbeat.

Do not weaken absolute minimum-SOC, target-SOC, tariff-end, lease-expiry or
conflict stops.

## Scheduler simplification

### Cycle state only

Replace the generic `CycleState` workflow with one transient, RAM-only cycle
phase:

```text
CyclePhase.NONE
CyclePhase.DISCHARGING
CyclePhase.RECHARGING
```

Ordinary cheap charging, reserve discharge and reserve following are stateless
desired states. Remove generic `CHARGING`, `RESERVE_DISCHARGING` and `STOPPING`.
The reconciler already stops conflicting native directions and proves them off
before starting a replacement; planner-level stop transitions duplicate that
behaviour and lose transition provenance.

No phase state is persisted or reconstructed after restart. Fresh setup derives
behaviour from current trusted inputs and authoritative Solis readback.

### Explicit desired control state

The plan must explicitly contain:

- the observable `StrategyAction`;
- zero or more narrowly supported logical schedule segments;
- desired Grid Peak Shaving enable state;
- cycle phase, phase deadline and device-observation gate when cycling; and
- existing reserve and diagnostic results.

The controller must not infer persistent policy from scattered action checks.
Normal reconciliation is:

```text
observe
→ create unconditional safety-stop debt
→ calculate desired state
→ stop conflicts in the required order
→ apply one missing desired control
→ prove the complete desired state
→ publish
```

### Destination-aware Peak Shaving handover

Use only these transition rules:

- an absolute safety, target or expiry stop disables the direction immediately;
- forced slot to reserve following: enable/prove Peak Shaving, then stop the
  forced slot;
- forced slot to another forced slot: keep Peak Shaving off, stop the conflict,
  then start the replacement;
- reserve following to a forced slot: prepare and arm the bounded slot, then
  disable/prove Peak Shaving; and
- if Peak Shaving is already off, do not turn it on merely to prepare another
  forced action.

Do not allow a Peak Shaving write failure to block an important stop.

### Remove redundant machinery

Subject to focused regression tests, remove:

- the duplicate consecutive `_retire_proven_stops()` call;
- unused `StopDebt.first_seen` storage while retaining its fixed fail-safe
  deadline;
- `_awaiting_off_proof`, whose state is already represented by stop debt and
  authoritative readback;
- used-slot time/current housekeeping and `_used_slots`;
- strategy-specific `preserve_standard_cheap_slot` plumbing; and
- compatibility branches that serve no commissioned runtime.

An off enable switch is sufficient. Do not spend Solis writes resetting the
time or current of a confirmed-off direction.

When an enabled allocated native direction already exactly matches a desired
segment, the adapter should reuse it generically instead of forcing the first
canonical allocation. Preserve the existing exact owner, direction, value,
schedule and active-range checks.

## Accepted action vocabulary

```text
CHEAP_CHARGE       ordinary tariff-driven charging
CYCLE_DISCHARGE    full-SOC cycle discharge phase
CYCLE_RECHARGE     full-SOC cycle recharge phase
RESERVE_DISCHARGE  export toward the dynamic reserve
RESERVE_FOLLOW     ordinary house-load following at reserve
IDLE               no active forced or reserve-follow action
```

Do not use `RESERVE_CHARGE`: cycle and cheap charging target 100%, not the
dynamic reserve.

## Candidate paired native cycle

For the configured cycle duty duration `d`, initially 10 minutes, the candidate
block is:

```text
discharge  [D0, D1)  where D1 = D0 + d
recharge   [D1, D2)  where D2 = D1 + d
```

Both directions use their observed supported maximum current. Discharge targets
the current quantized reserve and recharge targets 100%.

The block is atomic only as desired configuration; Solis API writes remain
sequential. Stage it before `D0` in this order:

1. stop and prove conflicting directions off;
2. configure both future schedules while disabled;
3. prove Feed-In Priority, Allow Grid Charging, Battery Reserve/target and Grid
   Peak Shaving off;
4. enable and prove the recharge schedule first;
5. enable the discharge schedule last as the commit point; and
6. prove the complete, adjacent, non-overlapping block before `D0`.

If the complete block is not proven before `D0`, discharge must remain or become
off. A partial future recharge-to-100 schedule is preferable to an unpaired
discharge.

The controller performs no service call at `D1`; the inverter clock performs the
handover.

### Repeat gate

Capture the Solis `device_timestamp` observation used to authorize the block.
Do not compare Home Assistant `last_updated` values.

After the block:

- a strictly newer device observation reporting 100% may authorize another
  block if the complete eligibility window remains;
- a strictly newer observation below 100% selects ordinary `CHEAP_CHARGE`;
- an unchanged observation cannot authorize another discharge; and
- absence of a newer observation prevents another block but does not by itself
  create fail-safe state.

Every encoded charge and discharge boundary remains inside trusted cheap
authority. Tariff or lease end always wins over completing or repeating a
cycle.

## Live capability experiment

Run the first experiment only during a trusted static cheap period, within one
inverter-local day and comfortably away from midnight. Do not test a bonus
dispatch or cross-midnight block in this acceptance.

Before mutation:

- stop the HA battery controller so it cannot write;
- snapshot storage mode, persistent controls and all 12 direction controls;
- prove Feed-In Priority, Allow Grid Charging on and Grid Peak Shaving off; and
- prove every unrelated direction off.

Configure one 10-minute discharge followed by one exactly adjacent 10-minute
recharge. Enable both directions before discharge begins. Capture:

- exact control readback for enable, time, current and target SOC;
- inverter/device timestamp, SOC and battery power;
- Octopus whole-site current demand; and
- timestamps before `D0`, during each phase, across `D1`, and after `D2`.

Acceptance requires:

1. both directions remain enabled together with non-overlapping schedules;
2. discharge/export occurs at approximately full supported power in `[D0,D1)`;
3. recharge/import occurs at approximately full supported power in `[D1,D2)`;
4. no material idle interval or simultaneous physical charge/discharge occurs at
   `D1`;
5. no cloud or Home Assistant write is needed at `D1`;
6. both physical actions stop at their encoded boundaries; and
7. a newer Solis device observation is captured for repeat gating.

After evidence capture, disable both directions, prove them off, reset any
experiment-only settings as required, and restore the controller.

Record exact settings, timestamps, readbacks and physical outcomes in the
commissioning log.

## Decision gate

### If paired execution is proven

Replace the current single-direction `LogicalIntent` wrapper with a small
desired schedule supporting only:

- zero logical segments;
- one ordinary charge/discharge segment; or
- exactly two ordered cycle segments: `CYCLE_DISCHARGE` then
  `CYCLE_RECHARGE`.

The Solis adapter may split an individual segment at local midnight using the
existing two-slot allocations. Do not build an arbitrary schedule graph,
workflow engine, transaction journal or persistent cycle record.

Add a bounded arming phase and hard `D0` abort. Recharge is enabled first and
discharge last. Initially permit paired cycles only in static cheap authority and
without a local-midnight crossing.

### If paired execution is rejected or ambiguous

Keep the existing single-direction intent model and implement sequential
`CYCLE_DISCHARGE → CYCLE_RECHARGE` using authoritative off proof. Arm recharge
optimistically despite an unchanged SOC value, retain the cycle phase through
fast control events, and require a strictly newer Solis device observation
before another discharge.

## Explicit exclusions

Do not add:

- durable cycle state or restart journals;
- immutable commissioning records or authority objects;
- a config-entry conversion or reload service;
- a generic multi-action scheduling language;
- an API timing predictor;
- additional guard, watchdog or health entities;
- blanket all-slot normalization during ordinary operation;
- retries for a start after its unchanged bounded retry generation is
  suppressed; or
- live changes before the experiment is explicitly commissioned.

## Documentation completion

After the decision gate, update one concise authoritative scheduler contract and
the commissioning log. Historical task cards remain evidence, not current
requirements. Resolve current contradictions about Peak Shaving ownership and
cycle transitions in the active top-level documentation.

## Local acceptance for implementation

Whichever implementation path is selected must prove:

- ordinary cheap charge, reserve discharge and reserve follow require no generic
  transition state;
- every conflict is off-proven before an incompatible start;
- cycle discharge always has a bounded recharge outcome or an explicit abort;
- unchanged device telemetry cannot authorize consecutive cycle discharges;
- newer below-100 and newer 100% observations take the documented paths;
- tariff/lease boundaries stop charge and discharge independently of telemetry;
- Peak Shaving handovers follow the destination-aware ordering;
- start failures remain best effort and important stops remain unbounded;
- fail-safe, sentinel and shutdown semantics remain unchanged;
- native schedules never overlap under half-open interpretation; and
- the full component and deployment suites pass.

