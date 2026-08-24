# T0025 — Lean runtime cutover and legacy cleanup

Status: Superseded by T0026

Historical cutover evidence below is retained, but its guard, static-enable,
broad-baseline and legacy actuator design is not current architecture.

## Objective

Replace the observation-only legacy coordinator with one T0022 runtime path and
delete every uncommissioned compatibility layer once it has no executable
consumer.

## Runtime path

1. Read the configured Solis, Octopus, forecast, and guard inputs.
2. Apply the autonomous safe baseline and report `DEGRADED` for transient
   Solis telemetry/control availability loss; recover automatically when those
   inputs validate again.
3. Report latched `FAIL_SAFE` for the manual guard, a hard safety/invariant
   failure, an unavailable critical planning input, or an unproven safe
   baseline.
4. Calculate reserve and select the T0023 strategy action.
5. Apply the action through the existing verified Solis policy/actuator boundary.
6. Publish heartbeat, health, current action, reserve, and last error.

## Scope

- Replace the configuration schema outright; do not migrate uncommissioned keys.
- Add `dynamic_control_enabled`, defaulting to `false`.
- Remove runtime commissioning services, records, fingerprints, and authority
  checks.
- Retain the live runtime boundary: telemetry, exceptional storage-mode
  transitions, grid-charging permission, inverter
  clock, Battery Reserve/reserve SOC, global charge and discharge current
  capabilities, and all six charge/discharge slots. Keep
  serialized writes, disable-all before direction changes, readback, and bounded
  fail-safe cleanup.
- The inverter datetime is sampled; extrapolate it from its Home Assistant
  `last_updated` timestamp before applying the one-minute clock-offset check.
  This keeps stale-but-freshly-telemetried samples from failing solely due to
  sample age, while preserving fail-closed behavior for invalid observations.
- Live finding: the Solis datetime sample can lag the five-minute telemetry
  cadence, so raw sampled values must not be treated as the current inverter
  clock.
- Treat Grid Peak Shaving as a one-time commissioned disabled setting. It is not
  a runtime dependency: charge slots require it disabled, while battery
  discharge/load following does not require it.
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

The independent watchdog deliberately ignores a fresh `DEGRADED` heartbeat so
temporary Solis/API loss can recover passively. It asserts the existing guard
for `FAIL_SAFE`, a stale/future heartbeat, or a health entity that remains
unavailable/unknown beyond the startup grace period. Ordinary startup and
shutdown do not latch the guard; the coordinator applies the same autonomous
safe baseline during shutdown without changing the manual latch.

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
- Current house-battery component suite: 147 tests passed.
- Ansible recap: `ok=139`, `changed=5`, `failed=0`, `unreachable=0`.
- Post-deploy `ha core check` completed successfully.
- Dynamic control is enabled and the coordinator is healthy in
  `RESERVE_DISCHARGE` with no last error.
- Initial fail-safe readback proved Self-Use, battery reserve off, and all 12
  slot directions off before activation. Native SolisCloud commissioning
  remains EMS disabled; Grid Peak Shaving remains disabled and unmanaged.
- Both fused Octopus sensors loaded with 96 current/next-day intervals.
- The commissioned Solis telemetry cadence is five minutes. SolisCloud has also
  returned successful polls with a device timestamp about 15 minutes old. The
  runtime `MAXIMUM_TELEMETRY_AGE` is therefore 30 minutes: it tolerates the
  observed delivery lag and scheduling jitter while retaining fail-safe for a
  sustained device-timestamp outage. Device timestamp validity remains
  authoritative and fail-closed.
- A second controlled restart produced no house-battery listener-removal error,
  Solis experimental-platform timeout, or Solis Cloud Control retry error.
- Native manual commissioning proved 100 A forced discharge: correcting the
  test slot from an inactive UTC-looking `19:01–19:30` to inverter-local
  `20:00–21:30` changed Octopus current demand from about +64 W import to
  −4116 W export. Runtime schedule boundaries are aware UTC instants, while
  Solis `HH:MM` fields are converted to and interpreted in the configured
  inverter/site local timezone. The coordinator currently supplies HA's
  configured `Europe/London` timezone for that boundary.
- A manual native charge slot was physically proven at 5.025 kW with Grid Peak
  Shaving disabled. Battery discharge/load following does not require Peak
  Shaving.
- Grid Feed in Power Limit is disabled as one-time SolisCloud commissioning. The
  mapped Solis Cloud Control Peak Shaving switch is not a runtime dependency.
