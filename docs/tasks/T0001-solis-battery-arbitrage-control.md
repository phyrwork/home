# T0001 — Solis battery arbitrage control

Status: Proposed

## Objective

Control the Garage Solis battery so that it:

1. Prioritises exporting surplus PV.
2. Uses stored energy to minimise expensive grid import.
3. Cycles profitably during standard and bonus Intelligent Octopus cheap periods.
4. Preserves a dynamically calculated household-energy reserve.
5. Falls back to safe autonomous operation whenever Home Assistant control becomes
   unhealthy.

## System context

- The battery is a 32.1536 kWh Fogstar Energy battery connected to a Solis
  inverter.
- The installed inverter and battery system is designed to operate safely at the
  inverter's maximum supported charge and discharge capability.
- PV uses a separate inverter and is not reported as PV generation by Solis.
- Surplus PV is nevertheless visible to Solis as negative site load.
- It has been verified that negative load can charge the battery, so the planner
  may retain that assumption.
- Home Assistant has separate Solis telemetry and Solis Cloud control
  integrations.
- Intelligent Octopus dispatch information is available in Home Assistant.
- The existing `house_battery_control` reserve planner should be reused where its
  model remains applicable.

## Named constants

Define fixed safety and policy values once in the integration configuration or
constants module:

```python
FULL_SOC_PERCENT = 100
MINIMUM_SOC_PERCENT = 10
FORCE_CHARGE_SOC_PERCENT = 7
MAXIMUM_GRID_IMPORT_POWER_KW = 0.1
```

The cycle duration is the live Home Assistant entity
`input_number.house_battery_cycle_discharge_duration_minutes`; it is currently
not yet live commissioned. Deployment will set it to 10 minutes; it remains
configurable from 1 to 60.

### One-time commissioned facts

These are operator-recorded Solis settings, not runtime-managed entities, except
that the runtime writes Self-Use when applying fail-safe:

- EMS is disabled for this plant.
- Self-Use is the fail-safe storage mode and is the one commissioned setting
  deliberately written by the runtime during fail-safe.
- Native Grid Peak Shaving is enabled with a 100 W maximum grid-import setting.
- The protective force-charge safety threshold is 7% (`FORCE_CHARGE_SOC_PERCENT`).
- The absolute minimum SOC is 10%; the already-recorded over-discharge and
  recovery settings use that floor.
- Maximum battery charge SOC is 100% (`FULL_SOC_PERCENT`).
- Maximum inverter output is 100%.
- Export is unlimited, within the DNO-approved site-inverter export limit.

The runtime boundary is limited to the storage mode, grid-charging permission,
inverter clock, Battery Reserve and reserve SOC, global charge/discharge current
capabilities, the cycle-duration helper, all six charge/discharge slots, and
telemetry. Runtime does not discover, write or reconcile the commissioned facts
above, apart from writing Self-Use for fail-safe.

Safety values must not be duplicated as numeric literals throughout the
implementation. Code, tests, diagnostics and documentation should refer to these
constant names.

Charge and discharge current are runtime capability-derived settings. For those
two current caps only, the actuator must:

- Discover the inverter-supported maximum or documented unlimited sentinel.
- Apply that value.
- Read it back and verify acceptance.
- Avoid treating a generic Home Assistant entity range as verified equipment
  capability.
- Fail safely rather than applying an implausible discovered limit.

Output power and feed-in power were commissioned manually and are recorded
above. They are not actuator inputs and must not be discovered, applied or read
back by the runtime.

The planner may retain the verified runtime capabilities for its energy and timing
calculations.

## Persistent candidate configuration (commissioning record)

The following records the one-time commissioned baseline. Only storage mode,
grid-charging permission, current capabilities and schedule slots remain in the
runtime boundary; the native protection, output, export and Peak Shaving values
are not runtime writes:

- Storage mode: `Feed-In Priority`.
- Grid Peak Shaving: enabled.
- Maximum grid power: `MAXIMUM_GRID_IMPORT_POWER_KW`.
- Allow grid charging: enabled.
- Allow export: enabled.
- Over-discharge SOC: `MINIMUM_SOC_PERCENT`.
- Force-charge SOC: `FORCE_CHARGE_SOC_PERCENT`.
- Recovery from protective force charging: `MINIMUM_SOC_PERCENT`.
- Maximum charge current: inverter-supported maximum or unlimited.
- Maximum discharge current: inverter-supported maximum or unlimited.
- Maximum output power: installed maximum (100%, one-time commissioned).
- Maximum feed-in power: unlimited within the DNO-approved export limit (one-time commissioned).
- All charge and discharge slots initially disabled.

Maximum grid power means the maximum power that may be drawn from the grid.
Maximum feed-in power means the maximum power that may be exported.

The DNO-approved export limit equals the combined rated output of the site
inverters, so no lower software feed-in limit is required.

The runtime deployment validates interactions among the already-commissioned
Feed-In Priority, Peak Shaving, protective thresholds, forced discharge and
timed slots without taking ownership of the one-time native settings.

## Core operating policy

### Healthy Home Assistant control

While the controller is healthy:

- Maintain Feed-In Priority.
- Keep the commissioned native Grid Peak Shaving setting enabled at 100 W; the
  controller does not write its non-authoritative HA switch.
- Limit ordinary grid import to `MAXIMUM_GRID_IMPORT_POWER_KW`.
- Control charge and discharge slots dynamically.
- Never enable charge and discharge slots simultaneously.
- Never discharge below the dynamic household reserve.
- Never calculate a household reserve below `MINIMUM_SOC_PERCENT`.

Feed-In Priority with the separate PV inverter is considered validated. The
candidate deployment will validate its interaction with the remaining controls.

### Fail-safe operation

On orderly Home Assistant shutdown or controller failure:

1. Disable every Solis charge slot.
2. Disable every Solis discharge slot.
3. Set storage mode to `Self-Use`.
4. Leave physical battery-protection settings intact.
5. Preserve the commissioned native Grid Peak Shaving setting; the controller
   does not write or verify a non-authoritative HA switch.
6. Verify the runtime-controlled fallback configuration where communication
   remains available.

Self-Use may absorb surplus PV, but this is an accepted degraded mode because it
provides autonomous battery operation without relying on schedules or Home
Assistant.

After recovery, the controller must wait for fresh inputs and verified Solis state
before restoring the healthy configuration.

## Battery protection and household reserve

### Physical discharge protection

`MINIMUM_SOC_PERCENT` is the lowest SOC available to normal inverter operation.
The controller must not command a lower target, calculate a lower household
reserve or export energy protected by this threshold.

### Emergency force charging

At `FORCE_CHARGE_SOC_PERCENT`, the inverter should initiate protective charging
and recover the battery to `MINIMUM_SOC_PERCENT`.

This protection is independent of normal cheap-period charging, arbitrage cycling,
the dynamic household reserve and the desired end-of-window SOC.

### Dynamic household reserve

The household reserve represents the energy required to avoid expensive grid
import before the next cheap charging opportunity. The lower bound for
discretionary export is:

```text
max(MINIMUM_SOC_PERCENT, calculated household reserve)
```

The reserve remains dynamic and should reuse the existing reverse-planning model.
It is not the same as any physical protection threshold.

## Reusing the reserve planner

Retain the existing reserve-planning concepts:

- Reverse-plan energy needed before the next cheap charging opportunity.
- Account for forecast household demand.
- Account for charge and discharge efficiency.
- Account for the verified runtime inverter capabilities.
- Retain the configurable reserve margin.
- Clamp the result to the physical battery range using
  `MINIMUM_SOC_PERCENT` and `FULL_SOC_PERCENT`.

Preserve the verified treatment of external PV:

- Unmonitored PV surplus appears as negative load.
- Negative load can charge the battery.
- Forecast surplus may therefore reduce the required initial battery reserve and
  increase forecast battery energy, subject to the active mode and charge-power
  limit.

Extend the planner to include:

- Standard cheap periods.
- Bonus Intelligent Octopus dispatches.
- `MAXIMUM_GRID_IMPORT_POWER_KW` supplied by the grid during Peak Shaving.
- The time required to replenish deliberately exported energy.
- The required final SOC at the end of each cheap interval.

The reserve is both a planning constraint and a candidate input to the Solis
Battery Reserve controls. The candidate deployment must establish whether those
controls enforce the reserve without causing unwanted peak-rate grid charging.

## Headroom strategy 1 — off-peak full-SOC cycling

This strategy applies only while the current import price is off-peak and the
battery has reached `FULL_SOC_PERCENT`.

When those conditions hold:

1. Disable and verify the charge slot.
2. Configure a discharge period lasting
   `input_number.house_battery_cycle_discharge_duration_minutes` (set to 10
   minutes during deployment, configurable from 1 to 60).
3. Force discharge, stopping early if the dynamic household reserve is reached.
4. Disable and verify the discharge slot.
5. Resume charging to `FULL_SOC_PERCENT`.
6. Repeat only if sufficient cheap time remains to complete another discharge and
   recharge cycle.

This creates a bounded amount of headroom per cycle and avoids frequent
SOC-driven direction changes.

The controller must not initiate a cycle unless expected net export makes the
cycle profitable. Discharge consumed by the house or EV is valued as avoided
off-peak import rather than export.

Near the end of a cheap interval, the controller must stop starting new discharge
cycles, disable any discharge slot and enter a final charge phase toward
`FULL_SOC_PERCENT`.

## Reserve export outside a trusted cheap window

When no trusted positive-margin cheap or Intelligent Octopus dispatch window is
active, export immediately toward the dynamic reserve.

Calculate:

- Battery energy expected at the start of the interval.
- Energy that could be charged during the scheduled duration.
- Forecast load and negative-load PV contribution.
- The dynamic household reserve required before the interval.

If SOC is above the upward-quantized dynamic reserve, discharge via physical
slot 2 to that reserve, bounded by the next trusted cheap start or a sub-24-hour
horizon.

The discharge target must:

- Stop at the upward-quantized reserve and never below it.
- Remain above `MINIMUM_SOC_PERCENT`.
- Account for forecast demand before cheap charging starts.

This reserve export is independent of full-SOC cycling, which applies only while
the trusted cheap interval is active.

## Intelligent Octopus dispatches

Use planned, started and completed Intelligent Octopus dispatch information
already exposed in Home Assistant.

The controller must:

- Recognise new and changed dispatch windows.
- Merge adjacent or overlapping cheap intervals when appropriate.
- Confirm the cheap tariff before starting a cycle.
- Recalculate available headroom when a dispatch changes.
- Stop and clean up if a dispatch is withdrawn.
- Clear expired slots so they cannot repeat the following day.
- Reconcile all slots after Home Assistant restarts.

A planned dispatch is a trusted cheap window when its margin is positive.

## Arbitrage calculation

For energy imported for later export, approximate value as:

```text
export price × round-trip efficiency − import price
```

Discharge output must be divided into:

- Energy offsetting grid import, valued at the current import price.
- Net exported energy, valued at the export price.

Do not assume all inverter discharge becomes metered export, particularly while
the EV is charging.

Do not start a cycle unless its expected value remains positive after charge loss,
discharge loss, forecast household and EV demand, the configured minimum profit
margin and any future battery-wear allowance.

## Controller failure guard

Expose a controller heartbeat and diagnostic health state. Enter fail-safe
operation if:

- Home Assistant begins orderly shutdown.
- The controller heartbeat becomes stale.
- Battery telemetry is stale or unavailable.
- Required tariff information is unavailable.
- Dispatch data cannot be interpreted safely.
- Solis writes fail.
- Written settings cannot be verified.
- Charge and discharge slots are simultaneously enabled.
- Applied state persistently differs from desired state.
- The planner or coordinator raises an unhandled exception.
- A dynamic slot remains enabled beyond its expiry.

A separate Home Assistant automation should monitor the custom integration
heartbeat and invoke the fallback services if the integration becomes unavailable
while Home Assistant remains operational.

Every dynamic slot must have a real near-term end time because a sudden loss of
the Home Assistant host cannot execute the shutdown handler.

## Safe write ordering

When changing direction:

1. Disable the currently active slot.
2. Verify that it is disabled.
3. Configure the new time, current and target SOC.
4. Verify the configured values.
5. Enable the new slot.
6. Verify the resulting state.
7. Record the action, reason and expiry.

A partial write must never leave both directions enabled.

## Slot ownership

- Charge slot 1: standard and bonus off-peak charging.
- Discharge slot 1: full-SOC cycling during off-peak.
- Discharge slot 2: reserve export outside a trusted cheap interval.
- Remaining charge and discharge slots: disabled and reserved.

Home Assistant owns all Solis charge and discharge slots. Fail-safe cleanup
therefore disables all slots.

## Candidate deployment and validation

Implement and deploy the persistent candidate configuration before enabling
automated cycling.

Observe and record the runtime behaviour against the commissioned baseline:

- Peak Shaving holding grid import near `MAXIMUM_GRID_IMPORT_POWER_KW` (the
  native 100 W setting is already commissioned).
- The interaction between forced charging and Peak Shaving.
- Timed charge and discharge behaviour in Feed-In Priority.
- The effective maximum accepted charge, discharge, output and feed-in settings.
- Force charging from `FORCE_CHARGE_SOC_PERCENT` to
  `MINIMUM_SOC_PERCENT`.
- Battery Reserve behaviour above and below its configured SOC.
- Updated slot entity bounds after applying the new physical protection values and
  reloading or re-enumerating the Solis integration.

Any unsafe or incompatible result must trigger the fail-safe configuration before
the design proceeds.

## Diagnostics

Expose:

- Controller heartbeat and health.
- Normal, degraded or recovery state.
- Current storage mode.
- Current SOC and telemetry age.
- `MINIMUM_SOC_PERCENT` and `FORCE_CHARGE_SOC_PERCENT`.
- Current dynamic household reserve.
- Current cheap interval and its source.
- Remaining cheap time.
- Current charge or discharge phase.
- Phase start and expiry.
- Target SOC and target energy.
- Grid import and export power.
- Expected arbitrage value.
- Required final charge time.
- Desired and applied Solis slot state.
- Last successful write and verification.
- Last error and fail-safe reason.
- Startup and shutdown reconciliation status.

The runtime freshness budget is the named `MAXIMUM_TELEMETRY_AGE` constant. It
is currently 30 minutes: SolisCloud has returned successful polls with device
timestamps about 15 minutes old, so the budget allows delivery lag and
scheduling jitter without accepting an indefinitely stale device observation.
Device timestamp validity remains fail-closed.

## Implementation phases

### Phase 1 — Candidate configuration

- Implement named constants.
- Map the real Solis telemetry and control entities.
- Implement runtime charge/discharge-current capability discovery.
- Implement the persistent candidate configuration.
- Implement fail-safe application before other real control.
- Deploy the candidate.
- Observe and document the required interactions.

### Phase 2 — Planner refactor in observation mode

- Reuse and adapt the household reserve model.
- Preserve negative-load charging assumptions.
- Add standard and bonus cheap intervals.
- Add both headroom strategies and refill-time calculations.
- Calculate proposed actions without applying them.

### Phase 3 — Controlled charging

- Enable dynamic charge-slot writes.
- Verify standard and bonus cheap-period handling.
- Verify final charging and slot cleanup.

### Phase 4 — Controlled export cycling

- Enable reserve export outside trusted cheap windows.
- Enable full-SOC off-peak cycling.
- Validate actual metered export and profitability.
- Confirm that EV demand is handled correctly.

### Phase 5 — Remove obsolete implementation

- Remove stub inverter entities.
- Remove inaccurate abstract actuator mappings.
- Update tests, deployment documentation and working notes.

## Acceptance criteria

- Fixed safety and policy values are declared as named constants.
- Solis Over-discharge SOC uses `MINIMUM_SOC_PERCENT`.
- Solis Force-charge SOC uses `FORCE_CHARGE_SOC_PERCENT`.
- Protective charging recovers the battery to `MINIMUM_SOC_PERCENT`.
- The dynamic reserve never falls below `MINIMUM_SOC_PERCENT`.
- Peak Shaving uses `MAXIMUM_GRID_IMPORT_POWER_KW`.
- Capability-derived settings use verified maxima or unlimited sentinels rather
  than task-card literals.
- Feed-in is not unnecessarily restricted below inverter capability.
- Negative load is represented as battery-charge potential in the planner.
- Charge and discharge slots are never enabled simultaneously in production.
- Reserve export preserves only the energy needed before the next trusted cheap interval.
- Profitable cycling can repeat after the battery reaches full SOC during a cheap
  interval.
- No discharge cycle starts without enough time to recharge.
- The battery approaches `FULL_SOC_PERCENT` before the cheap interval ends.
- Bonus dispatch changes and expiry are handled safely.
- Orderly shutdown leaves Self-Use active with all slots disabled.
- Controller faults invoke the same fallback.
- Startup reconciliation clears expired schedules before control resumes.
- Diagnostics explain every decision and fail-safe transition.

## Open decisions

- The minimum arbitrage profit margin.
- Whether to include an explicit battery-wear cost.
- The final timing margin before the end of each cheap interval.
- The validated fail-safe setting for Battery Reserve; native Peak Shaving
  remains the commissioned 100 W invariant.
- Any changes required after observing the deployed candidate.

## Appendix A — Current Home Assistant control mapping

This appendix records the active Garage Inverter Control entities observed on
2026-08-22. Entity capabilities must be re-read during implementation rather than
treated as immutable.

### Persistent operating policy

| Scheme setting | Home Assistant entity | Type and intended use |
| --- | --- | --- |
| Storage mode | `select.garage_inverter_control_storage_mode` | Select with `Self-Use`, `Feed-In Priority` and `Off-Grid`; use Feed-In Priority while healthy and Self-Use during fail-safe |
| Grid charging permission | `switch.garage_inverter_control_allow_grid_charging` | Enable while scheduled charging is available |
| Maximum grid import | Native Grid Peak Shaving setting | One-time commissioned at 100 W; no runtime entity mapping |
| Inverter clock | `datetime.garage_inverter_control_inverter_time` | Verify or synchronise before programming slots |

### Battery protection and reserve

| Scheme setting | Home Assistant entity | Intended use |
| --- | --- | --- |
| Battery Reserve enable | `switch.garage_inverter_control_battery_reserve` | Candidate actuator for enforcing the dynamic household reserve |
| Battery Reserve SOC | `number.garage_inverter_control_battery_reserve_soc` | Candidate mapping for the calculated reserve, rounded upward and bounded by `MINIMUM_SOC_PERCENT` |

The inverter clock entity is a sampled datetime, not a continuously current
clock. The reader extrapolates its raw value by the age of the Home Assistant
sample (`reader_now - last_updated`); the actuator's one-minute skew check then
measures the estimated current clock offset. The controller does not write or
synchronise this clock at runtime.

The physical discharge floor, 7% force-charge threshold, 10% recovery setting,
100% maximum battery charge SOC, 100% inverter output setting and unlimited
export setting are one-time commissioned facts recorded above; the runtime does
not map them.

The candidate deployment must establish whether Battery Reserve:

- Prevents Peak Shaving from discharging below the dynamic reserve.
- Causes unwanted grid charging when the battery is below the reserve.
- Works consistently in Feed-In Priority.
- Should remain enabled during fail-safe or be reduced to
  `MINIMUM_SOC_PERCENT`.

### Inverter capability controls

| Capability | Home Assistant entity | Intended handling |
| --- | --- | --- |
| Global maximum charge current | `number.garage_inverter_control_battery_max_charge_current` | Discover and apply the inverter-supported maximum; do not assume the exposed HA maximum is safe |
| Global maximum discharge current | `number.garage_inverter_control_battery_max_discharge_current` | Same |
| Export calibration | `number.garage_inverter_control_export_calibration` | Preserve unless meter calibration is explicitly required |

Output power (100%) and feed-in power (unlimited, within the DNO limit) are
commissioned facts and are not runtime capability inputs. Export calibration is
also outside the runtime boundary and remains unmanaged.

The active global current entities currently advertise a much broader range than
the individual slot-current entities. Capability discovery must not equate either
generic entity bound with the safe hardware capability without verification.

### Schedule-slot controls

Six independent charge slots and six independent discharge slots are exposed. For
each `n` from 1 through 6:

| Function | Entity pattern | Type |
| --- | --- | --- |
| Enable charge | `switch.garage_inverter_control_slot{n}_charge` | Switch |
| Charge time | `text.garage_inverter_control_slot{n}_charge_time` | Exact `HH:MM-HH:MM` text |
| Charge current | `number.garage_inverter_control_slot{n}_charge_current` | Number; introspect and verify supported maximum |
| Charge target SOC | `number.garage_inverter_control_slot{n}_charge_soc` | Number; introspect bounds at runtime |
| Enable discharge | `switch.garage_inverter_control_slot{n}_discharge` | Switch |
| Discharge time | `text.garage_inverter_control_slot{n}_discharge_time` | Exact `HH:MM-HH:MM` text |
| Discharge current | `number.garage_inverter_control_slot{n}_discharge_current` | Number; introspect and verify supported maximum |
| Discharge target SOC | `number.garage_inverter_control_slot{n}_discharge_soc` | Number; introspect bounds at runtime |

All slots were disabled with `00:00-00:00` times when this inventory was taken.

The slot SOC entities reported a lower bound derived from the then-current default
Over-discharge SOC. That default was higher than `MINIMUM_SOC_PERCENT`. Treat the
reported bound as stale or configuration-derived metadata: after applying the
candidate safety thresholds, reload or re-enumerate the integration and introspect
the bounds again. Do not encode the previously observed bound in the controller.

### Telemetry input

| Planner input | Home Assistant entity | Intended use |
| --- | --- | --- |
| Battery SOC | `sensor.garage_inverter_telemetry_garage_inverter_remaining_battery_capacity` | Authoritative current SOC, subject to freshness checks |

The telemetry timestamp and battery power entities should also be mapped during
implementation to validate freshness and whether the commanded charge or discharge
actually occurred.

### Controls outside the scheme

| Entity | Treatment |
| --- | --- |
| `switch.garage_inverter_control_mppt_multi_peak_scanning` | Leave unmanaged; the Solis inverter has no connected PV |
| `number.garage_inverter_control_mppt_multi_peak_scan_interval` | Leave unmanaged |
| `number.garage_inverter_control_export_calibration` | Preserve unless separately commissioned |

### Fail-safe mapping

Fail-safe application should:

1. Turn off `switch.garage_inverter_control_slot{n}_charge` for every slot.
2. Turn off `switch.garage_inverter_control_slot{n}_discharge` for every slot.
3. Select `Self-Use` through
   `select.garage_inverter_control_storage_mode`.
4. Set Battery Reserve to its validated fail-safe state.
5. Verify that no slot remains enabled.
