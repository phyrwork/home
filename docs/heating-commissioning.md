# Room heating: one-time setup

Room heating uses Versatile Thermostat (VTherm) with Sonoff TRVZB radiator valves
and separate room thermometers. VTherm directly controls each valve's opening and
closing entities. The garage thermometer supplies the closest available outdoor
temperature proxy; it does not control heating in the garage.

VTherm entries are created through Home Assistant's config flow. The inventory in
`deployment/group_vars/homeassistant.yaml` records the intended settings for
rebuilding them; Ansible does not apply those entries or their tuning. The valve
temperature limits are also live device settings. Configure these once, then
verify them after replacement, reset or migration. Do not reapply them on a timer.

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
