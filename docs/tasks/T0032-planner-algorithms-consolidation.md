# T0032 — Consolidate planning algorithms

Status: Accepted

Depends on: T0027
Contributes to: T0028

## Objective

Move the proven pure tariff, interval, load and reserve algorithms into
`planner.py` without changing their behavior. This is the first half of T0028.

Absorb implementation from:

- `octopus_windows.py`;
- `load.py`;
- `reserve_planner.py`;
- `energy.py`; and
- `interval.py`.

## Boundary

`planner.py` becomes the only implementation of:

- aware half-open intervals;
- fused import/export parsing, freshness, coverage and cheap-window evaluation;
- weekday/weekend household load forecast;
- reverse reserve recurrence, efficiencies, capability limits and negative PV;
  and
- compact private/result types needed by those algorithms.

Preserve exact Decimal mathematics, source binding, ordering rejection, DST gaps
and folds, coverage semantics and safety clamps.

Do not add `build_plan`, strategy selection, HA reads or controller changes in
this slice. Do not weaken validation to reduce lines.

## Transition

Existing module names may remain temporarily only as import-only re-exports from
`planner.py` so the current controller/tests stay green. They contain no logic and
are deleted by T0033. No production module may import implementation from them.

Retarget `runtime_inputs.py` to import the moved algorithms directly from
`planner.py` while preserving its public behavior. Replace the rejected T0028
wrapper: during this slice `planner.py` must not import `runtime_inputs.py` or
`strategy.py`, preventing a circular dependency.

Do not create a new wrapper that calls old implementations; implementation moves
into `planner.py` and dependency direction is one-way toward it.

## Tests

Retarget real behavior tests to `planner.py` while preserving cases for:

- rate schema, freshness, coverage, merge and margin;
- UTC/cross-midnight/DST boundaries;
- weekday/weekend load;
- reserve reverse recurrence, efficiency, surplus PV and infeasibility; and
- interval validation/proration.

Delete assertions that exist only for old result/status class identities.

## Completion

- `planner.py` owns all listed implementations.
- Old modules, if retained, are re-export-only and marked for T0033 deletion.
- `runtime_inputs.py` consumes `planner.py`; dependency never points back.
- No circular import exists.
- Production logic LOC is materially lower than the combined sources.
- Component/deployment tests, compile and diff checks pass.
- Commit from an isolated worktree; do not deploy or access credentials.
