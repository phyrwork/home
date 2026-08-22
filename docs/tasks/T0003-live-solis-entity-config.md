# T0003 — Define the live Solis entity configuration

Status: Implemented

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0002 — Define house battery control domain contracts

## Objective

Add a strict, typed and additive configuration contract for the real Solis
telemetry and control entities. Configure every known Garage inverter entity and
represent unresolved live facts honestly, while retaining the legacy stub
configuration so this task makes no runtime reads, writes or behavioural cutover.

This task must be fully implementable and testable offline.

## Configuration shape

Add an optional `solis` section to the top-level configuration. Existing config
without this section must continue to parse until the later cutover.

The section contains immutable typed configuration for:

- telemetry entities and unresolved telemetry metadata;
- persistent, protection and capability control entities;
- the manual maximum-grid-import commissioning requirement;
- six complete physical slot groups and their intentional MVP ownership.

The deployed YAML must include this new section while preserving the legacy
`battery` and `inverter` stub fields.

## Telemetry mapping

Configure the known authoritative SOC entity:

```text
sensor.garage_inverter_telemetry_garage_inverter_remaining_battery_capacity
```

The telemetry configuration must also be able to represent:

- signed battery-power entity ID;
- whether positive battery power means charging or discharging;
- an optional inverter/device timestamp entity ID.

The power entity, its sign convention and the device timestamp are unresolved
live facts, so the deployed configuration must contain explicit null values for
them. Do not invent entity IDs or assume a sign convention.

Home Assistant `last_updated` remains an observation timestamp candidate even
when no device timestamp exists. T0004 must validate its suitability and age.

T0004 must treat any unresolved, unavailable, unknown, stale or ambiguous required
telemetry as degraded and prohibit healthy actuation. The control integration is
not a fallback telemetry source.

Both installed integrations therefore remain part of the MVP:

- Solis telemetry supplies authoritative operating observations;
- Solis Cloud Control supplies control state and actuation entities.

## Persistent and protection control mapping

Map these exact managed controls from T0001 Appendix A:

- `select.garage_inverter_control_storage_mode`;
- `switch.garage_inverter_control_allow_grid_charging`;
- `switch.garage_inverter_control_allow_export`;
- `switch.garage_inverter_control_grid_peak_shaving`;
- `switch.garage_inverter_control_inverter_on_off`;
- `datetime.garage_inverter_control_inverter_time`;
- `number.garage_inverter_control_battery_over_discharge_soc`;
- `number.garage_inverter_control_battery_force_charge_soc`;
- `number.garage_inverter_control_battery_recovery_soc`;
- `number.garage_inverter_control_battery_max_charge_soc`;
- `switch.garage_inverter_control_battery_reserve`;
- `number.garage_inverter_control_battery_reserve_soc`;
- `number.garage_inverter_control_battery_max_charge_current`;
- `number.garage_inverter_control_battery_max_discharge_current`;
- `number.garage_inverter_control_max_output_power`;
- `number.garage_inverter_control_max_export_power`.

Do not map MPPT controls or export calibration into the managed contract. They
remain explicitly outside the controller's ownership.

T0004 must extend the runtime capability aggregate to cover the mapped global
maximum charge and discharge currents as well as the existing slot, SOC, output
and feed-in capabilities.

## Maximum grid import

Represent the Peak Shaving maximum-grid-import limit as the literal policy
`manual_commissioning`. There is no Home Assistant entity for this setting in
Solis Cloud Control v2.21.0.

The schema must not accept a helper or entity ID in its place. The controller must
never attempt an entity service call for this setting. Commissioning must verify
separately that the inverter contains `MAXIMUM_GRID_IMPORT_POWER_KW`.

## Physical slots

Add an immutable slot configuration for every physical slot 1 through 6. Each
slot maps all eight entities:

- charge enable switch;
- charge time text;
- charge current number;
- charge target-SOC number;
- discharge enable switch;
- discharge time text;
- discharge current number;
- discharge target-SOC number.

Use the exact `garage_inverter_control_slot{n}_...` entity patterns recorded in
T0001 Appendix A, yielding 48 explicitly configured slot entities.

The intentional MVP physical allocation is:

- charge slot 1: cheap charging;
- discharge slot 1: full-SOC off-peak cycling;
- discharge slot 2: pre-discharge;
- all other charge and discharge directions: reserved.

This allocation is a controller design decision, not an inferred Solis
capability. Reserved slots remain fully mapped and owned by Home Assistant for
cleanup and reconciliation, but this task performs no writes and never enables a
slot.

T0004 must treat an unexpectedly enabled reserved slot as degraded and must block
healthy operation until the later actuator can reconcile it safely.

## Offline validation

At configuration load time, validate:

- the expected Home Assistant domain for every entity ID;
- exactly one complete slot group for each physical slot 1 through 6;
- no missing, duplicate or out-of-range physical slots;
- global uniqueness of every managed entity ID;
- the exact intentional owner allocation;
- power sign is null when power telemetry is unresolved, or one of the explicit
  charging/discharging meanings when configured;
- device timestamp is either null or a sensor entity;
- the grid-import policy is exactly `manual_commissioning`;
- no legacy stub entity is present in the new Solis map.

Entity existence, integration/config-entry provenance, state availability,
select options, text values, number bounds, units, device classes, freshness and
current slot states are runtime concerns for T0004 and commissioning.

## Compatibility boundaries

- Preserve the legacy battery SOC, shared power-limit, operating-mode and target
  SOC configuration fields.
- Do not change the coordinator, input reader, planner, sensor or actuator.
- Do not call Home Assistant services or inspect live Home Assistant state.
- Do not delete helper entities.
- Do not deploy or write to the inverter.

## Tests

Add focused tests covering:

- existing configuration without `solis` still parses;
- the deployed YAML contains a valid Solis mapping;
- all 48 slot entities and all persistent controls are represented;
- wrong entity domains are rejected;
- duplicate managed entity IDs are rejected globally;
- missing, duplicate, out-of-range or incomplete slot groups are rejected;
- incorrect slot-owner allocation is rejected;
- unresolved power, sign and timestamp values are explicit and valid;
- a configured sign convention is restricted to the defined meanings;
- the manual grid-import policy cannot be replaced by an entity;
- legacy stub IDs are rejected inside the Solis mapping;
- the schema performs no Home Assistant imports, reads or writes.

Run all existing `house_battery_control` tests when dependencies are available.

## Acceptance criteria

- Every future Solis read or write has one typed configuration source.
- The deployed map contains known real entities and explicit nulls for unresolved
  live telemetry facts; it contains no invented entity.
- Both Solis integrations retain separate, unambiguous responsibilities.
- All physical slots are mapped, owned and assigned intentionally.
- Missing power/sign/time information is documented as a T0004 actuation blocker.
- Current stub-driven runtime behaviour remains unchanged.
- Offline validation and focused tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0003-live-solis-entity-config`
- Isolated worktree.
- Small-model implementation agent.
- Expected changes are limited to configuration types/parsing, deployed YAML,
  focused tests and this card.
