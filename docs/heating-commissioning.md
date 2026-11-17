# Room heating: one-time setup

Room heating uses Versatile Thermostat (VTherm) with Sonoff TRVZB radiator valves
and separate room thermometers. VTherm directly controls each valve's opening and
closing entities. The garage thermometer supplies the closest available outdoor
temperature proxy; it does not control heating in the garage.

VTherm entries are created through Home Assistant's config flow. The inventory in
`deployment/group_vars/homeassistant.yaml` records the intended settings for
rebuilding them; Ansible does not apply those entries or their tuning. Set the
valve options below once and verify them after replacement, reset or migration.

## Pausing room heating after no motion

The kids' bedroom, downstairs room, living room, and office can pause heating
after a period without motion. VTherm uses **Open Detection → Turn off** to
close the valves. In logs and charts, `hvac_off_window_detection` for these
thermostats means the corresponding daytime vacancy sensor turned on.

| Thermostat | Motion sensors | Daytime vacancy sensor |
| --- | --- | --- |
| Kids' bedroom | `binary_sensor.kids_bedroom_motion_sensor` | `binary_sensor.kids_bedroom_daytime_vacancy` |
| Downstairs room | `binary_sensor.downstairs_room_family_room_motion_sensor_occupancy`, `binary_sensor.downstairs_room_kitchen_motion_sensor_occupancy` | `binary_sensor.downstairs_room_daytime_vacancy` |
| Living room | `binary_sensor.living_room_living_room_motion_sensor_occupancy` | `binary_sensor.living_room_daytime_vacancy` |
| Office | `binary_sensor.office_office_motion_sensor_occupancy` | `binary_sensor.office_daytime_vacancy` |

- `schedule.room_heating_vacancy_window` defines when vacancy can pause
  heating in rooms that use it. It runs daily from 06:00 to 18:00 in Home
  Assistant local time. Normal Landing control resumes at 18:00 to allow
  preheating before the 19:00 bedtime and continues overnight.
- The shared `input_number.room_heating_vacancy_delay` defaults to **60 min**.
  Deployment sets it to 60 when the helper first appears with a value of zero.
  Zero also means 60 minutes while the helper is being set up. Home Assistant
  retains positive values across restarts and deployments.
- In each listed VTherm's configuration, enable Open Detection in sensor
  mode, select its daytime vacancy sensor, choose **Turn off**,
  and set both VTherm delays to **0 seconds**. Keep the window bypass off.

The schedule must stay on and every motion sensor for the room must stay
clear for the full delay. The countdown starts from whichever state changed
most recently. Downstairs, motion in either the family room or kitchen clears
vacancy for both radiators.
With the default delay and no motion, the earliest pause is 07:00. A restart
or reload that recreates either state starts a fresh countdown. Home
Assistant checks the elapsed time every minute and when an input changes.
Motion, the end of the schedule, or any required sensor becoming unavailable
clears the pause. VTherm then applies the latest Landing request, including
Landing Eco.

After wiring the sensor, verify that vacancy commands 0% opening and 100%
closing, that the Landing-derived target can update during the pause, and that
motion and the 18:00 schedule boundary both release the pause.

## Sonoff Adaptive Mode

Set **Adaptive Mode** to **Off** on all six Sonoff TRVZB valves so VTherm's direct
valve control can set the opening and closing degrees. Sonoff's Adaptive Mode
uses the valve's own temperature controller and disables manual opening
percentage control. See the [Sonoff Adaptive Mode documentation](https://sonoff.tech/en-nl/blogs/news/trvzb-upgrade-smart-temperature-control-for-more-stable-room-temperature).

In Home Assistant, use Developer Tools → Actions:

```yaml
action: switch.turn_off
target:
  entity_id:
    - switch.kids_bedroom_radiator_adaptive_mode
    - switch.downstairs_room_radiator_a_adaptive_mode
    - switch.downstairs_room_radiator_b_adaptive_mode
    - switch.middle_bathroom_radiator_adaptive_mode
    - switch.living_room_radiator_adaptive_mode
    - switch.office_radiator_adaptive_mode
```

Verify that each Adaptive Mode switch reads **Off**. During commissioning, check
that a radiator warms with the house zone calling and VTherm commanding
100% opening / 0% closing. With the room satisfied, verify 0% opening / 100%
closing and check that the radiator cools while another room still calls for heat.

## Radiator target ceilings

Set **Max heat setpoint limit** to **35°C** on all six radiator valves. This is an
allowed target ceiling, not a room target or a boiler water temperature.
VTherm clamps the target sent to an underlying valve to that valve's allowed range.

In Home Assistant, use Developer Tools → Actions:

```yaml
action: number.set_value
target:
  entity_id:
    - number.kids_bedroom_radiator_max_heat_setpoint_limit
    - number.downstairs_room_radiator_a_max_heat_setpoint_limit
    - number.downstairs_room_radiator_b_max_heat_setpoint_limit
    - number.middle_bathroom_radiator_max_heat_setpoint_limit
    - number.living_room_radiator_max_heat_setpoint_limit
    - number.office_radiator_max_heat_setpoint_limit
data:
  value: 35
```

Verify each number reads 35 and each underlying `climate` entity has
`max_temp: 35`. After VTherm next regulates, verify the underlying target matches
the room thermostat target, allowing for the valve's supported temperature step.

## VTherm tuning

Use per-room TPI settings with central TPI inheritance disabled. Keep Auto TPI
disabled: its learning is primarily designed for on/off heating and is problematic
for TRVs because valve opening does not translate linearly to heat output.
See the [Auto TPI documentation](https://www.versatile-thermostat.org/en/docs/feature-autotpi/).

| Room | `tpi_coef_int` | `tpi_coef_ext` | Auto TPI |
| --- | ---: | ---: | --- |
| Kids' bedroom | 0.6 | 0.01 | Off |
| Downstairs | 0.6 | 0.01 | Off |
| Bathroom | 0.6 | 0.01 | Off |
| Living room | 0.6 | **0.03 (trial)** | Off |
| Office | 0.6 | 0.01 | Off |

For the living room, use:

```yaml
action: versatile_thermostat.set_tpi_parameters
target:
  entity_id: climate.living_room_thermostat
data:
  tpi_coef_int: 0.6
  tpi_coef_ext: 0.03
```

On the deployed VTherm 10.4.0, this action saves the coefficients in the config
entry and recalculates heating demand. Verify the live coefficients in the room
climate entity's `vtherm_over_climate_valve.valve_regulation` attributes. Confirm
learning remains off in `specific_states.auto_tpi_state`.

The external coefficient supplies heat-loss compensation near target. Increasing
it is the documented adjustment for a persistent shortfall after temperatures
settle; the internal coefficient controls response to the room-temperature error.
See the [TPI tuning guidance](https://www.versatile-thermostat.org/en/docs/algorithms/).
These settings are starting values, not a claim that every room is calibrated.

## Applied changes and follow-up

On **10 November 2026, approximately 18:50 GMT**:

- Raised the living-room valve ceiling from 20.5°C to 35°C. All five other valves
  already had a 35°C ceiling at the pre-change audit.
- Increased only the living-room external coefficient from 0.01 to 0.03; verified
  the saved config entry, internal coefficient 0.6 and learning disabled.
- Verified the underlying valve received the full 21°C target and reported opening
  100 / closing 0 while the room thermometer read 19.6°C. These are device command
  states, not measurements of water flow or heat output.
- Left the other room coefficients and the boiler's 43°C flow setting unchanged.

Assess the trial over two comparable heating days using room temperature, target,
valve commands, garage temperature and `switch.house_heating_zone_call_for_heat`.
Look for sustained temperature within roughly 0.2–0.3°C of target without material
overshoot, distinguishing morning recovery from settled behaviour. Successful
warm-up has not yet been established. Relay-on history does not establish that
the burner ran continuously.

To roll back the tuning trial, run the same VTherm action with
`tpi_coef_ext: 0.01` and update the inventory override and this record. The corrected
35°C valve ceiling is independent of that tuning rollback.
