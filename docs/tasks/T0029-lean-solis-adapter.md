# T0029 — Consolidate the Solis boundary

Status: Accepted

Depends on: T0028

## Objective

Replace the generic writer, transaction, policy, actuator, reader and Solis
model stack with one narrow `solis.py` adapter for the configured inverter.

Absorb and delete after migration:

- `solis_config.py`, `solis_state.py`, `solis_reader.py`;
- `solis_policy.py`, `solis_actuator.py`;
- `ha_writer.py`, `write_contracts.py`; and
- their abstraction-oriented tests.

## Public boundary

Expose:

```python
def read_state(hass, config, *, now) -> SolisState
def split_intent(intent, *, timezone, midnight_end) -> LogicalIntent
class SolisAdapter:
    def next_start_change(state, intent) -> SolisChange | None: ...
    async def apply(change, *, deadline) -> WriteResult: ...
    async def stop(slot_key, *, deadline) -> WriteResult: ...
    async def set_mode(mode, *, deadline) -> WriteResult: ...
```

`SolisState`, `SolisChange` and `WriteResult` are small adapter-owned frozen
types. `slot_key` is a validated `(physical_slot, direction)` from the configured
map, never an arbitrary entity ID. `SolisChange` contains its entity, target and
the exact captured HA state revision/precondition. `apply` compare-and-sets that
precondition and returns conflict if state drifted. The controller owns retry
policy, fail-safe and shutdown sequencing.

`next_start_change` advances one idempotent ordered change at a time:

1. Feed-In Priority;
2. Allow Grid Charging and dynamic Battery Reserve controls;
3. allocated slot time, current and target while disabled; then
4. enable as the final change.

It returns no start change while any conflicting enable is on or unknown.

## Read and write contract

Preserve:

- strict state/domain parsing and device-timestamp freshness;
- positive-charging battery-power normalization;
- extrapolated inverter clock validation;
- live current, SOC and step capabilities;
- capability quantization without hardcoded power;
- all 12 enable directions for conflict/shutdown observation;
- half-open overlap checking;
- one ordinary `asyncio.Lock`; and
- bounded HA service completion plus post-call readback.

The adapter supports only `switch`, `select`, `number`, `text` and `datetime`
services used by this inverter. Delete generic request subclasses,
`WriteTransaction`, reentrant locking and cleanup tasks.

A successful service call plus matching post-call HA state is provisional
control-plane success. Timeout, error or cancellation is ambiguous even if an
optimistic HA revision matches. A later contradictory event is ordinary state
drift and must be visible to the controller.

## Slots and midnight

- Allocate only the T0027 owner slots.
- Convert aware UTC to site-local wall time only here.
- Split a logical cross-midnight interval into two temporally adjacent segments.
- Use the configured/commissioned midnight representation. `24:00` is the
  continuous candidate. If only `23:59` is accepted live, model and test its
  one-minute gap as an explicit Solis limitation rather than adjacency.
- Validate prospective enabled intervals only; a disabled stored interval is not
  an active overlap.
- A used slot may be reset to `00:00-00:00` and `0 A` after confirmed off, but
  that housekeeping is never proof and is never a blanket sweep.

## Stop and safety behavior

`stop` resolves only a validated configured slot key already selected by the
controller. Unknown or already-off decisions stay in the controller; the adapter
never speculates.

No method performs broad cleanup, selects Self-Use after ordinary failure,
asserts a guard, owns a retry loop, or writes Peak Shaving/Grid Feed-In limits.
`set_mode(Self-Use)` is the entire mode-only fail-safe primitive.

## Tests

Consolidate Solis behavior tests into `test_solis.py` covering:

1. exact generated entity reads and malformed/unavailable states;
2. telemetry freshness, signs and inverter-clock extrapolation;
3. capability bounds and quantization;
4. same-day and split-midnight charge/discharge plans;
5. half-open adjacency and active-overlap rejection;
6. ordered one-change-at-a-time start preparation;
7. conflicting/unknown enables blocking starts;
8. enable always being the final start change;
9. service/CAS conflict, success, timeout, cancellation and later drift;
10. optimistic match accepted only after successful service completion;
11. narrow stop and mode writes; and
12. no runtime Peak Shaving, feed-limit, guard or broad cleanup write.

## Completion

- `solis.py` is the only integration module that calls Solis-related HA services.
- No production imports any absorbed module.
- The adapter is materially smaller than the absorbed stack.
- Retry/lifecycle logic exists only in the later controller.
- Focused and full local tests pass.
