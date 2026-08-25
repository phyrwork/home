# T0020 — Cut diagnostics over to the guarded runtime controller

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0019 — guarded dynamic coordinator cutover.

## Objective

Replace the remaining legacy abstract-command diagnostics with stable, bounded
Home Assistant sensors for the real guarded runtime. Preserve the existing
heartbeat, health, battery-energy and reserve entities and their registry
identities while making policy, strategy, actuation, proof and recovery state
understandable without inspecting logs.

This is observation-only. It must not change a strategy decision, commission
authority, watchdog condition, fail-safe action or Solis control.

## Versioned diagnostics boundary

T0019 must first expose a frozen, immutable `RuntimeDiagnosticsV1` DTO as an
explicit field of every completed coordinator snapshot. T0019 constructs and
validates it during refresh, before publishing the snapshot. Entity properties
read only this DTO plus the snapshot's already-normalized heartbeat, energy and
reserve scalars.

Sensor code must never traverse composer, cycle, action-worker, journal,
authorization, proof or source objects. It must not hash, normalize, call a
strategy, inspect the store or infer diagnostics from nullable runtime fields.

`RuntimeDiagnosticsV1` has this exact allowlisted schema:

| Field | Type and constraint |
| --- | --- |
| `schema_version` | integer, exactly `1` |
| `control_status` | required `RuntimeControlStatus` |
| `strategy_phase` | optional `StrategyDiagnosticPhase` |
| `actuation_state` | required `ActuationDiagnosticState` |
| `mutation_obligation` | optional exact T0017 mutation enum value |
| `policy_status` | optional exact persistent-policy status code |
| `active_intent_type` | optional exact intent enum value |
| `active_intent_id` | optional bounded opaque ID |
| `intent_start`, `intent_end`, `intent_expiry`, `fresh_until` | optional aware datetimes |
| `cleanup_deadline`, `proof_deadline`, `lease_expiry` | optional aware datetimes |
| `dynamic_control_enabled` | required Boolean |
| `guard_state`, `guard_quality` | required bounded enum codes |
| `watchdog_status` | required bounded enum code |
| `watchdog_fresh_until` | optional aware datetime |
| `last_healthy_at` | optional aware datetime copied from the completed coordinator snapshot |
| `source_generation`, `decision_generation`, `transition_id`, `journal_generation` | optional non-negative integers |
| `journal_checksum` | optional validated lowercase SHA-256 digest |
| `journal_recovery_state` | optional bounded enum code |
| `audit_digest`, `stable_digest`, `mutation_digest`, `mapping_digest`, `policy_digest`, `commissioning_digest`, `forecast_digest`, `reserve_digest`, `tariff_digest`, `dispatch_digest` | optional validated lowercase SHA-256 digests |
| `desired_proof_status`, `ha_proof_status`, `device_proof_status` | optional bounded enum codes |
| `ha_proof_ordinal`, `device_proof_ordinal` | optional T0019-assigned unsigned 63-bit monotonic ordinals |
| `ha_proof_revision_digest`, `device_proof_revision_digest` | optional validated lowercase SHA-256 digests of opaque source revisions |
| `candidate_result`, `slot_result`, `cleanup_result`, `fail_safe_result` | optional bounded enum/status codes |
| `window_classification` | optional exact T0012 classification |
| `import_price`, `export_price` | optional finite `Decimal` scalars |
| `planned_stored_energy_kwh`, `planned_ac_energy_kwh`, `reserve_margin_kwh`, `incremental_value` | optional finite `Decimal` scalars |
| `issue_codes`, `rejected_candidate_codes`, `cancellation_codes`, `reconciliation_codes` | bounded tuples of stable codes |
| `truncated_code_count` | required non-negative integer |
| `control_attributes`, `phase_attributes`, `actuation_attributes`, `health_attributes`, `heartbeat_attributes` | immutable, precomputed tuples of allowlisted `(key, JSON scalar or tuple)` pairs |

No other field is permitted. A missing or invalid projection leaves the DTO
absent and sets one precomputed bounded `diagnostic_projection_issue_code` on
the completed T0019 snapshot. Sensor properties expose that code through
health/heartbeat diagnostics without inspecting the rejected object.

A valid DTO requires the snapshot's `diagnostic_projection_issue_code` to be
absent. An absent DTO requires that snapshot field to contain one exact
`DiagnosticProjectionIssueCode`. The issue code is never a DTO field.

## Native-state enums and precedence

Define a dedicated `RuntimeControlStatus`. It contains `DURABLE_RECOVERY`,
`DYNAMIC_DISABLED`, `OBSERVATION_ONLY`, and every exact T0017 high-level status:

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

It has no free-form values. T0019 selects it and the control sensor emits its
value unchanged.

T0020's first implementation step adds the DTO builder to the integrated T0019
snapshot. The builder applies this control-state precedence:

1. durable journal recovery → `DURABLE_RECOVERY`;
2. fail-safe/unsafe cleanup → `FAIL_SAFE`;
3. dynamic guard disabled → `DYNAMIC_DISABLED`;
4. T0019 has not admitted dynamic decisions → `OBSERVATION_ONLY`; and
5. the exact T0017 result status.

`StrategyDiagnosticPhase` contains exactly `DURABLE_RECOVERY`, `FAIL_SAFE`,
`POLICY_CONVERGENCE`, `CYCLE_READY`, `CYCLE_DISCHARGING`,
`CYCLE_WAITING_FOR_DISCHARGE_OFF_PROOF`, `CYCLE_RECHARGING`,
`CYCLE_WAITING_FOR_ALL_OFF_PROOF`, `CYCLE_COMPLETE`, `CYCLE_ABORTING`,
`CYCLE_ABORTED_INCOMPLETE`, `PRE_DISCHARGE`, `FINAL_CHARGE`, `CHEAP_CHARGE` and
`IDLE`. Its precedence is:

1. fail-safe or durable recovery;
2. policy convergence;
3. an active T0015 cycle phase, represented by a distinct cycle-prefixed enum;
4. an active pre-discharge, final-charge or cheap-charge intent phase; and
5. `IDLE`.

The sensor never chooses between composer and cycle phases.

`strategy_phase=None` is valid only when `control_status` is `UNAVAILABLE` or
`INVALID` and no trustworthy phase exists. Every other control status requires
a typed phase, including `IDLE`.

`ActuationDiagnosticState` contains exactly `DURABLE_RECOVERY`, `FAIL_SAFE`,
`CLEANUP`, `HA_PENDING_DEVICE`, `APPLYING`, `QUEUED`, `PROVEN` and `IDLE`.
T0019 applies that highest-to-lowest precedence when several conditions exist.
`HA_PENDING_DEVICE` is valid only under T0019's fresh bounded transition lease.

The remaining bounded enums are exact:

- `PolicyDiagnosticStatus`: `NOT_REQUIRED`, `FAIL_SAFE_REQUIRED`,
  `WAITING_FOR_FAIL_SAFE_PROOF`, `CANDIDATE_REQUIRED`,
  `WAITING_FOR_CANDIDATE_PROOF`, `CONVERGED`;
- `IntentDiagnosticType`: `PRE_DISCHARGE`, `FULL_SOC_DISCHARGE`,
  `FINAL_CHARGE`, `CHEAP_CHARGE`;
- `GuardDiagnosticState`: `ON`, `OFF`, `MISSING`, `UNKNOWN`, `UNAVAILABLE`,
  `INVALID`;
- `GuardDiagnosticQuality`: `VALID_OPEN`, `VALID_CLOSED`, `INVALID`;
- `WatchdogDiagnosticStatus`: `READY`, `UNAVAILABLE`, `DISABLED`, `STALE`,
  `INVALID`, `UNCOMMISSIONED`;
- `JournalRecoveryDiagnosticState`: `CLEAN`, `PENDING`, `RECOVERING`,
  `DURABLE_FAIL_SAFE`, `INVALID`;
- `ProofDiagnosticStatus`: `NOT_REQUIRED`, `MISSING`, `HA_MATCH`,
  `DEVICE_ADVANCED`, `PENDING`, `STALE`, `MISMATCH`;
- `ActionDiagnosticResult`, used separately for candidate, slot, cleanup and
  fail-safe: `NOT_ATTEMPTED`, `PENDING`, `SUCCEEDED`, `BLOCKED`, `FAILED`,
  `CANCELLED`, `UNSAFE`; and
- `DiagnosticProjectionIssueCode`: `MISSING`, `SCHEMA_INVALID`,
  `CROSS_FIELD_INVALID`, `VALUE_INVALID`, `SIZE_EXCEEDED`, `SECRET_REJECTED`.

Mutation-obligation and window-classification values must be members of their
imported domain enums. Diagnostic collections use one new versioned enum,
`RuntimeDiagnosticCodeV1`, with exactly these category-distinct literals:

- issue: `ISSUE_INPUT_UNAVAILABLE`, `ISSUE_INPUT_INVALID`,
  `ISSUE_SOURCE_STALE`, `ISSUE_SOURCE_FUTURE`,
  `ISSUE_FINGERPRINT_MISMATCH`, `ISSUE_AUTHORITY_MISSING`,
  `ISSUE_AUTHORITY_INVALID`, `ISSUE_DEADLINE_EXPIRED`,
  `ISSUE_PERSISTENCE_ERROR`, `ISSUE_WATCHDOG_NOT_READY`,
  `ISSUE_GUARD_CLOSED`, `ISSUE_WRITE_FAILED`, `ISSUE_PROOF_MISSING`,
  `ISSUE_PROOF_MISMATCH`, `ISSUE_CANCELLED`, `ISSUE_UNEXPECTED_ERROR`;
- rejected candidate: `REJECT_NO_TRUSTED_WINDOW`, `REJECT_NO_HEADROOM`,
  `REJECT_UNPROFITABLE`, `REJECT_RESERVE_INFEASIBLE`,
  `REJECT_TIMING_INFEASIBLE`, `REJECT_DWELL_ACTIVE`,
  `REJECT_REPEAT_BLOCKED`, `REJECT_POLICY_NOT_CONVERGED`;
- cancellation: `CANCEL_ACTION`, `CANCEL_CLEANUP`, `CANCEL_SHUTDOWN`,
  `CANCEL_TIMEOUT`; and
- reconciliation: `RECONCILE_ALL_OFF_REQUIRED`,
  `RECONCILE_DISCHARGE_OFF_REQUIRED`, `RECONCILE_CANDIDATE_PROOF_REQUIRED`,
  `RECONCILE_FAIL_SAFE_PROOF_REQUIRED`, `RECONCILE_DEVICE_ADVANCE_REQUIRED`,
  `RECONCILE_JOURNAL_RECOVERY_REQUIRED`, `RECONCILE_TRANSITION_PENDING`,
  `RECONCILE_DETACHED_ACTION_PENDING`.

No two literals are equal across categories. Deduplicate by complete enum member
globally after the fixed category-priority concatenation; the first occurrence
wins. Arbitrary strings are invalid.

Cross-field invariants include:

- `DURABLE_RECOVERY` control, phase and actuation must agree and journal
  recovery must be `PENDING`, `RECOVERING` or `DURABLE_FAIL_SAFE`;
- `FAIL_SAFE` control requires fail-safe phase/actuation or active cleanup;
- `DYNAMIC_DISABLED` and `OBSERVATION_ONLY` cannot be `QUEUED`, `APPLYING`,
  `HA_PENDING_DEVICE` or carry `APPLY_INTENT`;
- `APPLY_INTENT` requires an active intent ID/type and exact apply obligation;
- `HA_PENDING_DEVICE` requires a fresh lease, `HA_MATCH`, device `PENDING`, an
  active transition and matching mutation digest; and
- `PROVEN` requires `DEVICE_ADVANCED`, no cleanup/fail-safe/recovery state and
  no unresolved mutation obligation.

Additional invariants are exact:

- guard `OFF` pairs only with `VALID_OPEN`; `ON` only with `VALID_CLOSED`;
  `MISSING`, `UNKNOWN`, `UNAVAILABLE` and `INVALID` pair only with `INVALID`;
- `dynamic_control_enabled=True` forbids `DYNAMIC_DISABLED`;
  `dynamic_control_enabled=False` requires `DYNAMIC_DISABLED`, `FAIL_SAFE` or
  `DURABLE_RECOVERY` after precedence and forbids every apply/keep state;
- watchdog `READY` requires `watchdog_fresh_until` strictly after the snapshot
  heartbeat; `STALE` requires a non-null deadline at or before heartbeat; all
  other watchdog states require no freshness deadline;
- active intent ID, type, start, end and expiry are all present or all absent;
  when present, `start < end <= expiry`, the snapshot heartbeat lies in the
  half-open `[start, end)` interval, and diagnostic `fresh_until <= expiry`;
- `QUEUED` and `APPLYING` require transition ID, mutation digest and matching
  pending action result but no lease; `HA_PENDING_DEVICE` additionally requires
  a future lease expiry and proof deadline;
- lease fields are absent outside `HA_PENDING_DEVICE`;
- HA/device proof ordinals and revision digests are both present or absent for
  each proof, and device ordinal must be strictly greater than HA ordinal for
  `DEVICE_ADVANCED`; and
- `SUCCEEDED` with `PROVEN` requires `DEVICE_ADVANCED`; `PENDING` results require
  `QUEUED`, `APPLYING` or `HA_PENDING_DEVICE`; failed, cancelled, blocked or
  unsafe results cannot coexist with `PROVEN`.

## Stable entity and registry contract

Preserve these literal unique IDs and friendly names:

| Expected entity ID | Unique ID | Friendly name |
| --- | --- | --- |
| `sensor.house_battery_control` | `house_battery_control` | `House Battery Control` |
| `sensor.house_battery_control_heartbeat` | `house_battery_control_heartbeat` | `House Battery Control Heartbeat` |
| `sensor.house_battery_control_health` | `house_battery_control_health` | `House Battery Control Health` |
| `sensor.house_battery_energy` | `house_battery_control_energy` | `House Battery Energy` |
| `sensor.house_battery_reserve_target` | `house_battery_control_reserve_target` | `House Battery Reserve Target` |
| `sensor.house_battery_reserve_usable` | `house_battery_control_reserve_usable` | `House Battery Reserve (Usable)` |
| `sensor.house_battery_reserve_forecast` | `house_battery_control_reserve_forecast` | `House Battery Reserve (Forecast)` |
| `sensor.house_battery_reserve_balance` | `house_battery_control_reserve_balance` | `House Battery Reserve Balance` |

Remove the `observation_only:<legacy mode>` state and all sensor dependency on
old `controller.Command` and fake `solis_cloud.Control` vocabulary.

Add exactly:

| Expected entity ID | Unique ID | Friendly name |
| --- | --- | --- |
| `sensor.house_battery_strategy_phase` | `house_battery_control_strategy_phase` | `House Battery Strategy Phase` |
| `sensor.house_battery_actuation_state` | `house_battery_control_actuation_state` | `House Battery Actuation State` |

Heartbeat retains `SensorDeviceClass.TIMESTAMP`. The five energy entities
retain `SensorDeviceClass.ENERGY`, kWh, no state class, and display precision
2. Introduce no platform migration or device-registry association.

Test with a prepopulated entity registry containing every existing unique ID,
reload the platform, and prove the same rows remain with no `_2` entities.
Repeat for the two new IDs after first registration.

## Exact diagnostic projection

The three control/phase/actuation native states come only from the DTO's typed
fields. Attributes expose the remaining DTO fields under identical stable names
and types; omit optional `None` values.

Use exact enum values, never friendly prose. Do not derive mutation meaning
from a nullable slot. Render aware datetimes as ISO 8601 strings and finite
`Decimal` values as JSON numeric scalars.

T0019 assigns and durably persists collision-free monotonic ordinals for source,
HA proof and advanced-device observations; the DTO never converts an opaque HA
revision into an integer. Every generation, transition ID, ordinal and
truncation count is from 0 through `2**63 - 1`. Preserve each opaque raw revision
only as a canonical SHA-256 digest computed by the T0019 builder. Sensor fields
never expose or normalize the raw value.

The only opaque string is `active_intent_id`, with ASCII grammar
`[A-Za-z0-9._:-]{1,64}`. Stable codes come only from the exact enums above and
the imported domain enums. No arbitrary bounded string is recorder-safe.

Fingerprint fields accept only exact 64-character lowercase hexadecimal
digests. T0019 validates an existing digest, computes an allowlisted digest
while constructing the DTO, or omits it. Sensor properties never hash.

Never expose tokens, authorization objects, 1Password references, service
payloads, exception messages, raw journal records, slot tables, forecast arrays
or reserve trajectories.

Every diagnostic Decimal must be finite, have absolute value at most
`1_000_000_000`, and have no more than six fractional decimal places. Convert it
to a JSON number only when `Decimal(str(float(value))) == value`; otherwise the
DTO is invalid. This makes conversion deterministic and rejects silent precision
loss, NaN and Infinity.

## Availability and fail-closed semantics

Heartbeat and health remain available whenever a latest completed snapshot
exists, including degraded, fail-safe or stale control.

- Control is available iff a valid DTO has typed `RuntimeControlStatus`.
- Strategy phase is available iff a valid DTO has typed phase.
- Actuation is available iff a valid DTO has typed state; `IDLE` is available.
- Energy is available only from valid real Solis SOC/energy.
- Reserve target is available only from a complete commissioned reserve result.
- Reserve balance requires both valid energy and reserve target.

When the DTO is absent or invalid, control, phase and actuation are unavailable.
Heartbeat and health expose only the snapshot's precomputed
`diagnostic_projection_issue_code`; their properties do not re-validate or
traverse rejected input. Sensor availability never weakens T0008/T0009 health
or watchdog behavior.

## Recorder and secret boundaries

Define and enforce:

```python
MAXIMUM_DIAGNOSTIC_CODE_COUNT = 16
MAXIMUM_DIAGNOSTIC_STRING_LENGTH = 128
MAXIMUM_DIAGNOSTIC_ATTRIBUTE_BYTES = 8192
```

All projected values must be JSON-native after conversion, finite and within a
fixed key/type allowlist. Validate serialized attribute size before publishing.

- expose only the latest transition and latest result per action class;
- apply the code limit globally across all four tuples, not per tuple: remove
  duplicates within each tuple while preserving first occurrence, concatenate
  in priority order `issue`, `reconciliation`, `cancellation`, `rejected`, keep
  the first 16 and distribute them back to their original tuples;
- set `truncated_code_count` to the number of remaining unique entries omitted;
  discarded duplicates do not count as truncated;
- never silently truncate an individual code or string—an overlong value makes
  the DTO invalid;
- an attribute set that cannot fit the byte limit makes the DTO invalid;
- omit `None` keys rather than changing a key's type; and
- never use recursive `asdict`, `repr`, exception messages or generic object or
  string conversion.

Attribute tuples are `init=False` non-authoritative outputs of the one canonical
DTO builder. The builder derives them from typed fields, rejects duplicate keys,
and validates exact equality with the following projection. Callers cannot
supply or replace an attribute tuple.

The per-sensor key allowlists and fixed orders are exact; omit optional keys in
place without reordering the remainder:

- control: `schema_version`, `control_status`, `mutation_obligation`,
  `policy_status`, `active_intent_type`, `active_intent_id`, `intent_start`,
  `intent_end`, `intent_expiry`, `fresh_until`, `dynamic_control_enabled`,
  `window_classification`, `import_price`, `export_price`,
  `planned_stored_energy_kwh`, `planned_ac_energy_kwh`, `reserve_margin_kwh`,
  `incremental_value`, `source_generation`, `decision_generation`,
  `audit_digest`, `stable_digest`, `mutation_digest`, `tariff_digest`,
  `dispatch_digest`, `forecast_digest`, `reserve_digest`, `issue_codes`,
  `rejected_candidate_codes`, `truncated_code_count`;
- phase: `strategy_phase`, `active_intent_type`, `active_intent_id`,
  `intent_start`, `intent_end`, `intent_expiry`, `fresh_until`;
- actuation: `actuation_state`, `guard_state`, `guard_quality`,
  `watchdog_status`, `watchdog_fresh_until`, `transition_id`, `lease_expiry`,
  `journal_generation`, `journal_checksum`, `journal_recovery_state`,
  `cleanup_deadline`, `proof_deadline`, `desired_proof_status`,
  `ha_proof_status`, `device_proof_status`, `ha_proof_ordinal`,
  `device_proof_ordinal`, `ha_proof_revision_digest`,
  `device_proof_revision_digest`, `candidate_result`, `slot_result`,
  `cleanup_result`, `fail_safe_result`, `mapping_digest`, `policy_digest`,
  `commissioning_digest`, `mutation_digest`, `cancellation_codes`,
  `reconciliation_codes`, `truncated_code_count`;
- health: `control_status`, `actuation_state`, `guard_quality`,
  `watchdog_status`, `journal_recovery_state`, `fail_safe_result`,
  `cleanup_result`, `issue_codes`, `truncated_code_count`; and
- heartbeat: `control_status`, `actuation_state`, `fresh_until`,
  `last_healthy_at`.

Energy and reserve entities gain no diagnostic attributes in this task. No DTO
field may appear on more than the listed sensors.

When the DTO is absent, health attributes are exactly
`(("diagnostic_projection_issue_code", code),)` and heartbeat attributes are
exactly `(("last_healthy_at", value),
("diagnostic_projection_issue_code", code))`, omitting `last_healthy_at` when
absent. No other fallback key is permitted.

For each precomputed attribute tuple, build the canonical byte representation
with `json.dumps(dict(items), sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False).encode("utf-8")`. Each sensor's attributes
must independently fit `MAXIMUM_DIAGNOSTIC_ATTRIBUTE_BYTES`; no shared or
aggregate interpretation is permitted.

## Compatibility and sequencing

T0019 must be integrated before this task. T0020 first adds and freezes the
`RuntimeDiagnosticsV1` builder/snapshot field against the integrated runtime,
then cuts sensors over to it. T0020 must be integrated before T0021 deletes
legacy modules. Serialize T0020 and T0021 because both touch `sensor.py` and
legacy references.

Delete legacy sensor helpers only after an active-reference scan proves the
runtime no longer imports the abstract command or fake Solis control types.
Historical task cards and explicitly mapped legacy-simulation fixtures are
allowed. Do not delete legacy controller, mapper or simulation modules here.

## Tests

Use constructed immutable snapshots and Home Assistant's local test runtime;
do not access live Home Assistant.

Cover:

- every exact unique ID, friendly name, unit, device/state class and precision;
- a prepopulated entity registry and reload with no duplicate or `_2` entity;
- exact status, mutation, phase and actuation enum projection and precedence;
- disabled, observation-only, idle, keep, apply, HA-pending, proven, cleanup,
  fail-safe and durable-recovery snapshots;
- guard, watchdog, generation, journal, lease and proof diagnostics;
- every source-specific availability rule and invalid/absent DTO behavior;
- aware datetime and finite Decimal serialization;
- named code/string/attribute-byte limits, ordering and truncation counts;
- repeated polls and adversarial maximum input through Home Assistant's JSON
  encoder, proving constant size and no NaN, Infinity or type drift;
- adjacent adversarial token, service-payload, journal, forecast and
  1Password-looking values never appearing in state, attributes or serialized
  recorder data;
- no recursive projection, exception string or arbitrary object leakage;
- no sensor setup/property causing HA, device, store, composer or strategy
  calls;
- active-reference removal from `sensor.py` and current tests; and
- complete deployment and house-battery suites.

## Acceptance criteria

- Home Assistant exposes real guarded-runtime state rather than fake modes.
- Operators can distinguish decision, policy convergence, physical proof,
  cleanup and persistence recovery.
- Degraded or missing evidence cannot appear proven or healthy.
- Diagnostics are deterministic, byte-bounded and credential-free.
- Existing registry identities and energy semantics remain stable.
- No diagnostic path can authorize or perform a write.
- Offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0020-runtime-control-diagnostics`.
- Isolated worktree.
- Small-model implementation agent only after T0019 is integrated; DTO
  construction is the first fundamental sub-slice of this card.
- Serialize with T0021.
