# T0012 — Model trusted Octopus cheap windows and cycle value

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0002 — House-battery domain contracts

## Objective

Preserve the Octopus Energy integration's standard-off-peak and Intelligent
dispatch provenance, then build a Home Assistant-free model of trusted cheap
windows and their export-cycle value.

This slice answers only:

- whether a fully covered tariff interval is explicitly classified cheap;
- whether charging during that interval and later exporting is strictly
  profitable after efficiency losses and battery wear; and
- why a requested horizon is actionable, empty or unusable.

It does not choose charge/discharge energy, SOC targets, physical slots or
strategy phases and performs no Home Assistant writes.

## Verified upstream contract

The deployed Octopus Energy integration is v18.3.3. Its public rate-event
contract has these relevant behaviours:

- private API prices are converted from pence/kWh before public rate events are
  fired;
- public `value_inc_vat` values are therefore GBP/kWh;
- Intelligent dispatch intervals are changed to the event's minimum rate and
  carry `is_intelligent_adjusted: true`;
- ordinary intervals normally omit `is_intelligent_adjusted`; and
- current-day and next-day events each carry their own minimum and tariff
  provenance.

The current fused template drops `is_intelligent_adjusted`. This task must fix
that producer before the new model can consider its data trusted.

## Named trust limits

Define these finite constants:

```python
OCTOPUS_RATE_SOURCE_MAX_AGE = timedelta(hours=26)
OCTOPUS_EXPORT_SOURCE_MAX_AGE = timedelta(hours=26)
OCTOPUS_DISPATCH_SOURCE_MAX_AGE = timedelta(minutes=10)
MAXIMUM_SOURCE_FUTURE_SKEW = timedelta(minutes=2)
OCTOPUS_RATE_UNIT = "GBP/kWh"
```

The 26-hour rate limits match v18.3.3's deliberate day-plus-two cache: the
coordinator may retain a valid `last_retrieved` while cached rates still cover
the requested horizon. Freshness never substitutes for complete coverage.

The dispatch limit covers more than three nominal three-minute dispatch polls.

## Fused import and export producers

Enhance the existing fused import-rate template and add a fused export-rate
forecast from the export MPAN's current-day and next-day public event entities.

Process current-day and next-day events independently before concatenating
them. For each source event:

1. Parse every price as an exact finite `Decimal` in `GBP/kWh`.
2. Compute the exact minimum and number of unique prices.
3. Cross-check the event's supplied minimum against the computed minimum.
4. Normalize an omitted public `is_intelligent_adjusted` to `false`.
5. Reject a present null or non-Boolean adjustment field.
6. Classify each import interval using the rules below.
7. Preserve the source event and its local timestamps before concatenation.

The fused interval schema must retain all existing fields, including
`is_capped`, and add mandatory fields for:

- canonical unit;
- Boolean `is_intelligent_adjusted`;
- explicit classification;
- source day and event entity;
- tariff code;
- per-event minimum and unique-price count; and
- source revision timestamp for diagnostics.

`source_revision_at` records when the event entity last changed. It is not an
authoritative freshness timestamp because upstream fires rate events only when
rates change.

The fused schema requires its adjustment field to be present and Boolean.
Legacy fused data that omits the field is invalid; the pure parser must never
reinterpret it as an ordinary standard interval.

The producer must retain authoritative retrieval timestamps from:

- the import and export `rates_data_last_retrieved` diagnostic entities; and
- the Intelligent device's `intelligent_dispatches_data_last_retrieved` entity.

An unavailable or malformed source produces non-actionable data rather than a
guessed classification.

## Explicit cheap classification

Define an enum with at least:

```python
STANDARD_CHEAP
BONUS_DISPATCH
NOT_CHEAP
```

For one independently validated source event:

- `STANDARD_CHEAP` requires adjustment `false`, price equal to that event's
  exact minimum, and exactly two or three unique event prices;
- `BONUS_DISPATCH` requires adjustment `true` and price equal to that same
  event's exact minimum; and
- all other consistent intervals are `NOT_CHEAP`.

A flat tariff is not classified cheap. An adjusted interval whose price is not
the event minimum is contradictory and invalidates the requested result.

Never derive cheapness from `price < maximum`, price alone or profitability.
The existing legacy `< maximum` mapper may remain compatibility-only but the
new model must not consume its Boolean.

## Pure immutable API

Add Home Assistant-free immutable types equivalent to:

```python
AdjustedRateInterval(
    start,
    end,
    import_price,
    classification,
    source,
    tariff,
    source_day,
    source_revision_at,
)

ExportRateInterval(
    start,
    end,
    export_price,
    source,
    tariff,
    retrieved_at,
)

RateSourceObservation(retrieved_at, source)
DispatchSourceObservation(retrieved_at, source)
CheapWindowComponent(rate_interval, export_interval, margin_per_stored_kwh)
CheapWindow(start, end, components)
CheapWindowResult(coverage_status, windows, diagnostic_components, issues)
```

Names may vary if the semantics remain exact.

Coverage status must distinguish:

- `COMPLETE` — one or more actionable windows over complete trusted coverage;
- `TRUSTED_EMPTY` — complete, contiguous, fresh and valid non-empty requested
  coverage with zero profitable explicitly cheap components;
- `UNAVAILABLE` — a required source or freshness observation is missing, stale
  or future;
- `GAPPED` — the requested horizon is not covered exactly once; and
- `INVALID` — values, metadata, ordering or provenance are contradictory.

Gapped and invalid results may retain diagnostic components but expose no
actionable windows.

## Source trust and coverage

Every entry point takes an injected timezone-aware `now` and an explicit
non-empty requested horizon.

Require:

- aware ordered interval timestamps;
- finite prices and efficiencies;
- exact canonical `GBP/kWh` units;
- rate-source freshness for every actionable horizon;
- dispatch-source freshness only when an actionable bonus-dispatch component
  exists;
- export-source freshness and export-price coverage for every actionable
  horizon; and
- no source timestamp more than `MAXIMUM_SOURCE_FUTURE_SKEW` in the future.

Missing or stale dispatch provenance must fail the requested result closed when
bonus components would otherwise be actionable. Do not silently drop the bonus
components and expose a partial standard-only result.

Compare ordering, overlap, adjacency and horizon coverage by UTC instant.
Retain original local timestamps and PEP 495 folds for diagnostics. Require
each requested instant to be covered exactly once by both import and export
rates.

An empty source over a non-empty requested horizon is not `TRUSTED_EMPTY`.

## Economics

For each stored kWh later withdrawn from the battery, compute with exact
`Decimal` arithmetic:

```text
export_price * discharge_efficiency
- import_price / charge_efficiency
- BATTERY_CYCLE_COST_PER_KWH
```

Require charge and discharge efficiency to be finite and greater than zero and
at most one.

An interval is an actionable component only when:

- its explicit classification is `STANDARD_CHEAP` or `BONUS_DISPATCH`; and
- its margin is strictly greater than zero.

Zero margin is not profitable. Preserve the exact margin on every component.

Adjacent actionable components may be merged into one `CheapWindow`, but retain
the original components, prices, classification and margin. Never average them
into a synthetic tariff.

Expose `value_for_stored_energy(margin, energy_kwh)` or equivalent. Do not add a
"maximum profitable energy" helper: physical energy depends on SOC, household
load, power, timing and strategy sequencing in later tasks.

Avoided household-import value is also outside this export-value calculation
and will be considered by later strategy logic.

## Compatibility boundaries

- Do not alter controller, coordinator, reserve planner, Solis reader/writer,
  slot actuator, policies or sensors.
- Do not perform live Home Assistant access, authentication or deployment.
- Do not choose slot current, target SOC, cycle energy or strategy phase.
- Preserve existing fused import fields for other consumers.
- Leave the old off-peak mapper available until the later controller cutover.

## Tests

Use deterministic pure tests and template-producer tests to cover at least:

- public event GBP/kWh handling and rejection of pence/unit mismatches;
- current and next events with different minimum prices;
- ordinary omitted adjustment normalized only at the public-event producer;
- mandatory Boolean adjustment in fused data;
- the current legacy fused schema being rejected rather than treated cheap;
- null/non-Boolean adjustment metadata;
- two-rate and three-rate standard cheap classification;
- flat tariffs not being cheap;
- adjusted true at the event minimum;
- adjusted true away from the minimum invalidating the result;
- import, export and conditional dispatch freshness and future skew;
- complete export forecast coverage rather than a current value;
- exact 7p import/15p export cycle margin with configured efficiencies;
- zero and negative margins rejected;
- adjacent component merging without price averaging;
- gaps, overlap, duplicate coverage, empty input and invalid ordering;
- `TRUSTED_EMPTY` versus unavailable/gapped/invalid;
- cross-midnight and both DST folds using UTC-instant comparisons; and
- diagnostic components never becoming actionable for gapped or invalid data.

Run all existing house-battery and deployment tests.

## Acceptance criteria

- Standard and bonus cheap periods have explicit, auditable provenance.
- The current fused-template provenance loss is removed.
- Import and export coverage and freshness fail closed.
- Profitability is exact, strict and includes both efficiency losses and
  `BATTERY_CYCLE_COST_PER_KWH`.
- No price-only or `< maximum` inference can authorize a cheap window.
- No physical scheduling decision is introduced.
- Focused and full local tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0012-trusted-octopus-cheap-window-model`
- Isolated worktree.
- Small-model implementation agent after council approval.
- Parallel-safe with T0006 because it touches only Octopus producers, pure
  tariff/window code, focused tests and this card.
