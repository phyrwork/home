# House Battery Control — Working Notes

Updated: 2026-07-04

## Goal

Implement a fully typed Home Assistant custom integration, `house_battery_control`,
for a 32 kWh Fogstar battery and 6 kW Solis inverter expected to be installed on
2026-07-15.

The physical control path is assumed to be SolisCloud monitoring plus the
SolisCloud Device Control API through an S2-WL-ST logger. Device Control API
access is a hard dependency. Stub Home Assistant entities will stand in for the
uninstalled equipment.

## Architecture

1. External adapters map provider data into internal types:
   - `dependencies.octopus_energy`
   - `dependencies.forecast_solar`
   - load forecast dependency: undecided
   - `dependencies.solis_cloud`
2. `planner.fuse_forecasts` aligns tariff, load, and solar forecasts into
   self-contained `planner.InputInterval` values.
3. The reserve planner calculates the energy that must be protected without
   including deliberate arbitrage export.
4. The real-time controller selects a typed internal command.
5. The SolisCloud adapter maps the internal command to inverter controls.
6. Home Assistant exposes inputs, diagnostics, desired state, and confirmed
   applied state.

## Established conventions

- Domain calculations use `Decimal`.
- Domain types are frozen, slotted dataclasses.
- External provider shapes do not leak into planner/controller code.
- Anything representing a period ends in `Interval`.
- Datetimes are timezone-aware and intervals are half-open: `[start, end)`.
- Class documentation uses Google-style docstrings.
- Attribute documentation is a string literal directly below the attribute.
- Power limits live in `battery.Spec`, not individual controller commands.
- Battery charge/discharge power is measured on the AC side of the inverter.
- Charge efficiency converts AC input into stored battery energy; discharge
  efficiency converts stored battery energy into AC output.

## Existing types

- `TimeInterval`
- `energy.EnergyInterval`
- `tariff.Tariff`
- `tariff.TariffInterval`
- `battery.Spec`
- `battery.State`
- `planner.Input`
- `planner.InputInterval`
- `planner.ReserveInterval`
- `controller.GridCharge`
- `controller.ForceExport`
- `controller.SelfConsumption`
- `controller.Hold`
- `controller.Command`

## Controller intent

```python
if cheap_import_active:
    GridCharge(target_energy_kwh=capacity)
elif previous_command is ForceExport and battery_energy_kwh > reserve.start_energy_kwh:
    ForceExport(target_energy_kwh=reserve.start_energy_kwh)
elif battery_energy_kwh > reserve.start_energy_kwh + export_hysteresis_kwh:
    ForceExport(target_energy_kwh=reserve.start_energy_kwh)
else:
    SelfConsumption(minimum_energy_kwh=reserve.end_energy_kwh)
```

Commands carry safe stop targets. Solis charge/discharge power comes from the
current `battery.Spec`. Hysteresis uses the last desired command, avoiding cloud
confirmation latency breaking the export latch. `Hold` is reserved for fail-safe
handling of missing or stale inputs.

Each `ReserveInterval` is one segment of the reverse-planned reserve trajectory.
Its starting energy protects current and future demand from forced export; its
ending energy permits the current interval's demand to consume the energy that
was reserved for it.

### Off-peak cycling arbitrage

Published Octopus material explicitly permits charging home batteries when
electricity is cheap and exporting later. Intelligent Octopus Go is compatible
with Outgoing Octopus, and current export terms pay for metered export without
distinguishing whether stored energy originated from solar or grid import.

GTC G99 offer 90120 approves the submitted `SolarAndBatteryHybrid` installation
for 8.5 kW total generating and export capacity. The planned 6 kW
battery-inverter export therefore fits within the approved combined capacity,
assuming the installed configuration matches the submission.

Despite being permitted, deliberate off-peak cycling will not be implemented.
Intelligent dispatches commonly coincide with EV charging, and regular
off-peak hours are also likely EV-charging hours. Forced battery discharge would
therefore serve the EV behind the same meter rather than produce paid net
export, wasting conversion energy and battery throughput. The controller will
charge to capacity and hold that target throughout off-peak periods.

## Reserve-planner intent

The reserve planner answers:

> How much battery energy must be protected now to avoid expensive grid import
> before future cheap charging can recover the battery?

For each normalized interval:

- Off-peak import: charge at the configured maximum AC power, respecting
  efficiency and capacity. Solar and grid are not attributed separately:
  rooftop solar naturally reduces net grid import on the shared AC bus.
- Otherwise, solar serves load first.
- Surplus solar charges the battery.
- Remaining load drains the battery.
- Deliberate export is not included.

Calculate the theoretical minimum initial energy with a reverse requirement
pass:

1. Begin with the physical minimum required at the forecast horizon.
2. Walking backward, add battery energy needed for peak deficits, limited by
   the inverter's discharge power and adjusted for discharge efficiency.
3. Subtract energy supplied by off-peak charging or peak solar surplus, limited
   by charge power and adjusted for charge efficiency.
4. Clamp each requirement to the physical battery limits. Demand beyond the
   discharge-power or capacity limits is unavoidable grid import and does not
   inflate the reserve.

`planner.reserve_intervals` adds an explicit non-negative
`reserve_margin_kwh` at every trajectory boundary and clamps each value to
battery capacity. Home Assistant will initially supply an adjustable 2 kWh
margin.

## Tariff policy

Derive peak and off-peak classification directly from the Octopus import-price
forecast:

- Peak price is the highest import price present in the forecast.
- Any interval with a lower import price is off-peak.
- Intervals equal to the highest price are peak.
- If the forecast has only one distinct import price, all intervals are peak.
- `dependencies.octopus_energy` derives the classification while mapping the
  complete Octopus day-rate set.
- `tariff.Tariff.import_price_is_off_peak` stores the classification, keeping
  provider policy out of the planner and preventing contradictory simulation
  inputs.

This deliberately follows the tariff forecast rather than duplicating a price
in configuration, so tariff price changes require no manual update.

## Completed

- [x] Create branch `codex/house-battery-control`.
- [x] Add internal domain and controller types.
- [x] Add Octopus Energy, Forecast.Solar, and normalized SolisCloud boundaries.
- [x] Implement and test `planner.fuse_forecasts`.
- [x] Commit domain types as `4c52be09`.
- [x] Commit forecast fusion as `0528c222`.

## Pairing plan

- [x] Define tariff policy and the exact meaning of off-peak import.
- [x] Implement and test reserve calculation.
- [x] Decide the reserve safety margin: adjustable kWh, initially 2 kWh.
- [x] Implement the reserve safety margin.
- [x] Implement and test real-time controller decision and hysteresis.
- [x] Add a test-only controller simulator and scenario suite.
- [x] Add the initial flattened hourly non-EV house-load forecast.
- [ ] Optionally refresh the house-load profiles from history at most weekly.
- [x] Compare Forecast.Solar predictions with actual house solar generation and
      calibrate its effective capacity.
- [ ] Add Home Assistant manifest, configuration, coordinator, and update
      triggers.
- [ ] Define and install stub battery/inverter entities.
- [ ] Expose reserve, desired command, confirmed command, freshness, and errors.
- [ ] Implement idempotent SolisCloud command application.
- [ ] Replace stub Solis telemetry/control after installation.
- [ ] Add conservative behavior for missing/stale forecasts and API failures.
- [ ] Add deployment wiring and verify on the live HA instance.

## Load forecast decision

The initial flattened non-EV house-load forecast uses:

1. Use hourly intervals initially. Home Assistant retains hourly long-term
   statistics; half-hour detail can be added later by extending recorder
   retention or storing integration-owned history.
2. Store the analyzed 24-hour weekday and weekend profiles directly; do not
   recalculate them during normal planner updates.
3. Rebuilding profiles from Home Assistant history is later work and should
   run no more than weekly.

The initial analysis used the latest eight occupied weeks, separated weekday
and weekend, with EV energy removed. Each median hourly shape was normalized to
the median complete-day energy for its day class. Independent hourly medians
otherwise suppress loads whose timing varies: their unscaled sum is about
4.8 kWh/day, versus observed complete-day medians of about 6.28 kWh on weekdays
and 6.76 kWh on weekends.

Available live cumulative-energy sources are:

- grid import:
  `sensor.octopus_energy_electricity_21l4421345_2700007165105_current_total_consumption`
- grid export:
  `sensor.current_accumulative_consumption_export_electricity_21l4421345_2700009249389`
- rooftop solar:
  `sensor.solar_generation_meter_total_energy`
- EV charging: hourly mean power from
  `sensor.ev_charger_energy_meter_power`, with older renamed charger-power
  statistics used for historical continuity

For each historical interval:

`non_ev_load = solar_generation + grid_import - grid_export - ev_consumption`

Use data since January, but exclude the inferred holiday interval from 24 June
through 3 July 2026 from the baseline. During 24 June through 2 July, derived
house load is consistently about 3.6--3.8 kWh/day; import and EV charging rise
sharply late on 3 July, indicating the return. Keep EV load separate: it is
measured independently and should not distort the ordinary house-load forecast.

Some hourly meter statistics arrive late or contain gaps. Clamp only tiny
negative residuals caused by meter timing to zero. Reject an entire day if an
hour has a materially negative derived load, or if any source interval is
missing; for example, grid-import statistics are clearly incomplete during EV
charging on 20 June.

## Solar forecast calibration

Forecast.Solar is UI-configured with a 45-degree tilt, 152-degree compass
azimuth, and an effective capacity of 4270 W. The physical array remains
2400 W; that value is retained in `input_number.solar_installed_size` and used
to reject pulse-meter power spikes.

The effective Forecast.Solar capacity was derived from nine complete days,
25 June through 3 July 2026. Across 196 retained same-day and day-ahead
forecast updates, the aggregate actual-to-forecast ratio was 1.779, so
2400 W was scaled to 4270 W. This improves the native Home Assistant forecast
as well as planner input. Recheck the calibration after collecting a longer,
seasonally varied sample.
