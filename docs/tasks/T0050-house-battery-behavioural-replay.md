# T0050 — House-battery behavioural replay

Status: Implemented locally

Depends on: T0047

## Objective

Create a small executable acceptance-test framework that turns real debugging
incidents into permanent controller regressions. Its first scenario is the
2026-08-31 full-SOC cycling failure and the accepted rolling-pair behaviour.

This is behavioural replay, not economic backtesting. The tariff strategy is
reasoned about directly; the framework verifies that the implementation
actually produces the agreed decisions and Solis control changes under
realistic event ordering and timestamps.

## Framework contract

Each YAML scenario records:

- its incident/source and a concise purpose;
- the inverter timezone, trusted cheap interval and configured cycle duration;
- timestamped SOC and Solis device observations;
- expected action, cycle phase, deadline and logical schedule at each step; and
- expected reconciliation properties such as retained directions and enable
  order.

The runner uses the production `build_plan`, tariff-window evaluation, intent
model, local-time encoder, slot allocator and `SolisAdapter`. It carries only
the planner's transient cycle phase/deadline/gate between steps. Solis controls
are an in-memory copy of the commissioned entity surface.

Every replay also proves shared invariants:

- logical intervals are ordered, adjacent and half-open;
- every segment is inside trusted cheap authority;
- cycle directions use the commissioned owners and targets;
- a discharge ending the desired pair has enough cheap authority for its
  subsequent recharge;
- only conflicting/expired directions are rewritten; and
- recharge is enabled before discharge when a new pair is armed.

Fixtures state intended behaviour explicitly. Captured telemetry is input
evidence and never becomes an expected result merely because it happened.

## First acceptance scenario

Replay a reduced form of the 2026-08-31 incident over one 45-minute static
cheap interval with a 10-minute duty:

```text
00:00  D[00:00,00:10) + C[00:10,00:20)
00:05  unchanged
00:10  C[00:10,00:20) + D[00:20,00:30)
00:20  D[00:20,00:30) + C[00:30,00:40)
00:30  C[00:30,00:40), with no incomplete final discharge
00:40  stop cycling
```

The acceptance must fail if the implementation falls back to sequential
recharge creation, rewrites the retained phase, overlaps slots, pre-arms an
unrechargeable final discharge, or leaves cycling active after the final
recharge.

## Exclusions

Do not add raw recorder ingestion, an economic model, durable controller state,
a generic event language, or exact whole-write-stream golden files. Add only
the fields needed by an accepted incident scenario.

## Acceptance

- The first YAML replay passes through the production planner and Solis
  adapter.
- A deliberately wrong action, boundary or retained-direction expectation
  produces a focused assertion identifying the scenario step.
- Existing house-battery and deployment tests remain green.
- Documentation explains how to add the next incident fixture.

## Local result

The framework and `full_soc_cycle_2026_08_31` scenario are implemented. The
six-step replay passes through production planning, tariff-window evaluation,
UTC/local-time encoding, slot allocation, conflict projection and Solis desired
state reconciliation. It proves the initial charge-before-discharge commit,
two rolling boundary handovers, one unchanged heartbeat, final recharge-only
tail and terminal stop.

The complete house-battery suite passes with 157 tests and the deployment suite
passes with 53 tests. No live system was accessed or changed.

The replay was mutation-checked in an isolated worktree by replacing only
`controller.py`, `model.py`, `planner.py` and `solis.py` with their pre-T0047
versions from `f9f892a`, while retaining the new scenario and runner. The test
failed at step 0 because the old planner returned one discharge segment instead
of the required adjacent discharge/recharge pair. Restoring current runtime
code made the same replay pass. This proves the acceptance test detects the
specific silent implementation failure it was introduced to prevent.
