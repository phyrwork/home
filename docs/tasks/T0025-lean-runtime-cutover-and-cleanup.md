# T0025 — Lean runtime cutover and legacy cleanup

Status: Local cleanup complete; deployment and live verification pending

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
- Retain the live runtime boundary: telemetry, storage mode, grid-charging
  permission, inverter clock, Battery Reserve/reserve SOC, global charge and
  discharge current capabilities, and all six charge/discharge slots. Keep
  serialized writes, disable-all before direction changes, readback, and bounded
  fail-safe cleanup.
- Treat native SolisCloud Grid Peak Shaving (enabled at 100 W) as one-time
  commissioning; do not map or write its non-authoritative HA switch at runtime.
- Preserve the energy diagnostics `House Battery Energy`, `House Battery Reserve
  Target`, and `House Battery Reserve Balance`; planner diagnostic failures make
  only those sensors unavailable and do not weaken a proven disabled safe state.
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

## Commissioned integration boundary

- `Solis Inverter` v4.0.1 is telemetry-only. Its experimental control surface
  was enabled and evaluated live, then disabled: it added 68 control entities,
  delayed platform setup beyond 60 seconds, initially blocked telemetry, and
  exposed optimistic grouped schedule writes without authoritative immediate
  readback or explicit slot-enable entities.
- `Solis Cloud Control` v2.21.0 remains the sole control integration for the
  MVP. Keeping experimental controls disabled prevents both integrations from
  polling and writing the same SolisCloud control surface concurrently.
- The disabled experimental entities remain in Home Assistant's registry as
  restored `unavailable` entities; they are not active runtime dependencies.

## Previous live verification — 2026-08-23

The following describes the prior deployed revision. The cleanup in this task
is local only; deployment and live verification of the resulting revision are
still pending.

- Home Assistant OS 18.2 and Core 2026.8.3.
- Full focused suite: 175 tests passed.
- Ansible recap: `ok=139`, `changed=5`, `failed=0`, `unreachable=0`.
- Post-deploy `ha core check` completed successfully.
- Dynamic control remained disabled and the local disable guard remained on.
- The coordinator converged to `healthy` / `STOP` with no last error.
- Solis readback proved Self-Use, battery reserve off, and all 12 slot directions
  off. Native SolisCloud commissioning remains EMS disabled and Grid Peak
  Shaving enabled at 100 W.
- Both fused Octopus sensors loaded with 96 current/next-day intervals.
- The commissioned Solis telemetry cadence is five minutes. The controller
  remained healthy with a device timestamp 813 seconds old, then telemetry
  refreshed normally before the 15-minute boundary. This proves the new limit
  avoids the former boundary oscillation while retaining fail-safe after three
  missed poll intervals.
- A second controlled restart produced no house-battery listener-removal error,
  Solis experimental-platform timeout, or Solis Cloud Control retry error.
