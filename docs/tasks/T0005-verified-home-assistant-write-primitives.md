# T0005 — Implement verified Home Assistant write primitives

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0004 — Read and validate Solis state and capabilities

## Objective

Implement serialized, typed and idempotent Home Assistant entity-write primitives
for the Solis controls. Each primitive validates an explicit state precondition,
performs at most one bounded service call and verifies a new Home Assistant state
revision.

This task provides mechanism only. It defines no inverter policy, slot transition
ordering, coordinator cutover, live access or deployment.

## Time limits

Define named finite limits:

```python
HA_SERVICE_CALL_TIMEOUT = timedelta(seconds=10)
HA_READBACK_TIMEOUT = timedelta(seconds=15)
```

A service call or readback wait must never hold the global writer lock without a
finite bound.

## Pure write contract

Add Home Assistant-free immutable types for:

- state precondition;
- domain-aware write request;
- per-entity outcome and result;
- ordered transaction result and status.

The state precondition contains exactly:

```python
@dataclass(frozen=True, slots=True)
class StatePrecondition:
    entity_id: str
    state: str
    last_updated: datetime
    context_id: str | None
```

This is a best-effort compare-and-set contract. Home Assistant provides no atomic
compare-and-set against other integrations, the UI or the inverter.

## Supported domains and target values

Support these typed mappings:

| Domain | Target | Service | Service data |
| --- | --- | --- | --- |
| `switch` | `bool` | `switch.turn_on` or `switch.turn_off` | entity ID |
| `select` | exact string | `select.select_option` | entity ID and `option` |
| `number` | finite `Decimal` | `number.set_value` | entity ID and `value` |
| `text` | exact string | `text.set_value` | entity ID and `value` |
| `datetime` | aware datetime | `datetime.set_value` | entity ID and normalized datetime |

A text request may carry a caller-supplied validator. The primitive applies that
validator but contains no Solis slot-time policy itself.

A number request must carry the exact observed capability snapshot used to choose
the target.

## Preflight and compare-and-set

Immediately before every service call, while holding the writer transaction lock:

1. Re-read the entity.
2. Require the expected domain.
3. Require it to exist and not be `unknown` or `unavailable`.
4. Compare its raw state, `last_updated` and context ID with the request's
   precondition.
5. Return `CONFLICT` without calling a service when any component differs.

The lock serializes this writer only. It does not eliminate a final race with an
external writer, so later reconciliation remains mandatory.

## Domain validation and normalization

Normalize and compare values by domain:

- switch: boolean to exact `on` or `off`;
- select: exact option string, which must be advertised;
- text: exact string after the optional request validator accepts it;
- number: numeric `Decimal` comparison rather than raw string comparison;
- datetime: one documented timezone-aware representation in the configured Home
  Assistant/inverter timezone.

The datetime primitive must not adjust inverter-clock policy or silently change
timezones. Schedule-clock policy belongs to T0006 and commissioning.

For number writes, re-read live minimum, maximum, step and unit metadata and
compare it to the supplied observed capability. Metadata drift is `CONFLICT`.
Validate exact Decimal step alignment relative to the observed minimum:

```python
(target - minimum) % step == 0
```

Reject non-finite or out-of-range values. Never clamp, round or substitute a
different target.

## Idempotence

When the preflight value already equals the normalized target, return `NO_CHANGE`
and do not call a service.

`NO_CHANGE` means only that Home Assistant already held the requested value. It
does not prove the inverter's current device state.

## Service call and readback

For a changed value:

1. Arm state-change observation so a fast update cannot be missed.
2. Recheck the compare-and-set precondition immediately before the call.
3. Make exactly one blocking Home Assistant service call bounded by
   `HA_SERVICE_CALL_TIMEOUT`.
4. Read the entity immediately after service completion.
5. If necessary, wait for observed state changes up to `HA_READBACK_TIMEOUT`.
6. Clean up listeners on every exit path.

Return `APPLIED_HA_READBACK` only when:

- the normalized state equals the requested target; and
- `last_updated` or context ID demonstrates a post-call revision.

A matching value with no post-call revision is not proof of application. A
readback timeout means the service may have applied but Home Assistant did not
confirm it. Never retry automatically.

The outcome vocabulary must distinguish at least:

- `NO_CHANGE`;
- `APPLIED_HA_READBACK`;
- `CONFLICT`;
- `REJECTED`;
- `SERVICE_ERROR`;
- `SERVICE_TIMEOUT`;
- `READBACK_TIMEOUT`.

Home Assistant readback is not inverter/device verification. Do not describe
`APPLIED_HA_READBACK` as device-confirmed.

## Serialization and transactions

One writer instance owns one asynchronous lock. It exposes a transaction context
that lets T0006 hold the lock across a complete multi-entity slot transition
without nested acquisition.

The transaction:

- serializes this writer's requests;
- preserves ordered per-entity results;
- is not atomic;
- attempts no rollback;
- stops or continues only as directed explicitly by its caller.

Transaction status distinguishes:

- `SUCCESS` when every request is `NO_CHANGE` or `APPLIED_HA_READBACK`;
- `FAILED` when no request applied and a failure occurred;
- `PARTIAL_FAILURE` when an earlier request applied or was already satisfied and a
  later request failed or remained uncertain.

T0006 must treat `PARTIAL_FAILURE` as unsafe and enter fail-safe cleanup.

## Cancellation

Never swallow `asyncio.CancelledError`. On cancellation:

- remove state listeners;
- release the writer lock;
- retain the ordered outcomes accumulated so far and mark the transaction
  incomplete for the caller;
- re-raise cancellation so T0006 or lifecycle code can invoke fail-safe cleanup.

Expected external service or readback failures are represented in results.
Programming errors may raise.

## Compatibility boundaries

- Do not implement Solis slot ordering or persistent policy.
- Do not change coordinator, planner, controller, sensor or deployed YAML.
- Do not replace or invoke the stub actuator.
- Do not access live Home Assistant or deploy.
- Do not claim device verification.

## Tests

Use deterministic fake Home Assistant states, services and state-change events to
cover:

- every supported domain and exact service payload;
- invalid domain/target combinations;
- missing, unknown and unavailable entities;
- exact idempotence with no service call;
- state, revision and context precondition conflicts;
- external change immediately before a call;
- exact select-option validation;
- finite number validation, bounds, unit and exact step alignment;
- capability-metadata drift;
- caller-supplied text validation;
- aware datetime and timezone normalization;
- service error and bounded service timeout;
- immediate readback and later event readback;
- matching value without a new revision;
- bounded readback timeout and no retry;
- lock serialization and transaction ownership;
- successful, failed and partially applied ordered transactions;
- listener and lock cleanup on cancellation with `CancelledError` propagated.

Run all existing integration tests when dependencies are available.

## Acceptance criteria

- All entity writes use one typed, serialized boundary.
- Every mutation has an immediate best-effort compare-and-set check.
- Idempotent requests perform no service call.
- Both service execution and readback are bounded.
- Number targets are never silently changed.
- Results distinguish HA readback from device verification.
- Partial and uncertain application cannot be reported as success.
- Cancellation leaves no listener or lock leak.
- Focused offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0005-verified-home-assistant-write-primitives`
- Isolated worktree.
- Small-model implementation agent after T0004 is integrated.
- Expected changes are limited to pure write contracts, the Home Assistant writer
  adapter, focused tests and this card.
