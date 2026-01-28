# Energy Cost Forecast

Estimate appliance run costs using future tariff rates and a power profile.

## Use cases

- Fixed cycle appliances (dishwasher, washer/dryer) with a known power profile.
- Dynamic loads (EV charging, immersion heater) where the profile is computed from
  target energy or duration and stored in a sensor.

## Configuration

This integration supports UI config or YAML import. Each entry requires an import
rate entity and a power profile (inline, file, or sensor).

### Power profiles

Appliance power consumption over time is defined by a power profile. Each entry in the
profile is a `[power, duration]` pair.

Power is a number in watts (W).  Durations are strings supporting `h` and `m` units (for
example, `"1h30m"`).

Profiles can be provided via:
- `power_profile`: inline JSON/YAML list.
- `power_profile_file`: path to a YAML file containing the list.
- `power_profile_entity`: sensor or input_text containing the list.

### Static profile example (dishwasher)

Appliances with known power profiles can be defined using `power_profile` or
`power_profile_file`.

```yaml
energy_cost_forecast:
  - name: Dishwasher Cost
    import_rate_sensor: sensor.octopus_energy_rates
    export_rate_sensor: sensor.octopus_export_rate
    export_power_sensor: sensor.current_export_power
    target_percentile: 25
    start_after: "2024-01-01T06:00:00"
    start_before: "2024-01-02T06:00:00"
    finish_after: "2024-01-01T08:00:00"
    finish_before: "2024-01-02T10:00:00"
    start_step_mode: fixed-interval
    start_step_minutes: 5
    update_interval_minutes: 15
    power_profile:
      - [1500, "30m"]
      - [200, "90m"]
```

### Dynamic profile example (EV charging)

Appliances with variable power profiles can use a sensor or input_text entity to
provide the profile data. The entity must return a JSON/YAML list.

```yaml
energy_cost_forecast:
  - name: EV Charging Cost
    import_rate_sensor: sensor.octopus_energy_rates
    target_percentile: 25
    start_step_mode: fixed-interval
    start_step_minutes: 15
    update_interval_minutes: 15
    power_profile_entity: sensor.ev_charge_power_profile
```

The `power_profile_entity` should return a JSON/YAML list, for example:

```text
[[7200, "2h30m"]]
```

## Rate schema

The import rate entity must expose a `rates` attribute containing a list of rate
windows with `start`, `end`, and `value` or `value_inc_vat` (ISO timestamps).
The export rate entity uses the same schema when provided.
Example:

```json
[
  {"start": "2024-01-01T00:00:00+00:00", "end": "2024-01-01T00:30:00+00:00", "value_inc_vat": 0.25}
]
```

## Export offset (optional)

If both `export_power_sensor` (W) and `export_rate_sensor` (price per kWh) are
set, the "Start Now Cost" calculation offsets import costs using the current
export power and export rate. When export values are used, the `rate_source`
attribute on the "Start Now Cost" sensor is set to `export`.

## Limitations

- Costs are calculated over available rate windows; missing coverage yields no
  cost for that start time.
- Export offset uses a single instantaneous export power sample.

## Optional time filters

The `start_after`, `start_before`, `finish_after`, and `finish_before` options
limit which candidate start windows are considered. Values are local datetimes.
Filters are applied before percentile selection.
