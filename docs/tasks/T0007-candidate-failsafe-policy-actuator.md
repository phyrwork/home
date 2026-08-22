# T0007 — Implement candidate and fail-safe policy actuation

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0006 — Implement the transactional Solis slot actuator

## Objective

Implement pure persistent-policy builders and a verified actuator for the healthy
Solis candidate and the Home Assistant fail-safe. Candidate application is
default-off and fingerprint-authorized. Fail-safe application remains available
without healthy telemetry or commissioning authorization.

This task does not connect the coordinator, enable dynamic slot strategy, deploy
or access live Home Assistant.

## Candidate policy

Build the healthy candidate from named constants and explicit runtime inputs:

- storage mode: `Feed-In Priority`;
- grid charging: allowed;
- export: allowed;
- Grid Peak Shaving: enabled;
- Over-discharge SOC: `MINIMUM_SOC_PERCENT`;
- Force-charge SOC: `FORCE_CHARGE_SOC_PERCENT`;
- Recovery SOC: `MINIMUM_SOC_PERCENT`;
- Maximum charge SOC: `FULL_SOC_PERCENT`;
- Battery Reserve: enabled with the supplied reserve target;
- inverter on/off: preserve;
- inverter clock: preserve;
- all slot directions: disabled before policy application;
- global charge/discharge current, output and feed-in settings: preserve unless a
  valid verified capability-resolution record supplies a safe maximum or
  documented unlimited value.

Round a calculated reserve target upward to an integer percentage and bound it by
the named physical SOC floor and full SOC. Do not clamp it to stale Home Assistant
entity metadata.

Battery Reserve enabled at the minimum reserve is a commissioning candidate only.
Dynamic strategy and slot actuation remain blocked until T0011 observes and
validates its interaction with Feed-In Priority and Peak Shaving.

## Fail-safe policy

Build and apply this fail-safe:

1. Disable and prove off all 12 slot directions through T0006.
2. Select `Self-Use`.
3. Keep Grid Peak Shaving enabled.
4. Disable Battery Reserve.
5. Preserve inverter on/off state.
6. Preserve physical protection values, grid/export permissions and global
   capability settings.

Continue all requested fail-safe writes after an intermediate expected failure.
Final safe status requires current Home Assistant readback proving:

- all 12 directions off;
- storage mode `Self-Use`;
- Grid Peak Shaving on;
- Battery Reserve off.

Fail-safe bypasses candidate authorization and does not require healthy telemetry.
It uses fresh per-entity compare-and-set through T0005/T0006. Unknown,
unavailable or unverified required final state is unsafe.

## Candidate-policy fingerprint

Add deterministic canonical UTF-8 JSON serialization and SHA-256 fingerprinting
for the policy definition. Include:

- fingerprint schema version;
- actuator ordering version;
- named safety constants, encoded as exact Decimal strings where numeric;
- fixed candidate settings;
- fixed fail-safe settings;
- manual maximum-grid-import policy and expected value;
- Battery Reserve target rule, not the changing runtime reserve result;
- capability-resolution policy;
- mapping-fingerprint schema version.

Use sorted keys, stable arrays, explicit null/boolean values and no insignificant
whitespace. Never serialize policy values through binary floating point.

Recompute the current policy and mapping fingerprints before every candidate
application.

## Commissioning bootstrap authorization

Resolve the commissioning bootstrap with a separate ephemeral authorization:

- issued only through an explicit T0011-facing API;
- non-persistent and invalid after restart;
- single-use nonce;
- timezone-aware expiry no more than ten minutes after issuance;
- bound to the exact current mapping and candidate-policy fingerprints;
- permits one persistent candidate application for validation;
- cannot authorize T0006 slot actuation.

T0011 must use this candidate actuator rather than bypass it with lower-level
entity writes.

Consume the authorization on the first application attempt, including a failed
attempt. Reject reused, expired, future-dated or fingerprint-mismatched tokens.

## Persistent candidate authorization

Normal candidate application is default-off across restart. T0011 may persist a
normal authorization only after Home Assistant readback and delayed Solis
Cloud/device reconciliation are both validated.

The record contains:

- aware issuance/validation time not in the future;
- current mapping fingerprint;
- current candidate-policy fingerprint;
- explicit Home Assistant readback validation;
- explicit Solis Cloud/device reconciliation validation;
- commissioning schema version.

Any mismatch returns `BLOCKED` and performs no candidate write. Fail-safe remains
available.

## Manual grid-import verification

Candidate application also requires a manual commissioning record containing:

- exactly `MAXIMUM_GRID_IMPORT_POWER_KW` as a Decimal;
- aware verification time not in the future;
- matching mapping and candidate-policy fingerprints;
- explicit verification that the inverter Peak Shaving maximum-grid-power setting
  contains that value.

There is no Home Assistant entity write for this setting.

## Capability-resolution records

Maximum or documented-unlimited writes require a separate immutable verification
record per capability. Bind each record to:

- exact entity ID and observed unit;
- verified Decimal target or documented sentinel;
- aware verification time not in the future;
- current mapping and candidate-policy fingerprints;
- verification source/evidence classification.

Without a valid record, preserve the current setting. Never use a generic entity
maximum as a verified hardware capability.

## Candidate preconditions

Before writes require:

- a valid ephemeral or normal candidate authorization;
- matching policy and mapping fingerprints;
- valid manual grid-limit verification;
- `HEALTHY` T0004 state;
- all 12 slot directions disabled or successfully cleaned up;
- inverter clock within the T0006 skew limit;
- requested reserve within named physical bounds.

Require the control-disable guard to be exact `off`. Re-read it immediately before
every candidate mutation and immediately after each Home Assistant readback. If it
becomes asserted, missing, unknown, unavailable or otherwise invalid, stop
candidate application and invoke fail-safe. A prior guard read is never sufficient
authorization for a later write.

Candidate authorization does not authorize a slot intent.

## Candidate write order

Use one serialized orchestration with ordered T0005/T0006 results:

1. Disable and prove off all slot directions.
2. Apply and verify physical protection SOC settings.
3. Obtain a fresh T0004 snapshot because Over-discharge SOC can change Reserve and
   slot-SOC native bounds.
4. Require the refreshed Battery Reserve SOC capability to accept the desired
   reserve target exactly.
5. Apply only valid fingerprint-bound capability resolutions; preserve all
   others.
6. Enable grid charging and export.
7. Enable Grid Peak Shaving.
8. Set Battery Reserve SOC and enable Battery Reserve.
9. Select `Feed-In Priority` last.
10. Re-read and verify the complete desired Home Assistant policy state.

Do not clamp against incompatible refreshed metadata. A stale, missing or
incompatible refresh invokes fail-safe.

Any conflict, rejection, service error, timeout, partial application or uncertain
readback invokes fail-safe. Do not attempt rollback to an older candidate.

## Result semantics

Distinguish at least:

- `BLOCKED` — authorization or precondition rejected before candidate mutation;
- `APPLIED_HA_PENDING_DEVICE_RECONCILIATION` — candidate has complete Home
  Assistant readback but is not device-confirmed;
- `FAIL_SAFE_APPLIED_HA_PENDING_DEVICE_RECONCILIATION` — explicitly requested
  fail-safe has complete Home Assistant readback;
- `CANDIDATE_FAILED_FAIL_SAFE_APPLIED` — candidate failed and the required
  fail-safe state was conclusively established in Home Assistant;
- `FAIL_SAFE_FAILED_UNSAFE` or `FAILED_UNSAFE` — required safe state could not be
  proven.

Preserve ordered candidate and fallback entity results and issues. Never call an
HA-confirmed result device-confirmed.

## Cancellation

On cancellation during candidate application, use the T0006 cancellation pattern:
shield and await fail-safe cleanup, record whether final safe state was proven,
then re-raise the original `CancelledError`.

Cancellation during an explicitly requested fail-safe must continue the fail-safe
task to its bounded conclusion before propagating cancellation.

## Compatibility boundaries

- Do not connect coordinator, planner, controller or sensor.
- Do not enable or configure a dynamic slot intent.
- Do not add the independent watchdog yet.
- Do not remove legacy stubs or helpers.
- Do not deploy or access live Home Assistant.

## Tests

Cover:

- exact candidate and fail-safe builders using named constants;
- reserve ceiling and named physical bounds;
- canonical policy serialization and stable fingerprint;
- fingerprint changes for each relevant policy category;
- expired, reused, future and mismatched ephemeral authorization;
- proof an ephemeral token cannot authorize a slot;
- default-off and fingerprint-bound persistent authorization;
- exact manual grid-limit verification;
- missing, stale and mismatched capability records preserving current values;
- fail-safe without healthy telemetry or candidate authorization;
- fail-safe continuing after each possible intermediate failure;
- final proof of all four required fail-safe conditions;
- candidate protection-first ordering;
- mandatory fresh capability read after protection changes;
- changed compatible and incompatible Reserve SOC bounds;
- no clamping to stale or incompatible metadata;
- storage mode applied last;
- guard revalidation before and after every candidate mutation;
- an in-flight guard change invoking fail-safe before further candidate writes;
- failures at every candidate stage invoking fail-safe;
- explicit result distinctions and ordered diagnostics;
- shielded fail-safe and cancellation propagation;
- no slot enable, coordinator mutation, deployment or live access.

Run all existing integration tests when dependencies are available.

## Acceptance criteria

- Candidate writes are impossible without current mapping, policy and manual-grid
  authorization.
- Commissioning can bootstrap once without granting slot control.
- Policy or entity-map changes revoke prior authorization automatically.
- Physical protections are verified before reserve bounds are refreshed and used.
- Capability settings are preserved unless explicitly verified.
- Every candidate failure invokes the specified fail-safe.
- Safe status requires conclusive final-state proof.
- Home Assistant and device reconciliation are never conflated.
- Focused offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0007-candidate-failsafe-policy-actuator`
- Isolated worktree.
- Small-model implementation agent after T0006 is integrated.
- Expected changes are limited to policy/authorization contracts, policy actuator,
  focused tests and this card.
