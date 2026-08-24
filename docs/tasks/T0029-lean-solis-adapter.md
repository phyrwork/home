# T0029 — Consolidate the Solis boundary

Status: Implemented

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
    def next_start_change(state, intent, *,
                          reserve_soc_percent: Decimal) -> SolisChange | None: ...
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
The explicit reserve keyword is required because the planner's dynamic reserve
is separate from a slot's target SOC, especially for charge-to-full intents.
`LogicalIntent` remains slot-only. A valid-idle `intent=None` reconciles only
mode, grid-charging permission and reserve controls; it never infers slot
cleanup. A plan with an issue is not passed to the adapter.

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

## Implementation evidence

- `solis.py` is the single Solis state/write boundary. It owns the compact
  configuration, strict reader, frozen state/revision/change/results, local
  schedule split/allocation and one-lock CAS/readback writer.
- The reader normalizes positive-charging battery power, validates device and
  extrapolated inverter time, observes live capabilities and records every one
  of the 12 enable directions, including unknown states. Legitimate adjacent
  split segments are no longer rejected merely because two enables are on.
- `next_start_change` requires the separately planned dynamic reserve SOC,
  quantizes it against the observed capability, and advances exactly one
  ordered change. Feed-In Priority, grid charging and reserve controls precede
  disabled slot time/current/SOC configuration; enable is final.
- Allocation uses only the T0027 owner map. Full logical-intent comparison
  covers every enabled segment. Unknown or conflicting enables block starts;
  disabled stored schedules never participate in overlap decisions.
- `split_intent` keeps aware UTC segments temporally adjacent. The strict
  `solis.midnight_end` setting is currently `24:00`, the continuous candidate
  requiring live commissioning. `23:59` is also modeled and tested as native
  ranges `[start, 23:59)` and `[00:00, end)`, preserving its explicit one-minute
  limitation rather than describing it as adjacent.
- `apply` performs a second exact revision/capability CAS after acquiring one
  ordinary lock. Only successful blocking service completion plus matching new
  HA state is provisional success. Timeout/error remain ambiguous even after
  an optimistic event; cancellation propagates and later drift is visible on
  the next read.
- `stop` writes only the validated requested enable switch, and `set_mode`
  writes only the requested commissioned storage mode. No broad cleanup,
  Peak-Shaving/feed-limit write, retry loop, transaction, reentrant lock or
  cleanup workflow remains.
- The temporary coordinator physical-slot bridge is deleted. The staged
  coordinator consumes `SolisAdapter` directly and advances at most one change
  per pass. T0030 remains responsible for start/stop retries, fail-safe latch,
  minimum-SOC work and the authoritative Self-Use-then-observed-on shutdown
  sequence; T0029 deliberately does not implement a partial shutdown ordering.
- Deleted `solis_config.py`, `solis_state.py`, `solis_reader.py`,
  `solis_policy.py`, `solis_actuator.py`, `ha_writer.py`, `write_contracts.py`
  and their abstraction tests. No production import of an absorbed module
  remains.
- The Solis implementation reduced from 3,152 lines across seven modules to
  1,160 lines in `solis.py`, a reduction of 1,992 lines (63.2%).
- Local verification: 74 component tests and 45 deployment tests pass;
  `compileall`, absorbed-import search and `git diff --check` pass. No auth,
  network, live Home Assistant or deployment access was used.
