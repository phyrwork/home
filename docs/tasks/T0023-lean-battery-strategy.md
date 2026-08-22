# T0023 — Lean battery strategy and full-SOC cycling

Status: Implemented

## Objective

Implement the smallest pure strategy layer needed by T0022. Given validated
inputs and the current cycle state, select exactly one action:

- `FAIL_SAFE`
- `STOP`
- `CHEAP_CHARGE`
- `PRE_DISCHARGE`
- `CYCLE_DISCHARGE`
- `IDLE`

## Scope

- Add a four-state cycle model: `IDLE`, `DISCHARGING`, `CHARGING`, `STOPPING`.
- Start a bounded full-SOC discharge only during a profitable cheap window and
  only when enough cheap-window time remains to recharge.
- Stop discharging before charging resumes.
- Give fail-safe and stop conditions priority over economic actions.
- Reuse the existing Octopus window, reserve, and pre-discharge calculations
  without adding authority records, fingerprints, journals, or simulation
  infrastructure.
- Add focused pure tests for precedence, exclusivity, cycling, malformed input,
  and reserve/SOC floors.

## Boundaries

- Do not edit coordinator, Home Assistant setup, Solis actuator, deployment YAML,
  or configuration schemas.
- Do not add backwards compatibility for the uncommissioned command model.
- Keep the public result to action, optional slot intent, next cycle state, and a
  concise reason.

## Completion criteria

- Pure tests pass.
- No charging and discharging intent can be returned together.
- The API is small enough for the coordinator cutover to consume directly.
