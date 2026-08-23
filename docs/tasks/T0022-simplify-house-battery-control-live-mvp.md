# T0022 — Simplify house-battery control to a live-verified MVP

Status: Live verified

## Objective

Replace speculative commissioning, legacy compatibility, and unused abstraction
with the smallest safe control path that can be deployed and verified against the
installed Solis inverter:

1. read trusted Home Assistant inputs;
2. calculate the reserve and select one strategy action;
3. apply that action through the verified Solis controls; and
4. fail safe to Self-Use with all timed charge and discharge slots disabled.

## Installation and commissioning model

Commissioning is a one-time operator activity, not a runtime subsystem.

- Configure persistent Solis settings through SolisCloud or Home Assistant using
  the integrated browser.
- Inspect the real entity capabilities, ranges, options, and resulting inverter
  behaviour in place.
- Record the resulting settings and real entity mapping in IaC documentation and
  configuration.
- Do not create commissioning services, tokens, evidence records, fingerprints,
  authority objects, or a persisted commissioning state machine.
- Dynamic control is enabled only after live verification and explicit operator
  approval; the commissioned system is now live and healthy in
  `RESERVE_DISCHARGE`.

The documented configuration is the source of truth. If the inverter or
integration is replaced, the operator repeats this procedure and updates IaC.

### Commissioned SolisCloud baseline

- EMS is disabled for the plant so SolisCloud does not dispatch or overwrite
  inverter controls.
- Maximum inverter output is set to the installed entity's maximum value.
- Native charge and discharge slots were disabled before deployment. The
  runtime now owns them; SolisCloud was used for authoritative live readback.

## Keep

- Real Solis entity configuration, reader, and normalized state.
- Octopus cheap/dispatch-window input handling.
- Reserve planning, reserve-export and full-SOC cycling calculations needed by
  the agreed strategy.
- Transactional, read-after-write Solis actuation and bounded fail-safe cleanup.
- An independent heartbeat watchdog that restores the fail-safe configuration.
- Safety constants and limits discovered from the installed entities.

## Implement

### Runtime configuration

- A simple `dynamic_control_enabled` switch, defaulting to `false`.
- The real Solis entity map.
- Installation facts required by calculations, including battery capacity and
  entity-provided minimum, maximum, and step values.
- Named safety constants, including the absolute minimum SOC and protective
  force-charge SOC.

### Strategy cycle

Use only the state needed to suppress unnecessary toggling:

- `IDLE`
- `RESERVE_DISCHARGING`
- `DISCHARGING`
- `CHARGING`
- `STOPPING`

Select one action per evaluation:

- `FAIL_SAFE`
- `STOP`
- `CHEAP_CHARGE`
- `RESERVE_DISCHARGE`
- `CYCLE_DISCHARGE`
- `IDLE`

At most one charging or discharging direction may be active. A direction change
must first disable and confirm all timed slots. Outside a trusted cheap window,
`RESERVE_DISCHARGE` exports only down to the dynamic household reserve. During a
profitable cheap window, `CYCLE_DISCHARGE` creates bounded full-SOC headroom and
then returns to charging when enough time remains to recharge.

### Coordinator

Implement one explicit path:

1. read and validate live inputs;
2. calculate reserve and strategy;
3. choose fail-safe or one action;
4. apply and verify the required Solis writes; and
5. publish a small heartbeat, current action, and last-error surface.

Transient missing or stale Solis telemetry/control inputs select recoverable
`DEGRADED` with the autonomous safe baseline. Invalid safety invariants, other
critical planning inputs, the manual guard, or failure to prove that baseline
select latched `FAIL_SAFE`.

### Cleanup

Delete, rather than migrate or preserve:

- the candidate runtime commissioning workflow and services;
- immutable commissioning records and their authority/fingerprint machinery;
- legacy/stub entity compatibility checks;
- the abstract legacy command/control path and fake Solis dependency;
- superseded planner, input, and simulation code after coordinator cutover;
- obsolete Home Assistant helpers;
- uncommissioned legacy state and migration handling; and
- proposed task scope that exists only to support those mechanisms.

## Safety invariants

1. Dynamic control defaults to disabled.
2. A transient unavailable/stale Solis telemetry/control input selects
   recoverable `DEGRADED` and the safe baseline; a hard invariant, unavailable
   critical planning input, or unproven baseline selects `FAIL_SAFE`.
3. No action enables charging and discharging at the same time.
4. Direction changes begin from confirmed all-slots-off state.
5. Every SOC target is clamped to the named absolute safety floor.
6. A failed or cancelled write performs bounded cleanup to Self-Use with all
   timed slots off.
7. Shutdown applies the safe baseline without latching the guard; the watchdog
   latches only hard failure or a controller that remains dead beyond grace.
8. Dynamic control is enabled only after explicit live verification and operator
   approval.

## Live verification

Deploy the candidate through Ansible and verify in place while the operator is
present.

- Confirm entity availability, writable ranges, options, and state feedback.
- Confirm the persistent Solis configuration recorded by IaC.
- Exercise fail-safe first.
- Exercise a bounded charge action and a bounded discharge action only when the
  operator confirms conditions are safe.
- Confirm direction changes pass through all-slots-off.
- Confirm stale/missing-input and watchdog recovery return to fail-safe.
- Leave dynamic control enabled only after the operator explicitly approves it
  and the live checks below pass. The current approved state is enabled and
  healthy in `RESERVE_DISCHARGE`.

## Non-goals

- Runtime commissioning or recommissioning.
- Persisted commissioning evidence or cryptographic authority.
- Backwards compatibility with uncommissioned entities, helpers, or state.
- Journals, leases, bootstrap tokens, or distributed coordination.
- Empirical discovery of power limits already exposed by the integration.
- A general-purpose diagnostics or simulation framework.

## Completion criteria

- The simplified implementation and focused tests pass.
- Obsolete runtime and helper surfaces are removed.
- The candidate is deployed successfully by Ansible.
- Live fail-safe and selected action verification passes.
- IaC documents the actual persistent Solis settings and entity mapping.
- The system is left in the operator-approved state.

## Live verification — 2026-08-23

- Dynamic control is live and healthy in `RESERVE_DISCHARGE`.
- Forced export was verified with Solis battery power −4917 W and Octopus
  current demand −4104 W.
- Grid Peak Shaving at 100 W is compatible with forced export. The native Grid
  Feed in Power Limit had been enabled at 0 W / 0 A; commissioning it to
  9900 W / 52 A restored export. EMS is disabled and output power is 100%.
- Runtime schedule boundaries are aware UTC instants. Solis `HH:MM` fields are
  converted to and interpreted in the configured inverter local timezone. The
  coordinator currently supplies HA's configured timezone and therefore assumes
  HA and the inverter share the site timezone (`Europe/London`). The earlier
  one-hour offset came from serialising UTC wall-clock fields at this boundary.
- HA export and peak-shaving switch entities are non-authoritative/unavailable;
  persistent settings remain one-time SolisCloud commissioning.
