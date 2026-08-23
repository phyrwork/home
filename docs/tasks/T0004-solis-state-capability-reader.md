# T0004 — Read and validate Solis state and capabilities

Status: Implemented

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on: T0003 — Define the live Solis entity configuration

## Objective

Add a read-only adapter that turns the configured Solis telemetry and control
entities into typed observations, per-slot state and runtime capabilities. Bad or
unresolved external state must produce structured critical issues and must never
be reported as healthy.

This task performs no writes, coordinator cutover, deployment or authenticated
Home Assistant access.

## Safety constants

Define hard, named limits:

```python
MAXIMUM_TELEMETRY_AGE = timedelta(minutes=30)
MAXIMUM_FUTURE_CLOCK_SKEW = timedelta(minutes=1)
```

`MAXIMUM_TELEMETRY_AGE` is a conservative safety limit based on the commissioned
Solis Inverter cadence and observed SolisCloud delivery lag. The integration
polls every five minutes, but a successful fetch has returned a device timestamp
about 15 minutes old. Six polling intervals tolerate that lag and additional
scheduling jitter while still failing closed on a sustained outage. Device time
remains authoritative; this is not a Home Assistant timestamp fallback.

## Module boundary

Put immutable result, issue, telemetry, slot-state and capability types in a pure
module with no Home Assistant imports. Put Home Assistant entity access and state
attribute parsing in a separate adapter module.

The reader accepts:

- the typed T0003 Solis configuration;
- an injected timezone-aware current time;
- Home Assistant state access.

It returns a structured result containing:

- controller health;
- zero or one complete snapshot;
- any safe partial observations useful for diagnostics;
- an ordered tuple of typed issues.

Expected missing or invalid external state is represented as issues rather than
an unhandled exception. Programming errors may still raise.

## Issue semantics

Each issue records at least:

- stable code;
- severity;
- related entity ID when applicable;
- human-readable explanation.

Any critical issue prevents `HEALTHY`, even when a partial snapshot is returned.
The following are always critical:

- missing T0003 Solis configuration;
- unresolved, missing, unknown, unavailable, stale or non-numeric SOC or battery
  power;
- unresolved battery-power sign or timestamp authority;
- invalid, future or stale device time;
- invalid required control state, option, unit, bound or step;
- an enabled reserved slot;
- more than one enabled direction across the 12 charge/discharge controls;
- any enabled charge/discharge conflict.

T0004 itself never tries to repair a critical condition.

## Telemetry

Read and validate:

- SOC as a finite percentage from 0 through 100;
- battery power as a finite signed value;
- the configured battery-power sign convention;
- device timestamp;
- Home Assistant `last_updated` for diagnostic comparison.

Normalize supported battery-power units `W` and `kW` to kW. Missing or unknown
units are critical; do not assume a unit.

Normalize battery power to one documented internal convention, with positive kW
meaning charging and negative kW meaning discharging. Apply the configured source
sign explicitly.

## Timestamp authority

A valid configured device timestamp is the only authoritative freshness source in
this task. Home Assistant `last_updated` is recorded for diagnostics but cannot
make a result healthy until a later commissioning record explicitly validates it
as an integration-specific fallback.

The device timestamp must:

- be timezone-aware after parsing;
- not be more than `MAXIMUM_FUTURE_CLOCK_SKEW` ahead of the injected current time;
- not be older than `MAXIMUM_TELEMETRY_AGE`;
- not disagree implausibly with the Home Assistant observation timestamp.

An invalid configured timestamp must not silently fall back to `last_updated`.
The explicit null device timestamp in T0003 therefore keeps the local MVP reader
degraded and prevents actuation until commissioning resolves timestamp authority.

## Persistent control state

Read every managed persistent, protection and global capability entity from
T0003. Validate:

- each mapped entity exists and is not `unknown` or `unavailable`;
- switch states are exactly `on` or `off`;
- storage mode is one of its advertised options;
- advertised storage-mode options include `Self-Use` and `Feed-In Priority` while
  allowing additional options such as `Off-Grid`;
- inverter datetime is parseable;
- number states and capability attributes are finite and internally consistent.

For every numeric capability read current value, minimum, maximum, step and unit.
Do not equate a generic entity maximum with a verified safe inverter setting.

Extend the T0002 runtime capability aggregate with optional, defaulted global
maximum charge-current and maximum discharge-current capabilities so existing
T0002 construction remains compatible. A healthy T0004 snapshot requires both.

## Per-slot state and capabilities

Define an immutable per-slot capability type containing:

- physical slot number;
- charge current capability;
- charge target-SOC capability;
- discharge current capability;
- discharge target-SOC capability.

The snapshot contains exactly six of these rather than collapsing possibly
different slot bounds into one unsafe aggregate.

For all 12 configured directions read:

- enable state;
- time text;
- current state and capability metadata;
- target SOC state and capability metadata;
- configured owner.

Parse time text as exact `HH:MM-HH:MM`:

- `00:00-00:00` is valid only when that direction is disabled;
- an enabled start equal to end is invalid;
- end earlier than start is an unambiguous cross-midnight interval ending the
  following day;
- a disabled direction may retain a valid non-zero programmed interval.

The intentional T0003 assigned directions may be observed as enabled by later
dynamic operation. More than one enabled direction across all 12 controls is a
critical conflict. Any enabled reserved direction is critical. Reserved
directions otherwise remain present in the snapshot for later cleanup.

## Capability validation

Validate each observed numeric capability independently:

- current value, minimum, maximum and step are finite decimals;
- minimum is not greater than maximum;
- current value is within the advertised range;
- step is positive;
- unit exists and is appropriate for the mapped role;
- SOC roles use percentages;
- current roles use amperes;
- output and feed-in roles retain their observed unit for later explicit policy
  resolution.

Do not infer equipment-safe maximums, unlimited sentinels or equivalence among
slot bounds.

## Compatibility boundaries

- Do not change T0003 configuration shape or deployed YAML.
- Do not replace the legacy input reader.
- Do not change coordinator, planner, controller, sensor or stub actuator.
- Do not call any Home Assistant service.
- Do not deploy or access live Home Assistant.

## Tests

Use deterministic fake Home Assistant states to cover:

- a complete valid snapshot;
- absent optional Solis config;
- unresolved power, sign and device timestamp;
- missing, `unknown` and `unavailable` states;
- finite numeric and SOC validation, including NaN and infinity;
- W/kW normalization and both configured source signs;
- missing and unknown power units;
- fresh, stale, future and naive device timestamps;
- device-time disagreement with `last_updated`;
- required storage-mode options with permitted extras;
- invalid switch and datetime states;
- valid and invalid number bounds, steps and units;
- six distinct per-slot capability records;
- disabled zero-time sentinel;
- rejection of enabled zero-time;
- cross-midnight enabled intervals;
- enabled reserved directions;
- multiple enabled directions and charge/discharge conflict;
- proof that no Home Assistant service is called.

Run all existing integration tests when dependencies are available.

## Acceptance criteria

- Real Solis state can be represented without a write or coordinator change.
- Every unsafe or unresolved external fact deterministically prevents `HEALTHY`.
- Device time is authoritative and no unvalidated freshness fallback exists.
- Battery power has one normalized internal unit and sign convention.
- All global and per-slot capabilities are preserved without unsafe inference.
- All 12 slot directions are observable and conflicts are detected.
- Focused offline tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0004-solis-state-capability-reader`
- Isolated worktree.
- Small-model implementation agent after T0003 is integrated.
- Expected changes are limited to pure state/result types, the read-only Solis
  adapter, narrowly compatible capability-contract additions, focused tests and
  this card.
