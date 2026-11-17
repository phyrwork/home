# Room heating: one-time setup

Room heating uses Versatile Thermostat (VTherm) with Sonoff TRVZB radiator valves
and separate room thermometers. VTherm directly controls each valve's opening and
closing entities. The garage thermometer supplies the closest available outdoor
temperature proxy; it does not control heating in the garage.

VTherm entries are created through Home Assistant's config flow. The inventory in
`deployment/group_vars/homeassistant.yaml` records the intended settings for
rebuilding them; Ansible does not apply those entries or their tuning. Set the
valve options below once and verify them after replacement, reset or migration.

## Household presence and room targets

Connor's person uses only `device_tracker.pixel_8_pro`, supplied by the HA
Companion app. Sónia's person uses `device_tracker.sonia_s_phone`. Configure
these assignments in Home Assistant's People settings.

`binary_sensor.house_heating_away` turns on when both people have a known
location outside home for five continuous minutes. It turns off as soon as
either person returns home or either location becomes unknown or unavailable.
A pending departure countdown starts fresh after HA restarts. An already
confirmed away state can be restored if both locations still report away.

Household control uses this sensor:

- **Room heating follow house presence** selects `Stopped` on
  `select.central_mode` while the house is away, and `Auto` otherwise.
  Enable **Use central mode** in the main configuration of all five room
  VTherms, including the bathroom. Central stop closes the valves and removes
  their heat demand. Auto restores each thermostat's requested HVAC mode,
  subject to its room vacancy pause.
- **Landing heating follow house presence** selects the Landing Nest's
  `eco` preset while away and `none` otherwise.

**Room follow zone setpoints** copies the current numeric Landing target plus
each room's configured offset. It writes target temperatures only. This also
copies any target reported by the Landing Nest while in Eco. After Eco ends,
the next reported target is copied to the rooms.

**Master bedroom heating follow presence** selects the master-bedroom Nest's
`eco` preset when either the house is away or its room vacancy sensor is on.
Otherwise it selects `none`, returning control to the Nest's own schedule.
It does not copy Landing targets. An explicitly switched-off master Nest stays
off. Nest Eco is a temperature setback, so it can still heat below its Eco target;
it is not the VTherm valve-closing pause.

### Commissioning central control

Enable **Use central mode** through each VTherm's main options page.
Before enabling household control, confirm that all five thermostats have
requested HVAC mode `heat`. An effective `off` caused by room vacancy can
still have a requested mode of `heat`.

After changing VTherm configuration, restart Home Assistant fully and verify
that all five thermostats report `is_controlled_by_central_mode: true` and
their `specific_states.last_central_mode` matches `select.central_mode`.
On the installed VTherm 10.4.0, a config-entry reload can leave the applied
central mode unset until a real selector change or full HA restart. Apply
this restart and verification step after future VTherm options changes too.

Check that central Stopped commands opening 0% and closing 100% on all six
valves, that target changes still reach the VTherms while stopped, and that
Auto restores normal room control. Verify that an active room vacancy pause
continues to hold that room off after central mode returns to Auto.

On 17 November 2026, central control was enabled for all five VTherms and
verified after a full HA restart. A brief central Stopped test put all five
thermostats off and all six valves at opening 0% / closing 100%, preserving
their requested heat modes and targets. Writing the office's existing target
while stopped kept it off. Returning to Auto restored all five rooms to heat.
Both people were home, and the household presence automation was re-enabled
and reconciled after the test. A real household departure and return remains
to be observed.

## Room heating protection and vacancy

A protected room follows its normal thermostat target regardless of motion.
Outside protection, recent motion permits heating for the shared vacancy delay.
The daily protection schedules use Home Assistant local time (Europe/London):

| Room | Protection schedule | Protected times |
| --- | --- | --- |
| Kids' bedroom | `schedule.kids_bedroom_heating_protection` | 18:00–08:00 |
| Master bedroom | `schedule.master_bedroom_heating_protection` | 20:00–08:00 |
| Downstairs room | `schedule.downstairs_room_heating_protection` | 06:00–08:00 and 16:00–18:00 |
| Living room | `schedule.living_room_heating_protection` | 16:00–18:00 |
| Office | None | Motion controls vacancy at all hours |

Overnight schedules use separate 00:00–08:00 and evening–24:00 blocks.
Protection remains on across midnight. The previous shared
`schedule.room_heating_vacancy_window` is retired. There are no guest overrides.
Bathroom heating has no room vacancy control. Landing has household presence
control only.

The shared `input_number.room_heating_vacancy_delay` defaults to **60 min**.
Deployment sets it to 60 when the helper first appears with a value of zero.
Zero also means 60 minutes while the helper is being set up. Home Assistant
retains positive values across restarts and deployments.

For a room with protection, vacancy requires its protection schedule to be off
and every motion sensor to be clear for the full delay, measured from the later
of protection ending or motion clearing. For example, kids' protection ends at
08:00, so the earliest pause is 09:00. Office uses motion clearing alone.
A restart or reload that recreates either state starts a fresh countdown.
Home Assistant checks elapsed time every minute and when an input changes.
Motion, protection starting, or any required input becoming unknown or
unavailable clears the vacancy. Protection never overrides household away.

| Room | Motion sensors | Vacancy sensor |
| --- | --- | --- |
| Kids' bedroom | `binary_sensor.kids_bedroom_motion_sensor` | `binary_sensor.kids_bedroom_heating_vacancy` |
| Downstairs room | `binary_sensor.downstairs_room_family_room_motion_sensor_occupancy`, `binary_sensor.downstairs_room_kitchen_motion_sensor_occupancy` | `binary_sensor.downstairs_room_heating_vacancy` |
| Living room | `binary_sensor.living_room_living_room_motion_sensor_occupancy` | `binary_sensor.living_room_heating_vacancy` |
| Office | `binary_sensor.office_office_motion_sensor_occupancy` | `binary_sensor.office_heating_vacancy` |
| Master bedroom | `binary_sensor.master_bedroom_master_bedroom_motion_sensor_occupancy` | `binary_sensor.master_bedroom_heating_vacancy` |

Vacancy sensors apply outside each room's protection, including at night.
Their entity IDs and friendly names use **Heating Vacancy**. The four original
VTherm sensors retain their internal `unique_id` values so template reloads reuse
the same registry identities. `default_entity_id` supplies the current names for
new installations; existing installations are renamed through the entity registry
API so Recorder migrates their history. Update each VTherm's Open Detection sensor
through its options flow, preserving all other options, then restart HA and verify
central mode and historical events under the new IDs.

Retired `schedule.room_heating_occupancy_window` and
`sensor.kids_bedroom_last_motion_clear` registry entries are removed through HA's
entity registry API. The old per-room vacancy-delay helper is already absent.
Old restore-state cache entries expire automatically after seven days without
recreating entities; do not edit HA's running storage files.
Downstairs, motion in either the family room or kitchen clears vacancy for both
radiators.

The four VTherms use **Open Detection → Turn off** to close their valves.
`hvac_off_window_detection` means room vacancy here, not an actual open window.
Enable Open Detection in sensor mode, select the room's vacancy sensor, choose
**Turn off**, set both VTherm delays to **0 seconds**, and keep window bypass off.
On release, VTherm applies its requested mode and latest Landing-derived target,
subject to household central control. Master bedroom uses Nest Eco instead,
through **Master bedroom heating follow presence** described above.

### Commissioning protection and vacancy

Verify each schedule's daily blocks and current state. Check that protection
holds vacancy off, that the delay starts again at protection end, and that motion
clears an active pause. In particular, verify midnight does not interrupt bedroom
protection. Check downstairs with both sensors independently.

On each VTherm, verify vacancy commands 0% opening and 100% closing while keeping
the target, and that target updates still arrive during a pause. On the master
Nest, verify vacancy or household away selects Eco and releasing both selects
None without writing its target. Confirm protection cannot release household-away
Eco. Preserve an explicitly off Nest.

On 17 November 2026 at 20:45 UK time, the protection change passed Home
Assistant configuration validation and full Ansible deployment. After restart,
kids' and master bedroom protection were on; downstairs and living room
protection were off. All five vacancy sensors were available, with the restart
starting fresh countdowns. The old shared schedule was absent. All five VTherms
retained central participation, applied Auto, and their expected targets.
A brief master Nest test confirmed Eco (reported target 9.4°C), then the new
automation restored None and its previous normal target of 19.8°C. The automation
was left enabled. Natural protection boundaries, master vacancy and household-away
transitions remain to be observed under this configuration.

On 17 November 2026 at 20:58 UK time, the four original vacancy sensors were
verified under their new `*_heating_vacancy` entity IDs after migration and full
Ansible deployment. Registry entry IDs and internal unique IDs were unchanged.
Recorder retained the same metadata IDs, earliest timestamps, and historical
vacancy events under the new names, with no loss of recorded rows. Each VTherm's
only persisted configuration change was its window sensor reference. After the
restart, all five VTherms reported central Auto and requested Heat, with the new
window references applied; master Nest remained Heat/None. The two obsolete
registry entities and old per-room delay helper were absent. The old template
filename was removed and `templates/room_heating_vacancy.yaml` was installed.
A restricted migration snapshot of affected registry entries, VTherm options,
and Recorder metadata is retained at
`/config/backups/heating-vacancy-rename-20261117.json`.

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
