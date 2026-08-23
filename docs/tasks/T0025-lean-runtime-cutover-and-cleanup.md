# T0025 — Lean runtime cutover and legacy cleanup

Status: Council approved

## Objective

Replace the observation-only legacy coordinator with one T0022 runtime path and
delete every uncommissioned compatibility layer once it has no executable
consumer.

## Runtime path

1. Read the configured Solis, Octopus, forecast, and guard inputs.
2. Fail safe when dynamic control is disabled or any critical input is invalid.
3. Calculate reserve and select the T0023 strategy action.
4. Apply the action through the existing verified Solis policy/actuator boundary.
5. Publish heartbeat, health, current action, reserve, and last error.

## Scope

- Replace the configuration schema outright; do not migrate uncommissioned keys.
- Add `dynamic_control_enabled`, defaulting to `false`.
- Remove runtime commissioning services, records, fingerprints, and authority
  checks.
- Retain live entity capability validation, serialized writes, disable-all before
  direction changes, readback, and bounded fail-safe cleanup.
- Treat native SolisCloud Grid Peak Shaving (enabled at 100 W) as one-time
  commissioning; do not map or write its non-authoritative HA switch at runtime.
- Delete the fake Solis dependency, abstract legacy command model, superseded
  planner/input/simulation adapters and tests after cutover.
- Remove obsolete Home Assistant helpers and configuration.
- Keep diagnostics to heartbeat, health, current action, reserve, and last error.

## Boundaries

- This is the serialized integration hotspot and is integrated only in the main
  thread after T0023 and T0024 land.
- Do not add journals, leases, bootstrap tokens, persisted commissioning state,
  general simulation, or legacy parsers.
- Keep dynamic control disabled through deployment and initial live verification.

## Completion criteria

- Focused and full integration tests pass.
- No executable import references deleted legacy modules.
- Ansible deployment succeeds.
- Live fail-safe verification succeeds before any bounded charge/discharge test.
