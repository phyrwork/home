# T0025 — Lean runtime cutover and legacy cleanup

Status: Live cutover verified; dynamic control healthy

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
- The inverter datetime is sampled; extrapolate it from its Home Assistant
  `last_updated` timestamp before applying the one-minute clock-offset check.
  This keeps stale-but-freshly-telemetried samples from failing solely due to
  sample age, while preserving fail-closed behavior for invalid observations.
- Live finding: the Solis datetime sample can lag the five-minute telemetry
  cadence, so raw sampled values must not be treated as the current inverter
  clock.
- Treat native SolisCloud Grid Peak Shaving (enabled at 100 W) as one-time
  commissioning; do not map or write its non-authoritative HA switch at runtime.
- Preserve the energy diagnostics `House Battery Energy`, `House Battery Reserve
  Target`, and `House Battery Reserve Balance`; planner diagnostic failures make
  only those sensors unavailable and do not weaken a proven disabled safe state.
- Delete the fake Solis dependency, abstract legacy command model, superseded
  planner/input/simulation adapters and tests after cutover.
- Remove obsolete Home Assistant helpers and configuration.
- Keep diagnostics to heartbeat, health, current action, reserve, and last error.

## Helper ownership

- External IaC owns the control-disable guard.
- The cycle-duration helper, watchdog/script, and fused Octopus sensors remain
  external inputs.
- Coordinator diagnostics remain owned by this integration.
- The explicit slot list is intentionally duplicated for safety independence;
  its coverage and reconciliation are tested.

The independent watchdog deliberately uses minute polling plus health and guard
state changes, rather than triggering on every heartbeat state update. Home
Assistant can publish the heartbeat entity before the coordinator publishes its
corresponding health state; a heartbeat-triggered run can therefore observe the
previous `fail_safe` health and reassert the guard during an otherwise healthy
activation. Minute polling still detects a stale or future heartbeat within the
existing bounded watchdog interval, while the health trigger handles faults as
soon as the health state is published.

## Boundaries

- This is the serialized integration hotspot and is integrated only in the main
  thread after T0023 and T0024 land.
- Do not add journals, leases, bootstrap tokens, persisted commissioning state,
  general simulation, or legacy parsers.
- Keep dynamic control disabled through the initial deployment and live
  fail-safe verification. It is now enabled after explicit operator approval;
  the controller is live and healthy in `RESERVE_DISCHARGE`.

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

## Live verification — 2026-08-23

- Home Assistant OS 18.2 and Core 2026.8.3.
- Full focused suite: 175 tests passed.
- Ansible recap: `ok=139`, `changed=5`, `failed=0`, `unreachable=0`.
- Post-deploy `ha core check` completed successfully.
- Dynamic control is enabled and the coordinator is healthy in
  `RESERVE_DISCHARGE` with no last error.
- Solis readback proved Self-Use, battery reserve off, and all 12 slot directions
  off. Native SolisCloud commissioning remains EMS disabled and Grid Peak
  Shaving enabled at 100 W.
- Both fused Octopus sensors loaded with 96 current/next-day intervals.
- The commissioned Solis telemetry cadence is five minutes. SolisCloud has also
  returned successful polls with a device timestamp about 15 minutes old. The
  runtime `MAXIMUM_TELEMETRY_AGE` is therefore 30 minutes: it tolerates the
  observed delivery lag and scheduling jitter while retaining fail-safe for a
  sustained device-timestamp outage. Device timestamp validity remains
  authoritative and fail-closed.
- A second controlled restart produced no house-battery listener-removal error,
  Solis experimental-platform timeout, or Solis Cloud Control retry error.
- Slot 2 forced discharge is live from 16:54–22:30 at 100 A with a 19% target;
  schedule times use the inverter's authoritative UTC datetime.
- Solis battery power −4917 W and Octopus current demand −4104 W confirmed
  forced export. Grid Peak Shaving at 100 W is compatible; the actual blocker
  was native Grid Feed in Power Limit at 0 W / 0 A, corrected to 9900 W / 52 A.
- The HA export/peak-shaving switches are non-authoritative/unavailable, so
  these persistent settings remain one-time SolisCloud commissioning.
