# T0033 — Cut over the planning runtime

Status: Accepted

Depends on: T0032
Completes: T0028

## Objective

Move HA input collection and pure strategy selection into `planner.py`, cut the
current coordinator to its `build_plan` boundary, and delete every superseded
planning module/re-export.

## Public API

```python
async def build_plan(hass, config, solis_state, *, now, cycle_state,
                     cycle_deadline) -> Plan
```

`Plan` follows T0028 and returns `LogicalIntent | None`; it never exposes a
physical slot. `intent is None` with no issue is a valid idle plan and tells the
controller that an owned enabled action is no longer desired. `intent is None`
with an issue means planning is unavailable and preserves an existing bounded
action except for independently provable stop conditions. This distinction
replaces planner-level `STOP`. Planning/input failures never select Self-Use.

## Migration

Absorb `runtime_inputs.py` and `strategy.py` into `planner.py`:

- HA tariff/dispatch/forecast and cycle-duration reads;
- capability-derived power and reserve-target quantization;
- cheap charge, reserve discharge and full-SOC cycle selection;
- exact cycle/cheap deadlines and reserve observability; and
- validation at external reads plus one typed plan boundary.

Adapt the transitional coordinator to read Solis once and call `build_plan`.
When `Plan.issue` is present, publish `DEGRADED`, preserve the current bounded
native action and cycle state/deadline, and perform no policy, slot or mode write.
A valid idle plan is separate and may reconcile no active intent. Existing hard
Solis/invariant handling may remain transitional until T0030; do not add retry or
fail-safe timing here.

Delete `runtime_inputs.py`, `strategy.py`, `octopus_windows.py`, `load.py`,
`reserve_planner.py`, `energy.py`, `interval.py`, their compatibility re-exports,
and abstraction-only tests.

Remove transitional planner model debt now:

- `StrategyAction.FAIL_SAFE` and `STOP`;
- rename `CycleState.DISCHARGING` to `CYCLE_DISCHARGING`;
- planner imports only `model.SlotIntent`/`LogicalIntent`, never legacy
  `contracts.SlotIntent`; and
- planner-output tests prove aware UTC and no physical slot leakage.

## Tests

Consolidate into `test_planner.py` and prove all T0028 behavior plus:

- input unavailable/stale/incomplete returns no intent;
- all three actions through `build_plan`;
- cycle transition and enough-recharge-time behavior;
- reserve target/actual/balance output;
- exact boundary output;
- model-only logical intents; and
- transitional coordinator consumes `Plan` without old imports.

## Completion

- T0028 is fully satisfied; no absorbed module/import remains.
- One plan failure representation, interval representation and selector remain.
- Planner is materially smaller than the seven replaced modules.
- Component/deployment tests, compile and diff checks pass.
- Commit from an isolated worktree; do not deploy or access credentials.
