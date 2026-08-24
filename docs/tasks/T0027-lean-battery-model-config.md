# T0027 — Lean battery model and configuration

Status: Accepted

Depends on: T0026

## Objective

Establish the small shared model and compact strict configuration used by the
T0026 rewrite. Remove the manual guard and static enable flag end to end.

This slice changes no Solis write behavior and must leave the existing component
and test collection importable while later slices are unfinished.

## Model

Create `model.py` containing only shared behavior types:

- `ControllerHealth`: healthy, degraded, fail-safe;
- `StrategyAction`: idle, cheap charge, reserve discharge, cycle discharge;
- `CycleState`: idle, reserve discharging, cycle discharging, charging, stopping;
- `SlotDirection`: charge or discharge;
- `SlotOwner`: cheap charge, reserve discharge, full-SOC cycle;
- `StorageMode`: Feed-In Priority or Self-Use;
- `Capability`: observed minimum, maximum and step; and
- `SlotIntent`: one owner/direction segment with aware UTC start/end/expiry,
  current and target SOC; and
- `LogicalIntent`: one or two ordered, adjacent, non-overlapping `SlotIntent`
  segments with the same owner, direction, current and target SOC.

Each segment must be finite, half-open, ordered, timezone-aware and bounded by
expiry. The planner normally returns a one-segment logical intent. Physical-slot
allocation, local-midnight splitting into a two-segment logical intent, and local
time encoding belong only to `solis.py` in T0029.

Do not add logical-workflow, fingerprint, journal, authority, transaction or
retry types.

## Configuration

Keep only:

- battery capacity, `MINIMUM_SOC_PERCENT`, efficiencies and reserve margin;
- fused import/export rate entity IDs;
- forecast config-entry ID;
- cycle-duration helper entity ID;
- Solis telemetry, persistent, reserve and capability entity IDs; and
- one compact slot map.

The slot map contains:

- entity prefix `garage_inverter_control` and six physical slots;
- cheap-charge allocation: charge directions 1 and 2 (preserving slot 1);
- full-SOC-cycle allocation: discharge directions 1 and 3 (preserving slot 1);
- reserve-discharge allocation: discharge directions 2 and 4 (preserving slot
  2); and
- every generated charge/discharge direction remains readable for conflict and
  shutdown handling, including unallocated directions.

Generate the exact existing entity IDs from that pattern; do not duplicate 48
slot field IDs in YAML. Validate every generated ID and allocation for domain,
range, uniqueness and direction.

Remove rather than deprecate:

- `dynamic_control_enabled`;
- `control_disable_guard_entity_id`;
- their YAML/helper/test references; and
- any compatibility behavior for those uncommissioned fields.

Unknown configuration keys fail validation. Use Home Assistant's configured site
timezone; do not duplicate it in this integration's YAML.

## Transition rule

The second slot in each allocation is a deliberate extension for split-midnight
operation; the two time segments are adjacent but their physical slot numbers
need not be. Every existing primary owner remains on its commissioned physical
slot. Until T0030 cuts over the controller, update old call sites only enough to
use the new shared types and treat control as operational. Do not add
compatibility properties to `Config` and do not deploy this intermediate commit.

## Tests

Prove:

1. the commissioned compact configuration parses;
2. generated slot entity IDs exactly match all current 48 slot controls;
3. owner allocations are unique and valid;
4. guard/static-enable/unknown keys are rejected;
5. entity domains and battery safety values are strict;
6. one- and two-segment logical intents validate;
7. adjacency passes while overlap, gaps, mixed owners/directions/values, naïve,
   reversed, non-finite and out-of-range values fail; and
8. the full component test collection imports and passes.

## Completion

- `model.py` is the single source for the listed shared types.
- No source, YAML or test references the guard or static enable flag.
- Repeated slot YAML is gone and exact mappings remain test-proven.
- No Solis service behavior changed.
- Focused and full local tests pass.
