# T0047 — Simplify the scheduler and investigate paired native cycling

Status: Rolling paired cycle implemented locally — broader simplification, deployment and live acceptance pending

Depends on: T0039, T0040, T0041, T0042, T0043, T0046

## Objective

Reduce the house-battery scheduler to the smallest coherent model that retains
only reliability and safety behaviour justified by requirements or live
evidence. Full-SOC cycling is represented as one rolling pair of adjacent native
discharge/recharge phases so the next phase is always pre-armed before the
inverter clock reaches the boundary.

## Current evidence

### Earlier cycling evidence

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

### 2026-08-28 sequential recharge failure

Fresh overnight history disproves the broader conclusion that stale telemetry
was the only possible cause of a missing recharge:

- static charging on native charge slot 2 reached 100% SOC at 02:29 UTC;
- cycle discharge slot 1 ran from 03:29 to 03:39 local time and physically
  exported approximately 4.4 kW;
- the controller then stopped and proved that discharge direction off;
- fresh Solis device samples reported 99% during discharge and 98% immediately
  after it, so the recharge decision was not based on the original 100% sample;
- at 02:39 UTC the controller enabled charge slot 1 at 100 A and target 100%,
  with Feed-In Priority, Allow Grid Charging on and Grid Peak Shaving off; but
- the charge schedule was the original standard-cheap interval `00:00-05:30`,
  whose start boundary was already 3 hours 39 minutes in the past. Whole-site
  demand remained approximately 250 W and battery power remained around zero
  until cheap authority ended, proving that charging was physically
  ineffective.

The working standard slot was armed before its native start boundary. The
separate post-midnight recovery evidence armed at the 00:00 boundary itself;
it does not prove that Solis will start a newly enabled timer several hours
inside its encoded interval. Treat an already-past native start as unsuitable
for a newly armed cycle recharge even though retaining that stable start is
correct for an already-enabled ordinary standard-cheap slot.

Therefore a sequential implementation must preserve explicit cycle provenance:
after cycle discharge it selects `CYCLE_RECHARGE`, with a fresh actionable
minute boundary inside the remaining cheap interval. It must not pass through
ordinary `CHEAP_CHARGE` planning and reuse the historical standard-phase start.
This is also the fallback required if the paired native experiment remains
ambiguous.

### Sequential fallback implementation

The 2026-08-28 fix implements the previously accepted `CYCLE_RECHARGE` action
without adding persistent state or a general schedule engine:

- cycle discharge captures the authoritative Solis device timestamp that
  permitted it;
- after discharge, recharge receives a fresh minute boundary with a two-minute
  arming lead instead of inheriting the historical standard-cheap start;
- the desired recharge interval is fixed while the controller proves discharge
  off and reconciles the charge controls;
- the pre-discharge 100% sample cannot cancel recharge or authorize another
  discharge;
- only a strictly newer device observation at 100% permits the next cycle; and
- a bounded recharge that still reports below 100% rolls to another fresh
  bounded recharge interval while cheap authority remains.

The local baseline is green: 152 house-battery component tests pass. Regression
coverage now executes the complete discharge-to-recharge transition, exact
fresh/stable recharge timing, controller latching across conflict stop, stale
100% handling and repeat authorization from a newer device observation.

Deployment `bbde141` completed on 2026-08-28 with Home Assistant configuration
validation and restart successful. The deployed source contains the explicit
`CYCLE_RECHARGE` action and two-minute arming lead. Solis discovery initially
returned no inverter and the controller correctly remained `degraded` with a
fresh heartbeat. Telemetry then recovered passively, after which controller
health returned to `healthy` and normal `RESERVE_DISCHARGE` planning resumed.
Live acceptance still requires observing a complete 100%-SOC discharge/recharge
cycle during a cheap period.

### 2026-08-31 sequential failure and recovered experiment result

The next overnight run again proved the discharge half but not recharge:

- static cheap charging reached 100% SOC;
- cycle discharge slot 1 ran for 10 minutes at approximately 4.4 kW and reduced
  SOC to 98%; and
- the controller then repeatedly armed fresh 10-minute recharge intervals with
  Feed-In Priority, Allow Grid Charging on and Grid Peak Shaving off, but none
  produced material grid charging.

The earlier paired capability experiment did not reject paired operation. Its
runner exited before collecting a sample because an unset shell variable
(`label`) was referenced; the retained sample file is empty. The experiment
therefore supplied no physical evidence either way.

Solis documents timers as continuously effective while the inverter time is
inside the configured interval. Combined with the repeated sequential failure,
the accepted implementation is now the narrow rolling pair below. Local tests
are authoritative for scheduling and reconciliation; deployment must still be
followed by one live physical cycle.

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
RESERVE_FOLLOW     house-load following through planned reserve to the safety floor
IDLE               no active forced or reserve-follow action
```

Do not use `RESERVE_CHARGE`: cycle and cheap charging target 100%, not the
dynamic reserve.

## Accepted rolling paired native cycle

For the configured cycle duty duration `d`, initially 10 minutes, the candidate
block is:

```text
discharge  [D0, D1)  where D1 = D0 + d
recharge   [D1, D2)  where D2 = D1 + d
```

Both directions use their observed supported maximum current. Discharge targets
the current quantized reserve and recharge targets 100%.

The pair is atomic only as desired configuration; Solis API writes remain
sequential. Reconciliation prepares it in this order:

1. stop and prove conflicting directions off;
2. configure both future schedules while disabled;
3. prove Feed-In Priority, Allow Grid Charging, Battery Reserve/target and Grid
   Peak Shaving off;
4. enable and prove the recharge schedule first; and
5. enable the discharge schedule last as the commit point.

An interrupted start may leave only a future recharge-to-100 schedule armed; it
must never leave an unpaired cycle discharge armed.

The inverter clock performs the handover at `D1`. Reconciliation then retains
the active recharge and rolls the expired discharge forward:

```text
initial:  discharge [D0,D1) + recharge  [D1,D2)
at D1:    recharge  [D1,D2) + discharge [D2,D3)
at D2:    discharge [D2,D3) + recharge  [D3,D4)
```

The half-open intervals are adjacent and never overlap. A discharge is
pre-armed only when its complete following recharge also fits inside the same
trusted cheap window. The final phase is therefore always recharge.

### Observation and restart behaviour

Capture the Solis `device_timestamp` observation used to authorize the block.
Do not compare Home Assistant `last_updated` values.

The initial cycle still requires a fresh 100% device observation. Once a pair is
running, the controller advances from the external tariff window and fixed
phase deadline; it does not wait for a delayed SOC sample at each native
boundary. Native target SOC and the controller's unconditional safety stop
still prevent discharge through the reserve or absolute safety boundary.

Cycle phase is RAM-only. After restart the controller derives a fresh action
from current tariff, telemetry and native control state; it does not reconstruct
or persist the previous cycle.

Every encoded charge and discharge boundary remains inside trusted cheap
authority. Tariff or lease end always wins over completing or repeating a
cycle.

### Generic cheap-window placement

Production cycle placement is driven by the fused trusted cheap interval, not
by whether the authority originated from the static tariff or Intelligent
Dispatch. For cycle duty `d`, a block is eligible only when one current trusted
interval contains the complete half-open range `[D0,D0 + 2d)`. With the initial
10-minute duty this requires at least 20 minutes of remaining cheap authority.

While the battery is below 100%, the same interval selects ordinary
`CHEAP_CHARGE`. A fresh device observation reaching 100% may select the first
eligible paired block wholly inside the remaining interval. The accepted
20-minute cycle block is a narrow exception to the ordinary 15-minute future
charge-lease horizon: each physical direction remains bounded to 10 minutes,
the native end boundary caps exposure, and an authority change requests an
immediate reconciliation.

Changes to static rates, fused rates or Intelligent Dispatch provenance are
ordinary controller events:

- if the currently armed pair is still wholly covered, leave it unchanged;
- before `D0`, cancel or replace a pair no longer covered by trusted cheap
  authority;
- during either phase, loss or shortening of authority creates immediate stop
  debt for every direction whose remaining range is no longer cheap;
- an extension that still covers the existing pair causes no Solis write; and
- the one-minute reconciliation remains only a backstop for missed events.

Do not build separate static and dispatch cycle workflows. Existing tariff
trust, dispatch provenance, equality/readback and important-stop rules are the
boundaries. The existing local-midnight splitter allocates each split phase
within its commissioned owner; no separate cross-midnight workflow exists.

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

## Execution record

The first live capability experiment is scheduled for the static cheap period
on 2026-08-27. A self-contained runner is active in the Advanced SSH add-on as
PID `29611`; it is read-only until the experiment starts. Its validated
SHA-256 is
`baf8a88af0b0c3dca598d9eb60151f4dfb8a52ee098aef9df9f816d930bf766e`.

The bounded sequence is:

- at 04:25 Europe/London, prove the static cheap rate independently of any
  overlapping bonus dispatch;
- snapshot the persistent controls and every native direction;
- disable the YAML controller include and restart Core, leaving the Solis
  control and telemetry integrations available;
- prove the controller heartbeat has stopped and stop only directions observed
  on;
- arm slot 1 discharge `[05:00,05:10)` at the observed maximum current and the
  captured quantized reserve target;
- arm slot 1 recharge `[05:10,05:20)` at the observed maximum current and 100%;
- enable recharge first and discharge last, then prove the entire configuration
  before 04:58;
- make no control write at either 05:00 or 05:10 while capturing 15-second HA
  control, Solis telemetry and Octopus whole-site-demand evidence;
- after 05:22, disable and prove both directions off; and
- after the 05:30 tariff boundary, restore the controller include and restart
  Core.

If static cheap authority, controller shutdown, any readback or the complete
pair cannot be proven, the runner aborts the discharge,
cleans up the two experiment directions, selects Self-Use during emergency
cleanup and restores the controller. Evidence is retained on HA under
`/config/t0047-evidence/`.

The runner did not reach the experiment: it exited with `label: unbound
variable`, performed emergency cleanup and wrote zero sample records. This is
an aborted experiment, not a paired-operation rejection.

## Implementation decision

The desired schedule supports only:

- zero logical segments;
- one ordinary charge/discharge segment; or
- exactly two ordered cycle segments: `CYCLE_DISCHARGE` then
  `CYCLE_RECHARGE`.

The Solis adapter may split an individual phase at local midnight using the
existing two-slot allocations. It configures every desired phase while disabled,
enables recharge first and enables discharge last. Do not build an arbitrary
schedule graph, workflow engine, transaction journal or persistent cycle record.

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

After live acceptance, update one concise authoritative scheduler contract and
the commissioning log. Historical task cards remain evidence, not current
requirements. Resolve current contradictions about Peak Shaving ownership and
cycle transitions in the active top-level documentation.

## Local acceptance for implementation

The rolling-pair implementation must prove:

- ordinary cheap charge, reserve discharge and reserve follow require no generic
  transition state;
- every conflict is off-proven before an incompatible start;
- every cycle discharge is immediately followed by a bounded recharge;
- an incomplete final discharge/recharge pair is never added near tariff end;
- the active phase remains stable while its adjacent successor is pre-armed;
- tariff/lease boundaries stop charge and discharge independently of telemetry;
- Peak Shaving handovers follow the destination-aware ordering;
- start failures remain best effort and important stops remain unbounded;
- fail-safe, sentinel and shutdown semantics remain unchanged;
- native schedules never overlap under half-open interpretation; and
- the full component and deployment suites pass.
