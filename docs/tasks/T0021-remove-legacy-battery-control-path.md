# T0021 — Remove the superseded abstract battery-control path

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0010 — obsolete SOC and actuator stubs removed;
- T0018 — deterministic physical simulation and mapped legacy-equivalence gate;
- T0019 — guarded dynamic coordinator cutover; and
- T0020 — runtime diagnostics cutover.

## Objective

Delete the old abstract command, fake Solis mapper, observation-only planner and
their deployment helpers after the real guarded strategy is active and the new
simulation proves every retained physical invariant.

This is an atomic repository cleanup, not a behavior change. It must leave one
production decision path: trusted inputs and commissioned physical evidence →
T0013/T0014/T0015 → T0017 → T0019 → T0006/T0007, with T0008/T0009 fail-safe
coverage.

It does not deploy, authenticate, commission or access live Home Assistant.

## Hard removal preconditions

Before deleting anything, prove from the integrated tree that:

- T0019 never imports, constructs or interprets an old `controller.Command` or
  fake `solis_cloud.Control`;
- T0020 projects only the typed guarded-runtime snapshot;
- no runtime actuation or diagnostic path consumes the legacy recommendation;
- T0013 consumes commissioned asymmetric power rather than the helper power
  limit;
- T0017 owns all strategy precedence, continuation and cleanup decisions; and
- every legacy simulation scenario selected by T0018 has a passing mapped
  physical-ledger equivalent, while intentionally new behavior is explicitly
  outside that equivalence set.

If any precondition is false, stop and fix its owning task rather than retaining
two active controllers.

## Remove obsolete runtime modules and vocabulary

Delete the old abstract command boundary and fake mapping:

- `controller.GridCharge`;
- `controller.ForceExport`;
- `controller.SelfConsumption`;
- `controller.Hold`;
- `controller.Command` and `controller.select_command`;
- `dependencies/solis_cloud.py` and its fake `Control`/mode mapper; and
- their focused tests and fixtures.

Delete the observation-only `Decision.command`, `CoordinatorSnapshot.control`,
legacy command target/mode diagnostics and every related import, branch and
serialization field.

The implementation must check in the following disposition table and prove it
matches the final tree. It may not make additional ad-hoc retention decisions:

| Current file/symbol | Disposition | Exact final owner |
|---|---|---|
| `battery.py:Spec`, `battery.py:State` | REMOVE | `config.InstallationModelConfig` owns stable installation facts; current energy is recomputed from T0004 `SolisStateReadResult` as required by T0014/T0017. |
| `config.BatteryConfig` | MIGRATE | Rename/split to `config.InstallationModelConfig`; no live entity IDs remain in it. |
| `BatteryConfig.to_spec` and minimum-SOC conversion | MIGRATE | `InstallationModelConfig.minimum_energy_kwh`, an exact derived property using `capacity_kwh * MINIMUM_SOC_PERCENT / 100`; configuration cannot override the named safety constant. |
| capacity, charge efficiency and discharge efficiency | RETAIN | Required fields of `InstallationModelConfig`, fingerprinted by T0017/T0019. |
| reserve uncertainty margin | MIGRATE | Required finite non-negative `InstallationModelConfig.reserve_margin_kwh`; the deployed value is migrated from the existing helper configuration and is not a live entity. |
| protected floor | MIGRATE | Exact derived `InstallationModelConfig.protected_floor_kwh = minimum_energy_kwh + reserve_margin_kwh`; T0013 and T0017 consume this value and reject it above capacity. |
| `planner.py` (`Input`, `InputInterval`, `ReserveInterval`, fusion and reserve functions) | REMOVE | T0012 trusted rates, T0013 `ReserveInputInterval`/`ReservePlanResult`, and the T0017 forecast model. |
| `tariff.py:Tariff`, `TariffInterval` | REMOVE | T0012 `AdjustedRateInterval`, `ExportRateInterval`, `CheapWindow` and trusted results. |
| `energy.py:EnergyInterval` | REMOVE | T0013 `ReserveInputInterval` and T0017 `ForecastModelInput`; no parallel generic energy DTO remains. |
| `interval.py:TimeInterval` | RETAIN | Shared exact half-open interval type used by T0012–T0015 and the new simulator. |
| `load.py` profile constants and `forecast` | MIGRATE | `runtime_inputs.py` as the single T0019 source adapter; output is the exact T0013/T0017 forecast DTO, not `EnergyInterval`. |
| `config.TariffConfig` | MIGRATE | `config.OctopusSourceConfig` with the five literal entity-ID fields below. |
| `config.SolarConfig` | MIGRATE | `config.ForecastSourceConfig.config_entry_id`; T0019 `runtime_inputs.py` owns retrieval and typed provenance. |
| `config.PolicyConfig` | REMOVE | Reserve margin moves to `InstallationModelConfig`; export hysteresis has no replacement because T0014/T0015 use exact economics and named cycle cost. |
| `config.InverterConfig` | REMOVE | Already removed by T0010; live control mapping is solely `SolisConfig`. |
| `inputs.py` (`async_read_input`, `read_decimal`, legacy HA adapters) | REMOVE | T0019 `runtime_inputs.py`, which constructs one complete source generation from exact Solis, Octopus, dispatch and forecast evidence. |
| `dependencies/octopus_energy.py` | REMOVE | `octopus_windows.py` T0012 parser/evaluator and `runtime_inputs.py`. |
| `dependencies/forecast_solar.py` | REMOVE after MIGRATE | Its validated Wh-to-kWh normalization is moved into `runtime_inputs.py` and emits T0013/T0017 types with source identity, generation, retrieval time, digest and freshness. |
| `controller.py` and `dependencies/solis_cloud.py` | REMOVE | T0017 composer and T0006/T0007 real Solis boundary. |
| legacy `tests/simulation.py` and command-specific tests | REMOVE after gate | T0018 simulator plus the retained neutral equivalence fixtures/oracles described below. |

`runtime_inputs.py` is part of T0019's selected production path. If T0019 was
implemented under another filename, migrate it to this name before this cleanup
or amend this approved table in a separately reviewed card; do not silently
change the destination during implementation.

## Exact post-cleanup configuration schema

The only non-Solis model/source configuration after this task is:

```yaml
installation:
  capacity_kwh: <positive finite Decimal>
  charge_efficiency: <finite Decimal in (0, 1]>
  discharge_efficiency: <finite Decimal in (0, 1]>
  reserve_margin_kwh: 2

octopus:
  fused_import_rates_entity_id: <sensor entity ID>
  fused_export_rates_entity_id: <sensor entity ID>
  import_rates_retrieved_entity_id: <sensor entity ID>
  export_rates_retrieved_entity_id: <sensor entity ID>
  dispatches_retrieved_entity_id: <sensor entity ID>

forecast:
  config_entry_id: <non-empty Forecast.Solar config-entry ID>

control_disable_guard_entity_id: <input_boolean or switch entity ID>
solis: <the exact T0003 SolisConfig mapping>
dynamic_control_enabled: false

candidate_commissioning:
  services_enabled: false
  persistent_candidate_authorization: null
  candidate_mapping_fingerprint: null
  candidate_policy_fingerprint: null
  candidate_validated_at: null
  manual_grid_verification: null
  capability_resolutions: []
  commissioned_power_envelope: null

production_slot_commissioning:
  production_authorized: false
  production_slot_authority: null
  direction_operating_points: []
  control_timing_budget: null
```

`InstallationModelConfig` must expose `minimum_state_of_charge_percent` only as
a read-only value derived from `MINIMUM_SOC_PERCENT`; neither YAML nor Store may
override it. `minimum_energy_kwh` and `protected_floor_kwh` are derived exact
`Decimal` properties. Stable installation/model facts are configuration. The
T0013 asymmetric `CommissionedPowerEnvelope` and its authority fingerprints
remain a T0011 commissioning record. Fixed direction currents/powers,
voltage/temperature operating envelopes, target steps and timing evidence remain
T0016 commissioning records. None of these authorities is a stable installation
fact, and they must not be conflated.

The literal `installation.reserve_margin_kwh: 2` is the deterministic offline
migration of the checked-in IaC helper's `initial: 2` kWh. Mutable live helper
state is neither available nor authoritative in this task. A later explicit
configuration change may tune the typed value, but this cleanup must prove the
exact migration and may not infer a value from Home Assistant.

`OctopusSourceConfig` binds all five entity IDs exactly. The fused records'
embedded provenance must equal these configured retrieval/dispatch IDs, and
T0019 subscribes to all five entities. `ForecastSourceConfig` binds the exact HA
config entry; `runtime_inputs.py` adds the fixed producer/family/schema identity
required by T0014 rather than accepting those identities from callers.

`dynamic_control_enabled` remains literal `false`.
`candidate_commissioning` is the exact typed T0011 IaC authority container and
`production_slot_commissioning` is the exact typed T0016 IaC authority
container. Human-reviewed activation later replaces the null/false values with
the complete versioned immutable records emitted by those workflows; this
offline cleanup preserves their integrated fields verbatim and never fabricates
evidence. T0019 requires the configured T0007 persistent candidate,
manual-grid/capability records and exact T0016 production authority, operating
points and timing evidence from these containers.

`candidate_commissioning.commissioned_power_envelope` is the complete typed
T0011 `CommissionedPowerEnvelope`, including asymmetric AC charge/discharge kW,
source/evidence identity, validation time and all mapping/manual-grid/capability
authority fingerprints. It is null until human-reviewed activation. T0013 and
T0017 consume it only when every current fingerprint matches; T0016 fixed
operating points are additionally bounded by it and do not replace it.

T0019 Store data contains only non-authoritative composer/cycle state, journal
records, transition leases and recovery bookkeeping. Store data may not satisfy
or replace any configured candidate or production authority above. Runtime
watchdog readiness is freshly inspected and may use its separately commissioned
self-test record as defined by T0019; it is not migrated into coordinator Store
authority. If the integrated T0011/T0016 implementation uses different field
paths, reconcile their cards and this reviewed schema before cleanup rather than
silently dropping or relocating authority.

The following old keys are all unknown after cleanup and each must have its own
stale-key rejection fixture:

- top-level `battery`, `tariff`, `solar`, `policy` and `inverter`;
- `battery.capacity_kwh`, `battery.minimum_state_of_charge_percent`,
  `battery.charge_efficiency`, `battery.discharge_efficiency`,
  `battery.state_of_charge_entity_id` and `battery.power_limit_entity_id`;
- `tariff.import_price_entity_id` and `tariff.export_price_entity_id`;
- `solar.config_entry_id` at its old path;
- `policy.reserve_margin_entity_id` and
  `policy.export_hysteresis_entity_id`; and
- `inverter.operating_mode_entity_id` and
  `inverter.state_of_charge_target_entity_id`.

The new parser requires every key shown in the schema, rejects missing and extra
keys at every level, and tests that the deployed configuration preserves the
existing capacity, efficiencies, reserve margin and Forecast.Solar entry while
using the named SOC safety constant.

## Remove obsolete helpers and configuration

Delete these helper entities when the integrated runtime has no reference:

- `input_number.house_battery_power_limit`;
- `input_number.house_battery_reserve_margin`; and
- `input_number.house_battery_export_hysteresis`.

Remove their configuration fields, parser branches, tests and deployed YAML:

- `battery.power_limit_entity_id`;
- `policy.reserve_margin_entity_id`;
- `policy.export_hysteresis_entity_id`; and
- the legacy `PolicyConfig` container if empty.

Stale configuration containing a removed key must fail as unknown. Keep the
named safety constants and commissioned physical/capability evidence; do not
replace removed helpers with untyped magic literals.

Ensure deployment sync metadata makes helper deletion atomic with the custom
component/config change. Historical task documentation may retain the old IDs.

## Simulation migration and deletion gate

T0018's deterministic simulator becomes the only physical controller harness.
Delete the old command-driven `tests/simulation.py` and its tests only after a
machine-readable mapping records, for every overlapping retained scenario:

- initial physical state and authoritative inputs;
- comparable time horizon;
- grid import/export and stored-energy ledger quantities;
- reserve/capacity/safety invariants; and
- the reason any intentionally changed strategy outcome is excluded.

Before deletion, check in `tests/legacy_equivalence_manifest_v1.json` with an
immutable schema version and a frozen complete inventory of every test function
and parametrized scenario ID in the legacy harness. Every ID occurs exactly
once as either:

- `OVERLAP`: exact T0018 scenario ID, normalized physical input and horizon
  fixture IDs, comparable ledger field names, invariant/oracle IDs and decimal
  equality rules; or
- `EXCLUDED`: one closed reason enum (`OBSOLETE_ABSTRACT_COMMAND`,
  `REJECTED_STRATEGY_BEHAVIOR`, or `NO_PHYSICAL_LEDGER`) plus the owning T0001
  requirement or task-card section.

No free-text exclusion is authoritative. A pre-deletion dual-harness test loads
the complete manifest, enumerates both harnesses and proves every `OVERLAP`
against independent physical ledgers/oracles. The final tree retains neutral
fixtures and independent oracle code under `tests/equivalence/`; CI enumerates
every manifest `OVERLAP`, reruns it through T0018 alone, and proves manifest
schema, inventory completeness, uniqueness and referenced fixture/oracle
existence after old `tests/simulation.py` is gone.

Version 1 permits only exact normalized `Decimal` equality for energy
conservation, stored energy, grid import/export, reserve, capacity and safety
fields; it contains no caller- or fixture-supplied numeric tolerance. If a
future proven legacy discretization cannot be normalized exactly, that requires
a separately reviewed manifest schema version with a closed field-specific
tolerance enum, a named bounded constant, zero as the default and a mandatory
rationale/owning record. This implementation may not add such a tolerance.

Endpoint behavior is compared only for the normalized physical ledger and
safety invariants named in the manifest, not obsolete commands. Deliberately
new arbitrage behavior need not match, but it must be represented by a closed
`EXCLUDED` record rather than disappearing from the inventory.

## Active-reference and single-path proof

Add a checked-in mechanical scanner covering all git-tracked executable Python,
deployed active YAML/Jinja and current tests. Python uses AST import and symbol
checks; YAML/Jinja is parsed/rendered through the existing deployment test
fixtures to inspect entity IDs, configuration keys and service calls. It proves
there are none of:

- the removed helper entity IDs or configuration keys;
- old command/fake-control class names;
- imports of the deleted mapper or controller;
- legacy command-selection calls; or
- a second path capable of selecting or applying battery behavior.

Every exception is an exact path-and-token pair for one historical task-card
occurrence, stale-key rejection fixture or the versioned equivalence manifest.
Directory-wide, file-wide and substring-class exclusions are forbidden.

Normal dynamic and commissioning writes route through T0005 and T0006/T0007.
T0019 is the only owner of *dynamic* orchestration. Default-disabled T0011 and
T0016 commissioning workflows retain their expressly scoped ownership. T0009
is the sole direct built-in-service exception: the scanner constrains it to the
guard assertion and exact fail-safe entity/service allowlist in its approved
mapping, and proves no strategy/candidate value can enter that path.

Deployment tests must also prove the existing custom-component rsync task has
`delete: yes`, so deleted Python modules disappear remotely, and the
`input_numbers` rsync likewise deletes the now-empty house-battery helper file
while preserving unrelated helper files. No broad rsync or live deployment is
performed by this task.

Because the custom-component rsync intentionally excludes `__pycache__`, add a
narrow Ansible pre-sync cleanup which finds only `__pycache__` directories below
`{{ ha_config_dir }}/custom_components/house_battery_control` and removes those
directories before the component restart. It must not search another component
or the HA config root. Deployment tests prove the cleanup precedes sync/restart,
is scoped to that exact component path, and that a deleted module's `.py` and
every corresponding `.pyc` cache form cannot survive the rendered play. Merely
asserting rsync `delete: yes` is not sufficient.

## Compatibility boundaries

- Do not change T0017 strategy precedence, economics or timing.
- Do not relax any commissioning, freshness, guard, watchdog, journal, proof or
  cleanup contract.
- Do not remove capacity, SOC-floor, efficiency or commissioned-power facts.
- Do not remove T0008 heartbeat/health, T0009 watchdog or T0020 diagnostics.
- Do not authenticate, deploy or access live Home Assistant.

“The real guarded strategy is active” in this offline card means T0019 is the
integrated and selected code path. It does not mean the default-disabled dynamic
guard is opened, commissioning is complete, or live actuation has occurred.

## Tests

Cover:

- all removed configuration keys fail closed as unknown;
- exact helper YAML deletion with unrelated inputs preserved;
- removed modules/classes/functions cannot be imported;
- coordinator and sensors contain no legacy command/control fields;
- the active-reference scan and its exact documented exceptions;
- single dynamic decision and write-path ownership;
- T0018 mapped physical equivalence before old simulation deletion;
- preservation of capacity, SOC floor, efficiencies and commissioned asymmetric
  power evidence;
- preservation of sensor registry identities and watchdog mapping;
- no changed T0017 decision result for the integrated regression corpus; and
- the complete deployment and house-battery suites.

## Deployment handoff

The later authenticated deployment must apply this cleanup in one full Ansible
run with configuration validation and the required Home Assistant restart. It
must then prove the three helpers are absent, the guarded runtime sensors are
present, the watchdog mapping resolves and no stale custom-component module was
left on disk. Record those checks but do not run them in this offline task.

## Acceptance criteria

- Exactly one guarded battery-control decision path remains.
- No active code or deployment configuration refers to fake modes, abstract
  commands or manual power/reserve/hysteresis helpers.
- New simulation coverage preserves every retained physical safety invariant.
- Authoritative installation and commissioning facts are preserved.
- Cleanup is atomic and stale configuration fails closed.
- Offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0021-remove-legacy-battery-control-path`.
- Isolated worktree.
- Small-model implementation agent after T0020 is integrated.
- Serialize with T0020 and any config/coordinator cleanup task.
