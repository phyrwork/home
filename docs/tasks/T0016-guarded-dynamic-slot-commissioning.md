# T0016 — Commission dynamic Solis slot evidence safely

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0006 — transactional Solis slot actuator;
- T0007 — candidate and fail-safe policy actuator;
- T0008 — observation coordinator and heartbeat;
- T0009 — independent watchdog;
- T0011 — guarded candidate commissioning workflow.

## Objective

Add the offline, default-disabled workflow and immutable evidence contracts needed
to commission dynamic Solis slots without inferring an AC-power-to-current
conversion.

The workflow validates one fixed writable operating point for each managed
direction, measures conservative cloud/device control timing and can emit a
human-review IaC snippet. It never enables production slot authority, edits the
repository, deploys, authenticates or performs live commissioning in this task.

## Required production evidence

Dynamic production actuation requires all three current evidence groups below.
Any missing, incomplete, stale, legacy-version or fingerprint-mismatched group
makes T0006 uncommissioned and permits cleanup only.

### Production slot authority

Bump T0006's commissioning schema and replace the mapping-only record with an
immutable `ProductionSlotAuthority` containing:

- commissioning schema and inverter identity;
- current mapping and candidate-policy fingerprints;
- current manual-grid and capability fingerprints;
- the complete accepted operating-point-set fingerprint;
- accepted control-timing-budget fingerprint;
- validation time;
- explicit Home Assistant readback validation; and
- explicit advanced Solis Cloud/device reconciliation validation.

Legacy mapping-only `CommissioningRecord` values are uncommissioned. T0006's
constructor receives the current authority bundle and recomputes every fingerprint
before every production apply. Any drift revokes authorization immediately.

Bootstrap authorization and production authorization are distinct exact types.
The normal path must reject bootstrap values and the bootstrap path must reject
production records.

### Fixed direction operating points

Commission exactly one `DirectionOperatingPoint` for each managed path:

```text
(CHEAP_CHARGING, CHARGE, physical slot 1)
(FULL_SOC_CYCLING, DISCHARGE, physical slot 1)
(PRE_DISCHARGE, DISCHARGE, physical slot 2)
```

Each immutable record binds:

- owner, direction, physical slot and every exact control entity ID;
- exact writable current as a `Decimal`, unit and step;
- verified global-current ceiling;
- target-SOC unit, step, accepted range and protection/reserve semantics;
- declared authoritative SOC, battery-voltage and device/battery-temperature
  operating envelope;
- conservative minimum and maximum AC input/output kW at that exact setting;
- measurement boundary and sign semantics;
- measurement and manufacturer uncertainty rules;
- accepted sample IDs, device source revisions and timestamps;
- manufacturer/operator safety attestation;
- evidence source and validation time; and
- all current authority fingerprints.

There is no interpolation, extrapolation or arbitrary kW-to-amp conversion.
Later timing/refill calculations use the conservative minimum AC kW; reserve,
equipment and depletion proofs use the conservative maximum AC kW. Both must lie
within the T0013 commissioned AC envelope. Runtime conditions outside the
validated operating envelope make the operating point unavailable.

Generic Home Assistant bounds never establish equipment safety or power.

### Control timing budget

Add immutable `DevicePollCadenceEvidence` bound to integration version, entity
map, device, source revisions and validation time. It records both the configured
integration cadence and consecutive advanced cloud/device observations. A local
timeout or `MAXIMUM_TELEMETRY_AGE` cannot substitute for measured device cadence.

Measure separate sequential upper bounds for:

- disable request and Home Assistant readback;
- all-off device proof;
- slot configuration and Home Assistant readback;
- enable request and Home Assistant readback;
- exact enabled-slot device reconciliation;
- telemetry response latency;
- direction-change stop/off proof; and
- final cleanup/all-12-off reconciliation.

For each component, the conservative upper bound is:

```text
maximum accepted successful duration
+ timestamp measurement uncertainty
+ two conservative measured device polling periods
```

Define and fingerprint end-to-end sums:

```text
BOOTSTRAP_APPLY_BUDGET =
    disable_and_prove_off
    + configure
    + enable_and_ha_readback
    + enabled_device_reconciliation

DIRECTION_CHANGE_BUDGET =
    stop_and_device_off_proof
    + BOOTSTRAP_APPLY_BUDGET

CLEANUP_BUDGET =
    stop_and_ha_readback
    + all_twelve_device_off_reconciliation
```

Sum sequential operations; take a maximum only across genuinely alternative
branches. The `ControlTimingBudget` records every component, formula/version,
sample generation and complete fingerprint.

## Deterministic evidence matrix

Define:

```python
MINIMUM_OPERATING_POINT_SAMPLES_PER_STRATUM = 3
MINIMUM_TIMING_SAMPLES_PER_COMPONENT_PER_DIRECTION = 3
```

For each managed direction, the operating-point strata are the Cartesian product
of the inclusive lower and upper endpoints of every retained SOC, battery-voltage
and device/battery-temperature dimension, plus one all-dimension midpoint
stratum.

A manufacturer evidence record may remove a dimension only when it explicitly
proves the operating point invariant across that complete dimension and is bound
into the fingerprint.

Require the named minimum count of independent, successful, advanced-device
samples in every stratum. Each sample has a unique run nonce and sample ID. An
explicit replacement run may supersede a failed sample in a later evidence
generation, but the failure remains immutable audit evidence and its hash remains
in the accepted generation's fingerprint. Until replacement completes, the
generation is unavailable.

Conservative minimum/maximum AC kW are the extrema across every accepted stratum,
expanded by declared measurement and manufacturer uncertainty.

Apply the timing sample minimum to every timing component and managed direction.
Timeouts and failures are retained and block the generation until explicitly
replaced; they are never silently discarded as outliers.

## Pre-test safety attestation

Before any risk-increasing bootstrap write, require a separately supplied
manufacturer/operator `PreTestSafetyAttestation` covering the complete exact test
tuple:

- owner, direction, physical slot and all entity IDs;
- current, unit, step and verified global ceiling;
- target SOC and reserve/over-discharge/protection-floor semantics;
- active encoded interval, end, expiry and schedule representation;
- declared SOC, battery-voltage and temperature envelope;
- mapping, policy, manual-grid and capability fingerprints;
- issuer, issuance time and one test-case fingerprint.

Also require fresh live entity unit/range/step metadata and the exact verified
global-current ceiling. Any tuple change consumes or rejects the authorization.
The system never discovers safety only after applying a test setting.

## Bootstrap authorization

Resolve the normal-gate circularity with a memory-only
`DynamicCommissioningAuthorization` registry.

Each token is:

- issued only inside one explicit active commissioning session;
- an exact immutable registry record, not a self-asserted nonce;
- single-use and consumed on the first attempt;
- valid for no more than ten minutes;
- bound to one test case, managed direction, current, target SOC and active
  bounded interval;
- bound to current policy, mapping, session and attestation fingerprints; and
- lost on restart.

Add a distinct T0006 bootstrap entry point that accepts only the exact registered
token and exact pre-registered active test intent. It cannot target another slot,
owner, direction, value or time and cannot authorize a production intent.

Every bootstrap test begins and ends with authoritative all-slots-off proof and
always schedules/executes cleanup. If the safe bootstrap boundary cannot be
implemented without weakening the normal production gate, return `BLOCKED`.

## Watchdog readiness and deadline

Require a fresh immutable `WatchdogReadinessProof` showing:

- T0009 watchdog automation enabled;
- fail-safe script available;
- heartbeat fresh;
- guard semantics valid; and
- every watchdog-required entity resolvable.

Bind its source, revision, timestamp and session fingerprint.

Choose the bootstrap slot deadline such that:

```text
activation_time
+ HEARTBEAT_STALE_AFTER
+ measured_watchdog_trigger_cadence
+ watchdog_script_execution_bound
< encoded_slot_end
```

The complete apply and cleanup budgets must also fit the active session deadline,
and the encoded test is capped by a named maximum test duration. If all
inequalities cannot hold together, block the test.

## Commissioning workflow

Register strict, idempotent, response-capable begin, next-test, validate and abort
services. They are disabled by default and serialize one memory-only session and
one test at a time.

Before each direction test require:

- exact guard `off`;
- healthy T0008 coordinator and current candidate device reconciliation;
- fresh authoritative telemetry within the declared envelope;
- fresh exact watchdog readiness;
- all 12 directions authoritatively off;
- current matching authority fingerprints; and
- explicit human confirmation of the risk-increasing configuration/enable.

Human confirmation never gates disable-all, guard assertion or fail-safe cleanup.

For each managed direction:

1. prove all 12 directions off at the device;
2. validate the complete pre-test attestation and exact live metadata;
3. consume one exact bootstrap token;
4. apply the already-active bounded test through T0006;
5. prove exact Home Assistant readback;
6. require a post-write cloud/device refresh whose source revision/device
   timestamp advanced after the write and whose exact enable, time, current and
   target SOC values match;
7. record power direction, sign, boundary, operating conditions and AC power;
8. stop and clean up automatically; and
9. require an advanced device proof that all 12 directions are off.

An advancing inverter clock or battery-power sign alone is not device proof.

Any timeout, mismatch, cancellation, guard/health change, fingerprint drift or
deadline invokes automatic bounded guard assertion and T0007/T0009 fail-safe,
invalidates the session and reports whether safe state was conclusively proven.

## Successful validation

Complete evidence emits a deterministic human-review IaC snippet containing:

- the extended production authority fields;
- accepted fixed operating points and audit fingerprints;
- complete control-timing evidence and formulas; and
- evidence times and schema versions.

The snippet keeps production slot authorization `false`, contains no reusable
token and performs no repository write. Human review, IaC activation, deployment
and authenticated live execution remain separate future actions.

## Result semantics

Distinguish at least:

- `BLOCKED`;
- `PENDING`;
- `TEST_APPLIED_HA_PENDING_DEVICE`;
- `TEST_PROVEN_OFF`;
- `COMPLETE_REVIEW_REQUIRED`;
- `FAILED_SAFE`; and
- `FAILED_UNSAFE`.

Preserve ordered write, HA, device, timing, sample and cleanup evidence. Never
call HA readback device-confirmed.

## Compatibility boundaries

- Do not authorize a production slot in generated or deployed configuration.
- Do not implement the strategy composer; it moves to T0017.
- Do not infer power from voltage/current or generic HA bounds.
- Do not deploy, authenticate or access live Home Assistant.
- Preserve T0006 cleanup availability when all commissioning evidence is absent.

## Tests

Use deterministic fake clocks, reader, writer, slot actuator, watchdog and device
sources to cover at least:

- default-disabled services, strict schemas and restart loss;
- legacy T0006 record invalidation and every authority fingerprint drift;
- exact separation of bootstrap and production token types;
- forged, reconstructed, reused, expired and mismatched bootstrap tokens;
- proof a bootstrap cannot target another entity/value/interval or normal path;
- complete pre-test tuple attestation and each mismatch;
- exact live current/SOC metadata and global ceiling;
- all three managed paths and rejection of every other path;
- deterministic corner/midpoint strata and independent sample counts;
- missing dimensions, invariant evidence and outside-envelope conditions;
- successful, failed and explicitly replaced sample generations;
- uncertainty-expanded conservative minimum/maximum AC power;
- T0013 envelope compatibility and no interpolation;
- every timing component, minimum sample count and sequential aggregate;
- authoritative device poll evidence and rejection of timeout substitution;
- complete watchdog readiness and deadline inequalities;
- human confirmation for configure/enable but never cleanup;
- exact HA-versus-advanced-device enabled-slot proof;
- exact advanced all-12-off cleanup proof;
- every timeout, guard, health, fingerprint and device mismatch;
- cancellation and repeated cancellation during every stage;
- deterministic still-disabled IaC snippet; and
- absence of authentication, deployment and repository mutation.

Run the complete house-battery and deployment suites.

## Acceptance criteria

- No generic bound or current-to-power inference authorizes dynamic control.
- Every managed direction has conservative power evidence across a deterministic
  accepted operating envelope.
- Control deadlines use measured end-to-end cloud/device timing.
- Bootstrap authority cannot escape one exact supervised test.
- Policy or evidence drift revokes production authority immediately.
- Device proof covers exact slot configuration and cleanup state.
- Watchdog recovery can complete before every bootstrap slot ends.
- All cleanup remains automatic and independent of human confirmation.
- Generated IaC is review-only and production-disabled.
- Offline focused and full tests pass.
- The implementation commit changes this card's status to `Implemented` while
  recording live execution as deferred pending user authentication.

## Implementation ownership

- Branch: `codex/T0016-guarded-dynamic-slot-commissioning`.
- Isolated worktree.
- Small-model implementation agent after T0008-T0011 are integrated.
- Serialize T0006/T0007 schema changes with other actuator work.
