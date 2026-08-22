# T0002 — Define house battery control domain contracts

Status: Approved

Parent: T0001 — Solis battery arbitrage control

## Objective

Introduce a pure, typed vocabulary for the refactored house battery controller so
that later tasks can describe inverter capabilities, persistent configuration,
timed-slot intent, controller health and strategy phase without depending on Home
Assistant or the existing abstract command model.

This task must not change runtime behaviour.

## Named constants

Define the fixed policy and safety values once:

```python
FULL_SOC_PERCENT = 100
MINIMUM_SOC_PERCENT = 10
FORCE_CHARGE_SOC_PERCENT = 7
MAXIMUM_GRID_IMPORT_POWER_KW = 0.1
OFF_PEAK_CYCLE_DISCHARGE_DURATION = timedelta(minutes=10)
BATTERY_CYCLE_COST_PER_KWH = Decimal("0.0165")
```

`BATTERY_CYCLE_COST_PER_KWH` is applied to stored energy withdrawn from the
battery. Code, tests, diagnostics and later task documentation must use these
names rather than duplicate their values as literals.

## Controller state vocabulary

Add immutable enum or equivalent typed values for:

- Controller health:
  - `HEALTHY`
  - `DEGRADED`
  - `FAIL_SAFE`
- Strategy phase:
  - `OBSERVING`
  - `IDLE`
  - `PRE_DISCHARGE`
  - `OFF_PEAK_CHARGE`
  - `OFF_PEAK_CYCLE_DISCHARGE`
  - `FINAL_CHARGE`
  - `FAIL_SAFE`
- Physical slot direction:
  - `CHARGE`
  - `DISCHARGE`

## Runtime capabilities

Add an immutable type describing an observed numeric Home Assistant capability:

- current value;
- minimum;
- maximum;
- step;
- unit.

Add an aggregate runtime capability type covering:

- charge-slot current;
- discharge-slot current;
- charge-slot target SOC;
- discharge-slot target SOC;
- maximum output power;
- maximum feed-in power.

These values describe observed capability only. The type must not infer that a
generic Home Assistant maximum is a safe equipment setting.

## Persistent inverter policy

Add an immutable type that can describe:

- storage mode;
- grid-charge permission;
- export permission;
- Peak Shaving state;
- safety SOC settings;
- Battery Reserve state;
- capability-derived output and feed-in targets.

Capability-derived targets must be explicit variants such as:

- maximum verified value;
- documented unlimited value;
- preserve the current value.

Do not encode maximum or unlimited as an arbitrary numeric literal in this
domain contract.

## Timed-slot intent

Add an immutable slot-intent type containing:

- an owner identifying cheap charging, full-SOC cycling or pre-discharge;
- physical slot number;
- direction;
- timezone-aware start and end datetimes;
- current;
- target SOC;
- expiry.

Validate that:

- the physical slot is in the Solis range 1–6;
- all datetimes are timezone-aware;
- the interval is ordered and bounded;
- current is non-negative;
- SOC is a valid percentage.

The runtime reader will later refine acceptable current and SOC ranges using the
observed entity capabilities.

## Desired inverter state

Add an immutable desired-state type containing:

- the persistent policy;
- zero or one timed-slot intent;
- controller phase;
- reason;
- creation timestamp.

Its structure must make simultaneous charge and discharge slot requests
unrepresentable.

## Compatibility boundaries

- Do not change Home Assistant YAML, configuration schema, coordinator, planner,
  sensors or stub entities in this task.
- Preserve the existing command and control types until the later cutover.
- New domain code must not import Home Assistant.
- The deterministic simulation harness remains in place for later migration.

## Tests

Add focused unit tests for:

- the exact named constants, including exact `Decimal` representation;
- capability validation;
- rejection of naive or reversed slot intervals;
- rejection of invalid slot numbers, current and SOC;
- all health states, phases and slot owners;
- structural charge/discharge exclusivity;
- capability-derived target variants;
- immutable equality and value semantics.

Run all existing `house_battery_control` tests to prove that this additive change
does not alter current behaviour.

## Acceptance criteria

- Later tasks can express candidate and fail-safe policy, capabilities and
  strategy phases without the old abstract commands.
- The new types perform no Home Assistant reads or writes.
- Existing runtime behaviour is unchanged.
- New and existing integration tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0002-house-battery-domain-contracts`
- Isolated worktree.
- Small-model implementation agent.
- Expected changes are limited to new domain/constants modules, focused tests and
  this task card.
