# T0013 — Adapt reserve planning to trusted cheap periods

Status: Implemented

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0011 — guarded candidate commissioning workflow;
- T0012 — trusted Octopus cheap-window model.

## Objective

Add a new pure reserve-planning path that uses trusted Octopus import
classifications, a fingerprint-bound commissioned AC power envelope and the
ordinary Peak Shaving grid contribution.

Preserve the existing reverse-planning model and legacy entry points until the
later coordinator cutover. This slice computes exact energy requirements only;
it does not write Battery Reserve SOC, choose a strategy or actuate a slot.

## Import trust is separate from export profitability

T0012's `TRUSTED_EMPTY` status means no explicitly cheap component is profitable
for an export cycle. It does not mean no cheap charging opportunity exists.

The new planner consumes a separate, complete trusted import-classification
view retained by T0012:

- `STANDARD_CHEAP` can replenish energy needed for later household demand;
- `BONUS_DISPATCH` can do the same only with an independently fresh dispatch
  observation; and
- `NOT_CHEAP` is not a grid-recharge interval.

Export-source availability and export-cycle margin do not gate household reserve
planning.

Never authorize recharge from:

- gapped or invalid import coverage;
- diagnostic-only or partial intervals;
- a legacy `< maximum` off-peak Boolean; or
- a bonus interval whose dispatch observation is missing, future or older than
  `OCTOPUS_DISPATCH_SOURCE_MAX_AGE`.

The import classification view must cover the complete requested planning
horizon exactly once.

## Commissioned power envelope

Add an immutable `CommissionedPowerEnvelope` or equivalent containing:

- maximum battery charge power in kW at the AC input boundary, before charge
  efficiency;
- maximum battery discharge power in kW at the AC output boundary, before
  converting delivered demand to stored-energy demand;
- the exact ordinary maximum grid-import contribution;
- commissioning schema version;
- inverter identity;
- mapping fingerprint;
- candidate-policy fingerprint;
- manual-grid fingerprint;
- capability fingerprint; and
- evidence source and timezone-aware validation time.

The ordinary grid-import value must equal
`MAXIMUM_GRID_IMPORT_POWER_KW`. Do not hardcode 5 kW, convert amperes to kW,
trust generic HA maximum metadata or substitute a different value.

The caller supplies the current schema, inverter identity and all current
fingerprints. Every value must match exactly. The commissioned physical evidence
remains valid only until any schema, identity or fingerprint changes. A missing
record, missing verified kW value or mismatch returns `UNAVAILABLE`.

T0011 must emit these two verified AC power values and their measurement-boundary
semantics only when the live commissioning session supplies separate device
evidence. Generic Home Assistant bounds remain observation-only.

## Pure result contract

Add immutable types for:

- commissioned/current authority inputs;
- ordered reserve input intervals;
- reserve trajectory intervals;
- stable issues; and
- a result with status and zero or one complete trajectory.

Status must distinguish:

- `COMPLETE` — a full feasible trajectory;
- `INFEASIBLE` — trusted inputs prove the demand cannot be met;
- `UNAVAILABLE` — required trust, commissioning or freshness evidence is absent;
  and
- `INVALID` — numeric, interval or provenance inputs are malformed or
  contradictory.

Expected bad input is represented by a result and issue rather than an arithmetic
exception. Programming errors may still raise.

## Input validation

Validate before recurrence:

- capacity and minimum energy are finite, non-negative and ordered;
- reserve margin is finite and non-negative;
- charge and discharge efficiencies are finite, greater than zero and at most
  one;
- commissioned AC powers are finite and non-negative;
- ordinary grid import is finite and exactly the named constant;
- all timestamps are aware;
- intervals are ordered, positive-duration, contiguous and cover the exact
  requested horizon;
- load and solar energy are finite; and
- trusted import classifications and their required freshness evidence are
  complete.

Invalid values return `INVALID`. Do not clamp, round or silently replace them.

## Deterministic reverse recurrence

Define the protected trajectory floor exactly as:

```text
minimum_energy_kwh + reserve_margin_kwh
```

The terminal boundary and every minimum trajectory boundary use this floor.
If the floor exceeds capacity, the plan is `INFEASIBLE`.

Reverse across the complete contiguous requested horizon.

### Explicitly cheap interval

For trusted `STANDARD_CHEAP`, or trusted and fresh `BONUS_DISPATCH`, potential
stored recharge is:

```text
maximum_charge_ac_input_kw * duration_hours * charge_efficiency
```

Subtract it from the next boundary requirement, never below the protected
floor. Cheap import may supply household demand, so do not add that demand to the
battery reserve recurrence.

External PV and scheduled grid charge share the same physical battery charge
limit. Do not add separate recharge beyond that AC input cap.

### Non-cheap deficit

Let deficit be forecast household load minus forecast solar/negative-load
surplus for the interval.

Ordinary grid energy contributes:

```text
min(deficit_kwh, MAXIMUM_GRID_IMPORT_POWER_KW * duration_hours)
```

The remaining AC demand must be delivered by the battery. If its implied AC
output power exceeds the commissioned discharge limit, the plan is
`INFEASIBLE`. Add stored-energy requirement as:

```text
remaining_ac_output_kwh / discharge_efficiency
```

If any reserve boundary exceeds battery capacity, return `INFEASIBLE`; never
clamp it to capacity and report success.

### Non-cheap surplus

Unmonitored external PV may appear as negative load. Preserve the validated
assumption that this surplus can charge the battery.

Potential stored energy is:

```text
min(surplus_kwh, maximum_charge_ac_input_kw * duration_hours)
* charge_efficiency
```

Subtract it from the next boundary requirement, never below the protected floor.

## Output boundary

Return exact `Decimal` kWh at every boundary. SOC conversion, upward capability
step rounding and mapping to Solis Battery Reserve belong to T0007/later
coordinator work.

Do not include deliberate arbitrage export, pre-discharge headroom or ten-minute
cycling in this slice. Those strategies consume the reserve trajectory later.

## Compatibility boundaries

- Keep legacy planner entry points and their tests operational until cutover.
- Do not change Home Assistant YAML, config parsing, coordinator, controller,
  sensors, Solis adapters or actuators.
- Do not access live Home Assistant, authenticate or deploy.
- Do not infer power from current, voltage or generic entity bounds.

## Tests

Use deterministic pure tests to cover at least:

- exact commissioned schema/identity/fingerprint matches and every mismatch;
- absent AC power evidence;
- charge power as pre-efficiency AC input;
- discharge power as delivered AC output;
- asymmetric charge and discharge power;
- exact 0.1 kW grid contribution;
- deficits below, equal to and above the grid allowance;
- discharge-power infeasibility;
- standard cheap recharge independent of export profitability;
- `TRUSTED_EMPTY` export result with retained trusted cheap classification;
- bonus recharge with fresh, stale, future and absent dispatch evidence;
- import gaps, invalid and diagnostic-only intervals;
- cheap recharge bounded by AC charge power;
- non-cheap PV/negative-load surplus and the shared charge cap;
- protected floor, terminal boundary and margin;
- floor or any reverse boundary above capacity;
- invalid capacity, minimum, margin, efficiency, power and energy values;
- interval ordering, coverage and contiguity;
- cross-midnight and DST-fold horizons by UTC instant;
- exact reserve trajectory recurrence; and
- unchanged legacy planner behaviour.

Run the complete house-battery and deployment test suites.

## Acceptance criteria

- Reserve planning never uses export profitability as cheap-import trust.
- Bonus recharge always has independent fresh dispatch evidence.
- Power and efficiency boundaries cannot double-count losses.
- Commissioned power becomes unavailable on any authority mismatch.
- Ordinary grid contribution is exactly the named 0.1 kW constant.
- Impossible demand is reported infeasible, never hidden by capacity clamping.
- External PV/negative load remains usable within the commissioned charge cap.
- Existing planner callers remain deployable until cutover.
- Focused and full local tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0013-commissioned-reserve-planner`
- Isolated worktree.
- Small-model implementation agent after T0012 is integrated.
- Serialize with T0012 because both touch tariff/planner domain boundaries.
