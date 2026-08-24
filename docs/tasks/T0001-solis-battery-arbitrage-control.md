# T0001 — Solis battery arbitrage control

Status: In progress — lean local release candidate; live acceptance pending

Current implementation design: T0026. Chronological physical commissioning
evidence: `docs/house-battery-commissioning-log.md`.

## Objective

Control the Garage Solis battery so that it:

1. prioritises exporting surplus PV;
2. uses stored energy to reduce expensive grid import;
3. charges during standard and bonus Intelligent Octopus cheap periods;
4. preserves a dynamic household-energy reserve; and
5. creates profitable headroom by cycling at full SOC during cheap periods.

The implementation is one event-driven Home Assistant controller, one pure
planner, one narrow Solis adapter/writer, and one mode-only crash sentinel.

## System facts

- The battery is a 32.1536 kWh Fogstar Energy battery connected to a Solis
  S5-EH1P6K-L inverter.
- The installed inverter and battery are designed for the inverter's supported
  maximum charge and discharge capability.
- PV uses a separate inverter and is not Solis PV telemetry. Surplus PV appears
  as negative site load and has been proven to charge the battery.
- `Solis Inverter` is telemetry-only. `Solis Cloud Control` provides the runtime
  controls.
- Intelligent Octopus dispatches are fused into the trusted import-rate source.

## Commissioned boundary

These are one-time operator settings, documented rather than runtime-managed:

- EMS: disabled.
- Storage Mode baseline: Feed-In Priority.
- Grid Peak Shaving: disabled. Scheduled charge requires it disabled; battery
  discharge/load following does not require it.
- Grid Feed-In Power Limit: disabled. The DNO-approved site limit requires no
  lower software limit.
- Over-discharge SOC: 10%.
- Protective force-charge threshold: 7%, recovering to 10%.
- Maximum battery charge SOC and inverter output: 100%.
- Export: unlimited within the DNO-approved site-inverter limit.

Feed-In Priority, native charge, native discharge, the charge time/SOC stop and
unrestricted forced export have live physical evidence in the commissioning
log. The exact native midnight end representation remains to be commissioned.

Runtime owns only:

- Storage Mode when restoring Feed-In Priority or entering Self-Use fail-safe;
- Allow Grid Charging;
- Battery Reserve enable and reserve SOC;
- six native charge and six native discharge directions, including enable,
  local time, current and SOC target;
- live maximum charge/discharge current capabilities;
- `input_number.house_battery_cycle_discharge_duration_minutes`; and
- controller diagnostic sensors.

Runtime never writes Peak Shaving, feed limits, output power, protective
thresholds or the inverter clock.

## Safety and planning constants

- `FULL_SOC_PERCENT = 100`.
- `MINIMUM_SOC_PERCENT = 10`.
- The reverse reserve planner allows the established 0.1 kW residual-grid
  contribution through its named planning constant; this is not a Peak Shaving
  control.
- Cycle discharge duration is a live 1–60 minute helper, initially 10 minutes.

The dynamic reserve is the energy required to avoid expensive import before the
next trusted cheap opportunity. Reverse planning accounts for forecast demand,
negative-load PV, charge/discharge efficiency, live inverter capability and a
configured reserve margin. It never returns a reserve below
`MINIMUM_SOC_PERCENT` or above the physical battery range, and returns
unavailable rather than inventing an answer from invalid inputs.

## Operating policy

### Cheap charge

During a trusted positive-value cheap interval:

- use Feed-In Priority;
- enable Allow Grid Charging;
- charge at the supported current toward `FULL_SOC_PERCENT`; and
- bound the native slot by the cheap-window end and SOC target.

Standard cheap rates and Intelligent Dispatch are treated uniformly. A start is
best effort; failure degrades diagnostics and receives bounded retries only
while the same opportunity remains eligible.

### Reserve discharge

Outside a trusted cheap interval, discharge immediately toward the upward-rounded
dynamic reserve when SOC exceeds it. The native schedule ends at the next cheap
boundary or other bounded horizon, and the native target stops at the reserve.
The absolute `MINIMUM_SOC_PERCENT` stop is independent of planner availability.

### Full-SOC cycling

At full SOC during a profitable cheap interval:

1. stop and confirm charge off;
2. discharge for the configured duty duration, bounded by the reserve floor and
   cheap interval;
3. stop and confirm discharge off;
4. charge back toward full; and
5. repeat only if enough cheap time remains to discharge and recharge.

There is no pre-discharge mode.

## Schedule and direction invariants

- There is at most one desired direction.
- Direction changes are stop, confirm off, then start the other direction.
- Runtime instants are aware UTC; native schedule values are inverter-local
  `HH:MM` wall-clock values.
- Intervals are half-open `[start, end)`: adjacency is valid and overlap is not.
- A logical interval crossing local midnight is split into two adjacent native
  slots for both charge and discharge. Never rely on Solis priority between
  overlapping slots.
- An unavailable enable state blocks starts and is never treated as off.
- Native slot end and target SOC are authoritative bounds if Home Assistant is
  unavailable.
- Only a controller-owned direction that has been used and confirmed off may
  receive best-effort time/current cleanup. There is no all-slot normalization
  sweep.

## Reliability and lifecycle

One serialized, event-driven worker owns all runtime writes. Relevant entity
events and exact tariff/cycle/retry boundaries wake reconciliation; one minute
is only a missed-event backstop. Events during a write coalesce into a later
pass, and each pass advances at most one control change.

Starts retry at 0, 15 and 60 seconds for one unchanged generation, then suppress
until the plan or pending entity/target materially changes. Observation,
telemetry, inverter-time and CAS revision churn do not renew the budget.

Stops are important and retry indefinitely with capped backoff until observed
off. Timeout, error or cancellation creates ambiguous debt; optimistic `off`
cannot clear it without a later blocking service/readback proof. Re-created
drift creates stop work again.

Routine telemetry, tariff, forecast, service and readback failure is
`DEGRADED` and recovers passively. Continuous degradation for 15 minutes latches
a mode-only `FAIL_SAFE`: starts remain suppressed, known stops continue, and
Storage Mode retries toward Self-Use. Recovered inputs do not clear the latch;
fresh integration setup/restart does.

Orderly shutdown publishes fresh heartbeats, confirms Self-Use, then disables
only directions observed on. Unknown directions are reread without speculative
writes. A minimal stale-heartbeat sentinel may select Self-Use after startup
grace; it has no helper or recovery state and never writes slots or other
controls.

There is no manual guard, static enable switch, broad fail-safe script,
transaction/workflow framework, or second operational writer.

## Observability

Preserve sensors for:

- heartbeat, health, current action and diagnostic retry/error attributes;
- battery SOC and power;
- actual battery energy;
- dynamic reserve SOC and target energy; and
- reserve balance relative to actual energy.

Home Assistant service completion and matching control readback are provisional
control-plane evidence only. Physical operation requires fresh inverter battery
power, whole-site demand/export and preferably SOC movement.

## Remaining acceptance

Local behavior and repository release gates are owned by T0026/T0031. After an
authenticated Ansible deploy, live acceptance must:

1. validate HA configuration and the commissioned unmanaged settings;
2. prove cheap charge and its native end/SOC stop;
3. prove reserve discharge and its reserve stop;
4. prove a full-SOC discharge/recharge cycle;
5. prove no overlap during direction changes;
6. commission `24:00` or explicitly accept the `23:59` one-minute gap;
7. physically prove two-slot charge and discharge across local midnight;
8. prove fresh-setup recovery, degraded-to-Self-Use, sentinel and shutdown; and
9. retain 24 hours of evidence covering telemetry recovery, retries, reserve
   observability, static off-peak end, cycling and no next-day slot recurrence.

## Deferred policy choices

- minimum arbitrage margin;
- calibration of the existing battery-wear cost; and
- final timing margin before a cheap interval ends.
