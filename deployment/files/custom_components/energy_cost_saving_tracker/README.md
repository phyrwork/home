# Energy Cost Saving Tracker

Track realized energy cost savings against a configurable baseline rate.

Each tracker mirrors external source inputs into internal tracker state first:

- external current rate -> internal current rate
- external baseline rate -> internal baseline rate
- external energy/power source -> internal accumulation state

Derived sensors then use only the internal tracker state:

- current savings rate = internal baseline rate - internal current rate
- total savings += accepted energy delta * current savings rate

## Configuration

```yaml
energy_cost_saving_tracker:
  - name: Test Washing Machine
    energy_sensor: sensor.washing_machine_energy
    current_rate_sensor: sensor.octopus_energy_electricity_21l4421345_2700007165105_current_rate
    baseline_rate_sensor: sensor.octopus_energy_electricity_21l4421345_2700007165105_today_max_rate
```

Power-based trackers can be configured with `power_sensor` instead of `energy_sensor`.

## Sensors

Each tracker device exposes:

- Current Rate
- Baseline Rate
- Current Savings Rate
- Total Energy
- Total Savings
- Last Total Energy
- Last Updated Time

