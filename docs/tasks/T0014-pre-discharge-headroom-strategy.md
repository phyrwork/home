# T0014 — Plan pre-discharge headroom before standard cheap windows

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0012 — trusted Octopus cheap-window model;
- T0013 — commissioned reserve planner.

## Objective

Add a pure strategy calculation for the user's second headroom mechanism:
before a trusted scheduled standard-cheap window, deliberately discharge only
the energy that would otherwise prevent verified maximum charging from being
stored before the window ends.

The calculation produces an auditable decision only. It does not create Solis
requests, choose number-entity steps, mutate state or implement the later phase
composer.

## Strategy boundary

This strategy applies only to a complete trusted `STANDARD_CHEAP` window.
Never pre-discharge for `BONUS_DISPATCH`: planned Intelligent dispatches may be
removed or may arrive too late for a safe pre-window export.

Require:

- a fresh observed battery-energy value;
- a complete commissioned T0013 power envelope;
- complete aligned load and external-PV/negative-load forecasts;
- a complete feasible T0013 reserve trajectory;
- trusted import coverage over the refill window;
- trusted export coverage over the proposed discharge interval; and
- fresh, matching provenance for every source.

Any unavailable, invalid, gapped or infeasible input produces no discharge
decision.

## Fresh energy observation

Add an immutable observation containing exact stored kWh, timezone-aware
observation time, source and revision.

Use T0004's named limits:

```python
MAXIMUM_TELEMETRY_AGE
MAXIMUM_FUTURE_CLOCK_SKEW
```

The strategy decision contains:

```text
fresh_until = energy_observed_at + MAXIMUM_TELEMETRY_AGE
```

Later orchestration must obtain and revalidate a fresh observation immediately
before actuation. A matching historical decision fingerprint never authorizes a
future discharge after `fresh_until`.

## Complete input fingerprint

Create deterministic canonical serialization and a SHA-256 decision fingerprint
covering every material input:

- battery-energy observation and revision;
- capacity, minimum energy and efficiencies;
- commissioned power envelope and all authority fingerprints;
- aligned load and solar/negative-load forecast intervals;
- complete reserve trajectory;
- standard-cheap window and import components;
- discharge-time export intervals;
- all source IDs, retrieval/revision timestamps and freshness evidence; and
- strategy schema and named policy constants.

Any changed input invalidates the decision.

## Baseline forward simulation

First simulate the expected battery trajectory from `now` to the cheap-window
start without discretionary export, using the same physical boundaries as
T0013:

- ordinary grid contribution is `MAXIMUM_GRID_IMPORT_POWER_KW`;
- household deficit consumes commissioned battery AC discharge output after the
  grid contribution;
- external PV/negative load charges within the commissioned AC charge-input
  limit;
- charge and discharge efficiency are applied exactly once; and
- every energy boundary must remain at or above the T0013 reserve trajectory and
  at or below capacity.

If observed or simulated energy is below reserve, above capacity, or ordinary
demand cannot be supplied within the commissioned envelope, return
`INFEASIBLE` and allocate no discretionary export.

## Desired headroom

Maximum stored refill during the standard-cheap window is:

```text
maximum_charge_ac_input_kw
* cheap_window_duration_hours
* charge_efficiency
```

It is bounded by physical capacity and uses complete trusted import coverage.

Calculate:

```text
desired_window_start_energy_kwh = max(
    protected_floor,
    reserve_at_window_start,
    capacity_kwh - maximum_stored_refill_kwh,
)

desired_stored_withdrawal_kwh = max(
    0,
    baseline_window_start_energy_kwh - desired_window_start_energy_kwh,
)
```

When desired withdrawal is zero, return a valid `NO_HEADROOM_NEEDED` decision.

## Latest safe start and reserve overlay

Find the latest possible start by walking aligned forecast intervals backward
from the cheap-window start.

Within each interval:

- baseline household battery demand consumes part of the commissioned AC
  discharge-output limit;
- discretionary export may use only the remaining AC output;
- stored withdrawal is derived using discharge efficiency; and
- cumulative discretionary withdrawal must preserve the reserve trajectory at
  every boundary.

External PV affects the baseline battery trajectory but does not create extra
battery-inverter AC output capacity.

For a partial forecast interval, assume constant AC power within that interval
and allocate energy with exact `Decimal` proportional duration. Use that same
rule for baseline energy, reserve proof, remaining output and economics.

If the available time or reserve constraint cannot create all desired headroom,
plan only the safe reachable withdrawal and report the remainder honestly.

## Minute schedule representation

The calculated interval must be representable by T0006's exact minute-resolution
repeating schedule:

- round a calculated start earlier to the preceding minute when needed;
- require the end to be minute-exact or round it down;
- never extend discharge into the cheap window;
- reject intervals at least 24 hours long;
- reject ambiguous or nonexistent local wall times and intervals crossing a DST
  transition; and
- after any quantization, rerun the full backward allocation, reserve proof and
  economic calculation.

If rounding the end shortens available time, repeat the latest-start search. A
target-energy stop may prevent excess withdrawal after an earlier rounded start,
but the calculation must not rely on timing fiction.

## Conservative economics

Value export at the actual proposed pre-discharge interval and refill at the
future standard-cheap interval.

Require trusted export coverage across every proposed discharge instant and
trusted import coverage across refill. Compute a conservative stored-kWh margin
from every involved pair:

```text
discharge_time_export_price * discharge_efficiency
- refill_import_price / charge_efficiency
- BATTERY_CYCLE_COST_PER_KWH
```

The minimum relevant margin must be strictly greater than zero. Economic value
is:

```text
minimum_margin_per_stored_kwh * planned_stored_withdrawal_kwh
```

Do not multiply by AC export energy. Household-demand avoidance is diagnostic
upside only; discretionary capacity is authorized on conservative export value.

## Pure result contract

Add immutable decision/result types with statuses at least:

- `PLANNED`;
- `NO_HEADROOM_NEEDED`;
- `UNPROFITABLE`;
- `INFEASIBLE`;
- `UNAVAILABLE`; and
- `INVALID`.

A planned result records separately:

- desired window-start energy;
- baseline window-start energy;
- reachable window-start energy;
- `planned_stored_withdrawal_kwh`;
- `planned_ac_export_kwh = planned_stored_withdrawal_kwh * discharge_efficiency`;
- `uncreated_headroom_kwh`;
- proposed start, end and expiry;
- conservative margin and value;
- input fingerprint;
- `fresh_until`; and
- stable reason/issues.

When time is insufficient, reachable window-start energy is higher than the
desired target. Never describe this as reducing the target.

## Compatibility boundaries

- Do not emit `SlotIntent`; SOC/current capability mapping belongs to later
  composition.
- Do not change planner, controller, coordinator, sensor, Solis adapters or YAML.
- Do not access Home Assistant, authenticate or deploy.
- Do not implement ten-minute full-SOC cycling in this task.

## Tests

Use deterministic pure tests to cover at least:

- headroom overflow and no-overflow decisions;
- standard-only operation and rejection of bonus windows;
- fresh, stale, future and mismatched energy observations;
- complete input-fingerprint mutation for every source category;
- baseline load, PV/negative-load and 0.1 kW grid contribution;
- baseline reserve, capacity and commissioned-power infeasibility;
- desired versus reachable target and uncreated headroom;
- remaining AC output after household demand;
- interval-by-interval backward allocation and partial proportional intervals;
- reserve preservation at every boundary;
- asymmetric power and efficiency;
- actual discharge-time export price variation;
- refill import components and strictly positive worst-case margin;
- zero/negative margin and missing/gapped price coverage;
- insufficient-time partial withdrawal;
- both boundary quantizations and full recalculation;
- exact stored versus AC-output measurements;
- minute, cross-midnight, multi-day and DST representations; and
- expiry/freshness deadlines.

Run all house-battery and deployment tests.

## Acceptance criteria

- Pre-discharge occurs only for stable standard-cheap headroom.
- Every planned kWh is feasible under forecast power and reserve constraints.
- Partial headroom is reported without overstating the target reached.
- Actual discharge and refill prices make the plan strictly profitable.
- Stale or changed inputs cannot authorize actuation.
- The output is minute-schedule representable and DST safe.
- No Home Assistant mutation or strategy composition is introduced.
- Focused and full local tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0014-pre-discharge-headroom-strategy`
- Isolated worktree.
- Small-model implementation agent after T0012 and T0013 are integrated.
- Expected changes are new pure strategy files, focused tests and this card.
