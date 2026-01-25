# Energy Cost Forecast

Estimate appliance run costs using future tariff rates and a power profile.

## Use cases

- Fixed cycle appliances (dishwasher, washer/dryer) with a known power profile.
- Dynamic loads (EV charging, immersion heater) where the profile is computed from
  target energy or duration and stored in a sensor.

## Configuration

This integration supports UI config or YAML import. Each entry requires an import
rate entity and a power profile (inline, file, or sensor).

YAML example:

```yaml
energy_cost_forecast:
  - name: Dishwasher Cost
    import_rate_sensor: sensor.octopus_energy_rates
    export_rate_sensor: sensor.octopus_export_rate
    export_power_sensor: sensor.current_export_power
    target_percentile: 25
    start_step_mode: fixed-interval
    start_step_minutes: 5
    update_interval_minutes: 15
    power_profile:
      - [1500, "30m"]
      - [200, "90m"]
```

Dynamic profile example (EV charging):

```yaml
energy_cost_forecast:
  - name: EV Charging Cost
    import_rate_sensor: sensor.octopus_energy_rates
    target_percentile: 25
    start_step_mode: fixed-interval
    start_step_minutes: 15
    update_interval_minutes: 15
    power_profile_sensor: sensor.ev_charge_power_profile
```

The `power_profile_sensor` should return a JSON/YAML list, for example:

```text
[[7200, "2h30m"]]
```

## Rate schema

The import rate entity must expose a `rates` attribute containing a list of rate
windows with `start`, `end`, and `value` or `value_inc_vat` (ISO timestamps).
Example:

```json
[
  {"start": "2024-01-01T00:00:00+00:00", "end": "2024-01-01T00:30:00+00:00", "value_inc_vat": 0.25}
]
```

## Power profile formats

Profiles are lists of `[power_watts, duration]` entries. Duration supports `h`
and `m` units (for example, `"1h30m"`).

Sources:
- `power_profile`: inline JSON/YAML list.
- `profile_file`: path to a YAML file containing the list.
- `power_profile_sensor`: sensor or input_text containing the list.

## Limitations

- Costs are calculated over available rate windows; missing coverage yields no
  cost for that start time.
- Export offset uses a single instantaneous export power sample.
