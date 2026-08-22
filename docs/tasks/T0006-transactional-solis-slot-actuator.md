# T0006 — Implement the transactional Solis slot actuator

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0005 — Implement verified Home Assistant write primitives

## Objective

Build a fail-closed actuator for the 12 mapped Solis slot directions. It accepts
one active typed slot intent, disables every direction, configures the intended
slot while disabled and enables exactly that direction only after Home Assistant
readback succeeds.

The actuator must remain unable to enable a slot until an explicit commissioning
record matches the current Solis entity map. This task adds no strategy,
persistent inverter policy, coordinator cutover, deployment or live access.

## Named safety constant

Define:

```python
MAXIMUM_INVERTER_CLOCK_SKEW = timedelta(minutes=1)
```

Reject schedule application when the observed inverter clock differs from the
controller's local time by more than this value.

## Directional ownership

Only these owner and direction combinations are targetable:

```text
(CHEAP_CHARGING, CHARGE) -> physical slot 1
(FULL_SOC_CYCLING, DISCHARGE) -> physical slot 1
(PRE_DISCHARGE, DISCHARGE) -> physical slot 2
```

Reject every other combination. All other directions are reserved: Home Assistant
owns them for reconciliation and cleanup, but no intent may target them.

## Commissioning gate

The actuator constructor requires an immutable typed commissioning record. Its
default and restart state is uncommissioned.

Authorization requires all of:

- timezone-aware `commissioned_at` not later than the injected current time;
- exact current Solis mapping fingerprint;
- explicit Home Assistant readback validation;
- explicit Solis Cloud/device reconciliation validation;
- the expected commissioning schema version.

T0011 is the only workflow that may persist a commissioned record after live
validation. A missing, malformed, future-dated or mismatched record is
uncommissioned.

When uncommissioned, `async_apply_intent`:

- performs only best-effort disable-all cleanup;
- never writes time, current or SOC;
- never enables a direction;
- returns `BLOCKED_UNCOMMISSIONED_SAFE` only when all 12 directions are proven
  disabled;
- otherwise returns `BLOCKED_UNCOMMISSIONED_UNSAFE`.

`async_disable_all` always bypasses the commissioning gate.

## Canonical mapping fingerprint

Add deterministic canonical serialization and SHA-256 fingerprinting for the
current T0003 Solis map. Include:

- fingerprint schema version;
- telemetry IDs, explicit nulls and sign convention;
- every managed persistent, protection and capability control ID;
- manual maximum-grid-import policy;
- all 48 slot entity IDs;
- every directional owner in physical-slot order.

Canonical bytes are UTF-8 JSON with:

- sorted object keys;
- no insignificant whitespace;
- explicit null and boolean values;
- physical slots in a stable ordered array;
- Decimal policy values represented as canonical strings such as `"0.1"`, never
  binary floating-point values.

Recompute and compare the fingerprint at construction and before every intent
application. Any mapping change immediately returns the actuator to uncommissioned
behaviour.

## Intent preflight

Before acquiring a write transaction, require:

- a `HEALTHY` T0004 snapshot;
- one targetable owner/direction combination;
- intent physical slot matches the fixed allocation;
- aware `now`, start, end and expiry;
- `start <= now < end` and `now < expiry`;
- target current and SOC fit that exact physical direction's current observed
  capabilities;
- inverter local time is within `MAXIMUM_INVERTER_CLOCK_SKEW`;
- schedule wall-clock times are representable by Solis.

Only active intents are accepted. Do not program a future repeating slot early.
Return the next mandatory disable deadline as `min(end, expiry)`.

The actuator also requires the configured control-disable guard entity ID. Only
exact `off` permits slot application. Missing, `unknown`, `unavailable` or any
other value is asserted and permits cleanup only.

Reject any local interval whose repeating `HH:MM` representation is ambiguous or
nonexistent across a daylight-saving transition. Do not guess a fold.

## Schedule encoding

Convert the active aware interval to the configured inverter local timezone and
encode exact `HH:MM-HH:MM` text.

- Preserve a valid cross-midnight interval with an end earlier than its start.
- Never encode an active start equal to end.
- Never use `00:00-00:00` for an enabled direction.
- Do not alter the inverter clock automatically in this task.

## Normal application transaction

For a commissioned, valid intent:

1. Acquire the T0005 writer transaction lock.
2. Read each slot-enable switch immediately and construct a fresh per-entity
   compare-and-set precondition.
3. Disable every enabled direction across all 12 controls.
4. Re-read and prove all 12 are disabled.
5. Configure target time, current and target SOC while the target is disabled.
6. Require successful T0005 Home Assistant readback for every configured value.
7. Re-read the control-disable guard and require exact `off` immediately before
   enabling.
8. Enable only the target direction.
9. Re-read the guard after enable readback; an asserted or invalid guard triggers
   immediate all-slot cleanup.
10. Re-read all 12 enable switches and prove exactly the target is enabled.

Initial disable may use the healthy snapshot as a diagnostic hint, but every write
uses current-state compare-and-set and every post-write proof uses current state.

The normal operation returns only:

- `APPLIED_HA_PENDING_DEVICE_RECONCILIATION` when exactly the target direction is
  enabled with confirmed Home Assistant readback;
- `FAILED_SAFE` when the operation fails but all 12 directions are proven off;
- `FAILED_UNSAFE` when any direction may remain enabled or cannot be proven off.

Home Assistant readback remains an application acknowledgment, not device proof.
Production use stays gated until T0011 validates the delayed Solis Cloud/device
reconciliation behaviour.

## Emergency disable-all

Provide an independently callable `async_disable_all` that does not require:

- healthy telemetry;
- a complete T0004 snapshot;
- valid capability metadata;
- a commissioned gate.

For each of all 12 enable switches:

1. Read its current Home Assistant state.
2. Construct a fresh current-state compare-and-set precondition.
3. If on, request off through T0005.
4. Continue to the next switch regardless of an earlier expected failure.

After all attempts, re-read every switch. Report success only when every switch is
available and exactly off. An unknown, unavailable or unverified direction makes
the cleanup unsafe even if all service calls that could be issued succeeded.

## Failure cleanup

Any conflict, rejection, service error, timeout, uncertain readback or partial
application during normal apply triggers a separate best-effort disable-all
cleanup transaction.

Do not attempt to restore prior repeating schedules. The safe recovery target is
all directions disabled.

Preserve ordered write and cleanup results for diagnostics. A failed cleanup must
never be summarized as safe.

## Cancellation

T0005 propagates `asyncio.CancelledError`. If cancellation reaches T0006 at any
stage, including enable or cleanup:

1. Record the ordered results accumulated so far.
2. Start disable-all cleanup in a separate task/transaction.
3. Shield and await cleanup through repeated cancellation delivery.
4. Record whether all 12 directions were proven off.
5. Re-raise the original cancellation.

Guarantee writer-lock and listener cleanup. Do not swallow cancellation or leave
an unobserved cleanup task.

## Compatibility boundaries

- Do not select storage mode or change persistent/protection policy.
- Do not synchronize inverter time.
- Do not change coordinator, planner, strategy, sensor or deployed YAML.
- Do not invoke the legacy stub actuator.
- Do not deploy or access live Home Assistant.

## Tests

Use deterministic fake T0004 snapshots and a recording T0005 writer to cover:

- exact directional owner mapping and rejection of every invalid combination;
- uncommissioned, malformed, future-dated and fingerprint-mismatched gates;
- proof that an uncommissioned path never writes configuration or enables;
- guard missing/asserted/invalid before enable and guard change after enable;
- proof that a post-enable guard change always invokes all-slot cleanup;
- canonical serialization and stable fingerprinting;
- fingerprint change after every mapped field category changes;
- active, future, expired and expiry-bounded intents;
- exact per-direction capability checks;
- inverter-clock skew;
- normal, cross-midnight and DST-ambiguous intervals;
- exact successful write order;
- all directions disabled before any configuration write;
- exactly one final enabled direction across all 12 controls;
- failures and conflicts at every transition step;
- partial and uncertain T0005 outcomes;
- best-effort cleanup continuing across all 12 switches;
- unknown/unavailable cleanup state producing unsafe status;
- cancellation before configuration, during configuration, during enable and
  during cleanup;
- shielded cleanup completion and original cancellation propagation;
- no writes outside the T0005 transaction boundary.

Run all existing integration tests when dependencies are available.

## Acceptance criteria

- No uncommissioned or mapping-mismatched operation can enable a slot.
- Every normal transition begins with all 12 directions proven disabled.
- Only one intentional direction can be enabled.
- Every failure attempts all-direction cleanup.
- Safe status requires conclusive proof that all 12 directions are off.
- Cancellation cannot bypass cleanup.
- Mapping authorization is deterministic and invalidated by any relevant change.
- Results never confuse Home Assistant readback with inverter execution.
- Focused offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0006-transactional-solis-slot-actuator`
- Isolated worktree.
- Small-model implementation agent after T0005 is integrated.
- Expected changes are limited to slot-actuator contracts and implementation,
  deterministic mapping fingerprint support, focused tests and this card.
