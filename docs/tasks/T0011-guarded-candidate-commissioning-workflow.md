# T0011 — Build the guarded candidate commissioning workflow

Status: Implemented

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0007 candidate/fail-safe actuator;
- T0008 observation coordinator and heartbeat;
- T0009 independent watchdog.

## Objective

Implement a strict, two-step Home Assistant commissioning workflow for applying
and validating the persistent Solis candidate. The workflow is fully testable
offline, default-disabled and incapable of authorizing dynamic slots.

Live service execution, deployment and authenticated Home Assistant access are
explicitly deferred until the user provides authorization.

## Named lifetimes

Define:

```python
APPLICATION_AUTHORIZATION_LIFETIME = timedelta(minutes=10)
COMMISSIONING_OBSERVATION_WINDOW = timedelta(hours=2)
```

The first bounds the T0007 single-use candidate-application token. The second
allows supervised Reserve and Peak Shaving behavior to be observed. They are
different lifetimes and must not be conflated.

## Default-disabled configuration

Add typed commissioning configuration with:

- commissioning services disabled by default;
- no persistent candidate authorization;
- null mapping and policy fingerprints;
- null validation time;
- no capability-resolution entries;
- no slot authorization.

Service calls while disabled or before coordinator startup return structured
`BLOCKED` responses and create no session or mutation.

Pending runtime sessions are memory-only. Restart never restores one and the
default-on T0008 guard establishes fail-safe.

## Services

Register idempotently with strict schemas and response support:

- `house_battery_control.begin_candidate_commissioning`;
- `house_battery_control.validate_candidate_commissioning`;
- `house_battery_control.abort_candidate_commissioning`.

Serialize begin, validate, abort and expiry through one workflow lock. Permit only
one pending session.

All responses deterministically include status, session ID when applicable,
fingerprints, checklist, issues, evidence summary and any generated IaC snippet.

## Begin preconditions

Beginning requires:

- exact explicit confirmation phrase defined by the service schema;
- commissioning services enabled;
- coordinator completely healthy;
- guard exact `off`;
- no fail-safe obligation or pending fail-safe task;
- all 12 slot directions currently proven off;
- complete T0004 telemetry including configured power, sign and authoritative
  device timestamp;
- current mapping and candidate-policy fingerprints;
- operator attestation that the inverter Peak Shaving limit is exactly
  `MAXIMUM_GRID_IMPORT_POWER_KW`;
- exact manual-grid verification value and matching fingerprints;
- no existing commissioning session.

Create and consume the T0007 ephemeral application nonce before awaiting candidate
application so concurrent or cancelled calls cannot reuse it. The token cannot
authorize T0006.

Capture a complete before snapshot and create a random session ID. Apply the
persistent candidate through T0007; never bypass it with lower-level writes.

Candidate failure immediately invalidates the session, asserts the disable guard
and invokes bounded fail-safe.

## Active expiry

On successful candidate application, schedule an expiry callback for
`COMMISSIONING_OBSERVATION_WINDOW`.

Expiry must acquire the workflow lock, invalidate the session, assert the guard
and invoke bounded fail-safe. It is not sufficient to discover expiry only when a
later validation call arrives.

Cancel and consume the deadline callback on:

- successful validation;
- explicit abort;
- shutdown;
- session replacement;
- candidate failure;
- critical validation failure;
- guard assertion;
- coordinator degradation;
- mapping or policy fingerprint drift.

## Abort

Abort is safe and idempotent. It:

1. asserts `input_boolean.house_battery_control_disable`;
2. cancels and consumes the active deadline;
3. invalidates the session and application token;
4. invokes bounded T0007 fail-safe;
5. returns the final exact fail-safe proof and ordered issues.

Turning the guard on is an equivalent emergency abort detected by workflow
lifecycle observation.

## Validation evidence

Validation uses the same session ID and fingerprints. It requires a fresh complete
T0004 snapshot with:

- authoritative device time within `MAXIMUM_TELEMETRY_AGE`;
- valid SOC, battery power, sign and units;
- every mapped control available;
- all 12 slots conclusively disabled;
- unchanged mapping and policy fingerprints;
- inverter clock within `MAXIMUM_INVERTER_CLOCK_SKEW`.

Prove a genuinely separate post-candidate observation:

- device timestamp advanced beyond the begin snapshot;
- observation occurred after candidate application;
- explicit operator/cloud-readback attestation is supplied;
- Home Assistant state/context consistency is recorded but is not device proof.

`homeassistant.update_entity`, a changed context or optimistic HA readback alone is
never sufficient cloud/device evidence.

## Exact candidate readback

Require exact observed values for:

- `Feed-In Priority`;
- grid charging enabled;
- export enabled;
- Grid Peak Shaving enabled;
- Over-discharge `MINIMUM_SOC_PERCENT`;
- Force-charge `FORCE_CHARGE_SOC_PERCENT`;
- Recovery `MINIMUM_SOC_PERCENT`;
- Maximum charge `FULL_SOC_PERCENT`;
- Battery Reserve enabled at the candidate target;
- preserved inverter on/off state;
- preserved or separately verified global capability settings;
- all slots still disabled.

Any critical mismatch invalidates the session, asserts the guard and invokes
fail-safe.

## Capability evidence

Record post-protection current, minimum, maximum, step and unit for:

- global maximum charge/discharge current controls;
- maximum output and feed-in controls;
- every current and target-SOC number across all 12 slot directions;
- Battery Reserve and protection SOC controls.

Refreshed Home Assistant metadata is observation evidence only. Capability
resolution entries remain absent or `preserve` unless the live session supplies
separate verified maximum/unlimited evidence tied to the current fingerprints.
Never derive a resolution from a generic HA maximum.

When separate device evidence establishes the effective planner power envelope,
also record:

- maximum battery charge kW at the AC input boundary, before charge efficiency;
- maximum battery discharge kW at the AC output boundary, before stored-energy
  conversion; and
- commissioning schema, inverter identity, mapping, policy, manual-grid and
  capability fingerprints.

These fields are absent unless explicitly verified. They remain valid only while
every schema, identity and fingerprint matches. T0013 must treat missing or
mismatched evidence as unavailable and must never infer kW from amperes or generic
Home Assistant bounds.

## Required behavior outcomes

Validation collects explicit enumerated outcomes, evidence notes and observation
times for:

- Battery Reserve prevents discharge below the candidate reserve;
- Battery Reserve does not cause unwanted grid charging below its target;
- Battery Reserve behaves consistently in Feed-In Priority;
- Grid Peak Shaving remains enabled and respects the manually commissioned import
  limit during an observed load condition;
- protection settings and refreshed slot SOC bounds behave as expected;
- Feed-In Priority and all-disabled slots remain stable during observation.

Required outcomes must be `PASS`. `FAIL` or ambiguous evidence is critical and
invokes fail-safe. Missing evidence may return `PENDING_EVIDENCE` only before the
active session deadline; it can never authorize.

## Successful validation

On complete success:

- cancel the deadline;
- invalidate the runtime session and nonce;
- leave the candidate running only while guard remains off and coordinator health
  remains complete;
- return a deterministic report and IaC YAML snippet.

The generated snippet:

- contains current mapping/policy fingerprints and evidence times;
- contains exact manual-grid verification;
- includes only separately verified capability resolutions;
- includes the separately verified AC planner power envelope and its authority
  fingerprints when available;
- sets persistent candidate authorization to `false`, pending human review;
- contains no reusable ephemeral token;
- contains no T0006 slot commissioning authorization;
- performs no repository write.

Human-reviewed IaC activation and deployment are separate authenticated actions.

## Failure and lifecycle handling

Guard assertion, coordinator degradation, critical mismatch, fingerprint drift,
expiry, shutdown or unexpected workflow error invalidates the session and invokes
bounded fail-safe.

Missing non-critical behavior evidence may keep the session pending until the
deadline. It must not silently extend the deadline.

Shutdown cancels and consumes the deadline before T0008's existing shutdown
fail-safe executes. Service registration and teardown are idempotent.

Never swallow `CancelledError`; perform the established shielded bounded fail-safe
cleanup before propagation.

## Compatibility boundaries

- Do not authorize or exercise a dynamic slot.
- Do not automatically edit IaC or persist an enabled record.
- Do not treat HA optimistic state as cloud proof.
- Do not deploy, call the services or access live Home Assistant in this task.

## Tests

Use fake coordinator, reader, actuator, clock and scheduler boundaries to cover:

- disabled/pre-start structured blocking;
- strict schemas and deterministic service responses;
- concurrent begin/validate/abort serialization;
- single pending session and nonce consumption before await;
- ten-minute application token versus two-hour observation window;
- active expiry callback and cancellation on every terminal path;
- guard-on abort equivalence;
- begin preconditions and exact confirmation/manual-grid attestation;
- candidate failure invoking fail-safe;
- fresh advanced device timestamp versus optimistic HA-only changes;
- exact readback of every candidate field;
- changed mapping/policy fingerprints;
- capability metadata capture without unsafe resolution inference;
- every required Reserve and Peak Shaving behavior outcome;
- pending evidence bounded by deadline;
- critical mismatch and explicit abort final proof;
- deterministic still-disabled IaC snippet;
- absence of slot authorization/token/repository mutation;
- restart and shutdown lifecycle;
- cancellation cleanup and propagation.

Run all existing integration tests when dependencies are available.

## Acceptance criteria

- Candidate commissioning is explicit, serialized, bounded and abortable.
- No evidence set short of separate cloud attestation and complete PASS outcomes can
  emit a reviewable authorization record.
- Every terminal failure asserts the guard and invokes fail-safe.
- Generic HA metadata never becomes capability verification.
- Generated IaC remains disabled and contains no slot authority.
- Local tests pass without authentication or live writes.
- The implementation commit changes this card's status to `Implemented` while
  recording live execution as deferred pending user authentication.

## Implementation ownership

- Branch: `codex/T0011-guarded-candidate-commissioning-workflow`
- Isolated worktree.
- Small-model implementation agent after T0008 and T0009 are integrated.
- This is a serialized service/lifecycle/config task; do not overlap it with other
  coordinator or configuration implementation work.
