# T0028 — Consolidate battery planning

Status: Implemented by T0032 and T0033

Depends on: T0027

## Objective

Replace the planning chain with one pure-focused `planner.py` while preserving
proven tariff, forecast, reserve and strategy behavior.

Absorb and delete after migration:

- `runtime_inputs.py`;
- `strategy.py`;
- `reserve_planner.py`;
- `octopus_windows.py`;
- `load.py`;
- `energy.py`; and
- `interval.py`.

## Public boundary

Expose one operation:

```python
async def build_plan(hass, config, solis_state, *, now, cycle_state,
                     cycle_deadline) -> Plan
```

`Plan` contains only:

- action and optional `LogicalIntent` (normally one segment; T0029 may split it
  at local midnight);
- next cycle state and optional exact deadline;
- current and next trusted cheap windows;
- reserve target SOC and energy;
- actual battery energy and reserve balance;
- maximum charge/discharge power used by the calculation; and
- one concise recoverable issue when no trustworthy plan is available.

Do not expose intermediate parser/coverage/authority result hierarchies. Private
frozen dataclasses are allowed where they make the algorithms clearer.

## Required behavior

Preserve:

- fused import/export interval parsing and source freshness;
- static and Intelligent Dispatch cheap intervals, including merges;
- complete interval coverage checks;
- aware UTC comparisons, DST gaps and both folds;
- stored-energy margin after configured charge/discharge efficiency;
- forecast household load and concurrent PV without surplus carry-forward;
- reverse reserve planning to the next cheap opportunity;
- reserve margin and safety/physical-range clamps;
- live charge/discharge capability limits;
- cheap charge to full SOC;
- reserve discharge to the dynamic reserve;
- full-SOC discharge/recharge cycling with the configured duty duration;
- enough-remaining-time checks before cycling; and
- reserve target, actual energy and balance observability.

There is no pre-discharge mode. The planner returns one logical UTC interval;
T0029 owns physical slots and any local-midnight split.

Unavailable, stale, incomplete or invalid planning inputs return a recoverable
plan with no new intent. They never invent data or request cleanup/Self-Use.
An independently observed minimum-SOC discharge stop remains controller/Solis
work and does not depend on a successful plan.

## Simplification rules

- One validation path per input source.
- One representation for an aware interval.
- One reserve result, not authority/envelope/status wrappers.
- One strategy selector, not a command composer plus state machine adapter.
- No HA writes, Solis entity parsing, retry state, workflows or simulation API.
- Preserve mathematics and edge cases; reduce transport objects and duplicated
  validation rather than weakening trust checks.

## Tests

Replace module-boundary tests with `test_planner.py` behavior tests covering:

1. fused import/export parsing, freshness and coverage;
2. static/dispatch merging and positive-margin classification;
3. UTC, cross-midnight, DST-gap and DST-fold behavior;
4. load forecast and negative-load PV;
5. reserve recurrence, efficiencies, margins, capabilities and SOC clamp;
6. reserve target/actual/balance diagnostics;
7. cheap charge, reserve discharge and cycle selection;
8. cycle continuation, stop and enough-recharge-time decisions;
9. malformed/unavailable inputs returning no intent; and
10. one end-to-end case for each action.

Run the full component suite after migrating imports.

## Completion

- No production import references any absorbed module.
- Required behavior is covered through the public planner boundary.
- `planner.py` is materially smaller than the absorbed implementation.
- No tariff/reserve behavior is weakened merely to reduce lines.
- Focused and full local tests pass.
