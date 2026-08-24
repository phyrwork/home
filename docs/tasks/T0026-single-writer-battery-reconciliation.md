# T0026 — Lean house-battery control integration

Status: Accepted

## Objective

Replace the current 21-module, 6,000-line implementation with the smallest
integration that preserves the commissioned battery strategy and reliable Solis
control.

The result is one YAML-loaded, event-driven controller and one minimal crash
sentinel. Delete uncommissioned compatibility, duplicate models, generic
transaction machinery, broad cleanup, manual guard, static enable flag, and
persisted/workflow-style state.

## Commissioned boundary

One-time Solis commissioning is complete:

- EMS is disabled.
- Feed-In Priority is the normal operating mode.
- Grid Peak Shaving is disabled and unmanaged.
- Grid Feed-In Power Limit is disabled and unmanaged.
- Solis Cloud Control is the sole control integration.
- Solis Inverter is telemetry-only; its experimental controls are disabled.
- The separate PV inverter is not Solis telemetry, but negative house load is
  valid surplus PV and may charge the battery.

Runtime owns only:

- Storage Mode when entering or leaving fail-safe;
- Allow Grid Charging;
- Battery Reserve enable and reserve SOC;
- the six native charge/discharge slot enables, times, currents and SOC targets;
- the configured full-SOC cycle duration helper; and
- controller diagnostic sensors.

The configured entity map is authoritative. Missing or invalid entities are
reported, never guessed. Runtime power uses live inverter capability controls;
nominal power values are not hardcoded.

## Inputs and planning

Use the existing fused Octopus import/export intervals and forecast source.
Intelligent Dispatch is an ordinary trusted cheap interval. A cheap interval is
eligible only when its import/store/export value remains positive after the
configured efficiencies.

Keep the proven dynamic reserve model:

- reverse-plan household energy until the next cheap opportunity;
- include forecast load, negative-load PV, reserve margin and efficiencies;
- respect live charge/discharge capability;
- clamp to `MINIMUM_SOC_PERCENT` and the physical battery range; and
- return unavailable instead of inventing a result from bad inputs.

Preserve sensors for health, heartbeat, action, last error, SOC, battery power,
actual battery energy, reserve target energy and reserve balance.

## Strategy

There is at most one desired direction. A logical schedule may use two adjacent
native slots when it crosses inverter-local midnight; charge and discharge both
support this split. Never encode an overnight logical interval as one native
cross-midnight slot until that behavior is physically proven. The exact midnight
end representation (`24:00` if accepted, otherwise the commissioned supported
boundary) is a required live commissioning result. If only `23:59` works, the
resulting one-minute gap is an explicit accepted limitation rather than silently
treated as continuous.

### Cheap charge

During a trusted cheap interval, enable Allow Grid Charging and a charge slot at
the supported current, targeting full SOC and ending at the cheap boundary.

### Reserve discharge

Outside a trusted cheap interval, discharge immediately toward the dynamic
reserve target when SOC exceeds it. Stop at the reserve target or native slot
boundary.

### Full-SOC cycling

At full SOC during a profitable cheap interval, discharge for the configured
cycle duration to create headroom, stopping at the dynamic reserve floor or
cycle boundary. Then charge back to full. Start another cycle only if enough
cheap time remains to discharge and recharge. There is no pre-discharge mode.

### Direction and schedule rules

A direction change is stop, confirm off, validate, then start. The two segments
of one split logical interval are the only multiple enabled slots permitted and
must be adjacent, same-direction, and non-overlapping. Never rely on Solis
priority between overlapping slots.

Runtime instants are aware UTC. Solis schedules are inverter-local `HH:MM`
wall-clock values converted at the Solis boundary using the site timezone.
Intervals are half-open `[start, end)`: adjacency is valid; overlaps are rejected,
including across midnight.

## Reconciliation

Use one serialized worker. Relevant HA state events and exact time/SOC boundaries
mark it dirty; events received during a write cause one further pass. Retain one
minute as a missed-event backstop, not as the primary cadence.

Each pass:

1. read the current Solis controls and planning inputs;
2. derive one desired intent when inputs are valid;
3. stop one observed conflicting, expired or target-complete direction;
4. otherwise advance one write toward the desired one- or two-slot plan; and
5. publish one diagnostic snapshot.

An unavailable slot-enable state blocks starts and is never treated as off.

Starts are best effort. A failed start reports `DEGRADED` and performs no
Self-Use or cleanup. For one unchanged eligible intent, attempt at
`START_RETRY_DELAYS = (0s, 15s, 60s)` and then suppress it until a relevant
planning or Solis-control generation changes. Re-evaluate eligibility before
every attempt. Events may wake reconciliation early but do not reset the attempt
count unless they change that generation.

Stops are important. Keep only the enable entity IDs awaiting stop plus retry
timing. Retry until observed off. Strategy changes cannot cancel a known stop.
Unknown stop state is reread with backoff and never receives a speculative write.

Each start, stop, or mode service attempt has a 30-second monotonic deadline.
Critical stop retries are unbounded across attempts using
`STOP_RETRY_DELAY = min(5s × 2**attempt, 60s)`.

After a stop service timeout, error, or cancellation, an optimistic HA `off`
state cannot clear stop debt. A later successfully completed blocking `off` call
must provide provisional proof; this deliberately prefers a harmless duplicate
stop to abandoning an important stop.

Any observed enabled discharge at or below `MINIMUM_SOC_PERCENT` is stop work
even when tariff, forecast, reserve planning, or other strategy inputs are
unavailable.

Disabled times, currents and targets are not stop proof. After a direction owned
and used by the controller is confirmed off, its time and current may be reset
once as best-effort housekeeping so stale stored values do not accumulate. Never
normalize all 12 directions as a sweep, and never let normalization block a stop,
direction change, fail-safe or shutdown.

## Health, fail-safe and lifecycle

- `HEALTHY`: current valid intent/state is reconciled.
- `DEGRADED`: inputs, planning, a start, stop or readback are temporarily
  insufficient; automatic recovery continues.
- `FAIL_SAFE`: continuous degraded operation exceeds
  `DEGRADED_FAILSAFE_TIMEOUT` (initially 15 minutes), or a hard internal invariant
  fails.

Fail-safe writes only `Storage Mode = Self-Use`, suppresses starts, continues
known stops, and retries Self-Use until observed. It does not reset reserve,
grid charging, slot fields or capability controls. The latch is memory-only; a
fresh integration setup, including Home Assistant restart, clears it and
reconstructs state from live controls.

After startup/restart has valid Solis state and planning prerequisites, restore
and confirm the commissioned Feed-In Priority mode before any start. Failure to
prove that transition remains `DEGRADED` with no start, but never blocks known
stop work.

Routine telemetry, tariff, forecast, service or readback failure begins as
`DEGRADED`, not `FAIL_SAFE`.

`FAIL_SAFE` never exits automatically when inputs recover. Only a fresh
integration setup/restart clears the latch; this intentional operator boundary
is tested.

On orderly shutdown:

1. stop accepting starts and publish a shutdown heartbeat;
2. select and confirm Self-Use;
3. read all configured slot enables;
4. switch off only directions observed on;
5. reread unknown directions with backoff; and
6. continue until all are authoritatively off or HA forcibly terminates.

Do not write already-off or unknown directions. Deliberate disablement is YAML
configuration removal followed by HA restart. There is no runtime guard or
config-entry conversion.

## Crash sentinel

Retain one minimal independent automation for an actually dead integration. It
may write only Self-Use, and only after startup grace when the controller
heartbeat is stale/unavailable and Storage Mode is not already Self-Use. It
rechecks both conditions immediately before writing.

A fresh heartbeat suppresses the sentinel regardless of `HEALTHY`, `DEGRADED`,
`FAIL_SAFE` or shutdown action. The sentinel has no helper, latch, script or
recovery state and never writes slots, reserve, grid charging, currents or
targets.

## Target implementation

Target seven source modules plus tests:

```text
house_battery_control/
├── __init__.py      # YAML setup and lifecycle
├── config.py        # strict config and entity map
├── model.py         # shared enums and dataclasses only
├── planner.py       # tariff windows, load, reserve and pure strategy
├── solis.py         # state read, overlap, narrow CAS/readback writes
├── controller.py    # reconciliation, retry, fail-safe and shutdown
└── sensor.py        # diagnostic entities
```

Implementation constraints:

- simple functions and frozen dataclasses are preferred;
- one ordinary `asyncio.Lock` serializes all writes;
- one narrow writer supports only the configured HA entity domains;
- service success plus matching post-call HA state is provisional control-plane
  success, not proof of physical inverter action; timeout/error/cancellation is
  never accepted as success merely because optimistic HA state happens to match;
- native slot end/SOC bounds limit actions if HA disappears;
- no general request hierarchy, transaction object, reentrant lock, cleanup task,
  second retry layer or reusable workflow engine; and
- do not rewrite proven DST/rate coverage or reserve mathematics merely to
  reduce file count.

Delete or absorb the current `contracts`, `domain_constants`, `energy`,
`ha_writer`, `interval`, `load`, `octopus_windows`, `reserve_planner`,
`runtime_inputs`, `solis_actuator`, `solis_config`, `solis_policy`,
`solis_reader`, `solis_state`, `strategy`, and `write_contracts` modules after
their required behavior has moved to the target modules.

Also delete:

- `dynamic_control_enabled` and the control-disable guard end to end;
- `input_booleans/house_battery.yaml`;
- the broad fail-safe script;
- broad safe-baseline proof and slot normalization;
- obsolete generated bytecode in the integration tree;
- tests of deleted abstractions, replacing them with behavior tests; and
- obsolete current documentation that describes guard/broad-baseline operation.

## Local acceptance

Tests must prove:

1. strict config and exact configured entity mapping;
2. rate/dispatch parsing, freshness, coverage and DST boundaries;
3. reserve planning, efficiency, capability limits and safety clamping;
4. reserve-energy observability values;
5. cheap charge, reserve discharge and full-SOC cycle selection;
6. aware UTC/inverter-local schedule conversion;
7. half-open overlap rejection, adjacency acceptance, and two-slot local-midnight
   splitting for both charge and discharge;
8. unavailable inputs degrade without a start or cleanup write;
9. an unchanged failed start is not rewritten indefinitely;
10. conflicting/expired/target-complete actions create persistent stops;
11. unknown slot state blocks starts and speculative writes;
12. observed minimum-SOC discharge stops even without planning inputs;
13. direction changes require confirmed previous stop;
14. eventually-consistent CAS/readback success, timeout and later drift;
15. optimistic `off` after a timed-out stop does not clear stop debt;
16. an event during a write causes a subsequent reconciliation;
17. prolonged degraded state enters mode-only Self-Use fail-safe and recovered
    inputs do not clear its latch;
18. fresh integration setup/restart reconstructs state and clears the fail-safe
    latch;
19. shutdown selects Self-Use and stops only observed-on slots;
20. stale-heartbeat sentinel writes only Self-Use;
21. rendered deployment has no guard helper or broad fail-safe script; and
22. the full repository test suite, compile checks and Ansible syntax check pass.

Council review must approve the final implementation against this card and
confirm that the production integration is materially smaller and contains no
duplicate active control path.

## Live acceptance

After local acceptance and explicit auth availability:

1. deploy through Ansible and validate HA configuration;
2. verify the commissioned boundary and no unexpected runtime writes;
3. prove cheap charging and its time/SOC stop;
4. prove reserve discharge and its reserve stop;
5. prove full-SOC cycling returns to charge;
6. prove direction changes do not overlap;
7. commission and document the native midnight representation and exact two-slot
   allocation; physically prove split charging across midnight with the first
   segment stopping and the second becoming effective without overlap, and prove
   equivalent split-discharge control/readback behavior;
8. perform a fresh integration setup/restart with a bounded action and verify
   live-state recovery and fail-safe-latch clearing;
9. prove degraded-to-Self-Use and crash-sentinel behavior without contention;
10. prove orderly shutdown behavior; and
11. observe for 24 hours, retaining evidence for telemetry freshness, retries,
    reserve target/balance, physical power flow, the static off-peak end, at
    least one full-SOC discharge/recharge cycle, no direction overlap, and no
    next-day recurrence of stopped slots.

## Non-goals

- Runtime commissioning or migration of uncommissioned state.
- Config-entry support or a manual pause helper.
- A general-purpose scheduler, queue, transaction or simulation framework.
- Persistent intent, retry, authority, lease, fingerprint or fail-safe records.
- A broad watchdog, second operational writer or all-controls safe baseline.
- Runtime Peak Shaving or Grid Feed-In Power Limit control.
