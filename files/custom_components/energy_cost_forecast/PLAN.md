# Energy Cost Forecast

## Brief
Build a reusable Home Assistant integration that evaluates appliance run cost against time-varying energy tariffs using a fixed appliance power profile. It exposes sensors that let users decide when to run an appliance (run now vs wait) without relying on uncertain future forecasts.

## Requirements

### Inputs
- Import price sensor (Octopus `_current_day_rates` or `_next_day_rates`) as the single source of rates.
- Optional export price sensor.
- Optional export power sensor (power/energy-class sensor) for steady-state export offset.
- Appliance power profile as a list of `(power, duration)` tuples.
  - Format: JSON/YAML list like `[[2000, "1h"], [500, "1h30m"]]`.
  - `power` is numeric (watts), `duration` is a string using `h`/`m` tokens.
- Optional profile file path as an alternative to inline profile input.

### Outputs (Sensors)
- `cost_now`
  - Computes cost if the appliance starts now.
  - Optionally includes export offset using current export power (steady-state assumption).
- `cost` (time series)
  - Provides cost for starting at each available future price slot.
  - Exposed as a single sensor with an attribute list (no per-slot sensors).
- `cost_min_time`
  - Best start time based on lowest cost in available data.
  - Optional window constraints: start-by or finish-by deadline.
  - Attributes include `cost`, `start`, and `finish`.
- `cost_min`
  - Minimum cost value available in the time series.
- `cost_max`
  - Maximum cost value available in the time series.
- `cost_now_percentile`
  - Percentile of `cost_now` within the future-only cost range.

### Behavior
- Event-driven updates: recompute on any input change.
- Granularity follows the price sensor schedule; integrate over rate windows for accuracy.
- No battery modeling in v1.
- Only one import price sensor supported in v1 (user can pre-fuse if needed).

### UX / UI
- Custom integration in `files/custom_components/energy_cost_forecast`.
- One config entry per appliance instance.
- Sensors grouped as a device for discovery and management.
- Provide a clear, short name per instance for entity naming.

## Plan
1. Scaffold integration structure for `energy_cost_forecast` with config flow and data coordinator.
2. Implement parsing/validation of the power profile input (inline string or file path).
3. Implement cost simulation:
   - Convert profile into a time-power series.
   - Integrate against rate windows for each candidate start time.
4. Expose sensors:
   - `cost_now`
   - `cost` with attribute list of `{start, finish, cost}`
   - `cost_min_time` with `cost`, `start`, `finish`
   - `cost_min`, `cost_max`
   - `cost_now_percentile`
5. Wire update triggers for price/power sensor state changes.
6. Validate behavior against a sample Octopus rates entity.
