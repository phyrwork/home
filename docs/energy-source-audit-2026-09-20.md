# Energy sources and export-history repair — 20 September 2026

Audited against Home Assistant Core 2026.9.0, frontend 20260826.4, live entity attributes, recorder metadata, and retained state history. The electricity meter serial is `21L4421345`; import MPAN is `2700007165105`, export MPAN is `2700009249389`. Gas is `e6s15267292161/7629106410`.

## Expected sensor shapes

HA uses cumulative **energy**, with `device_class: energy`, `state_class: total` or `total_increasing`, and supported energy units such as kWh. Instantaneous **power** uses `power` / `measurement` / W. State of charge uses `battery` / `measurement` / %. Water volume uses `water` / `total` or `total_increasing` / m³. See [HA’s source requirements](https://www.home-assistant.io/docs/energy/faq/#troubleshooting-missing-entities).

`total_increasing` supports counters that reset to zero; daily resets do not disqualify an energy sensor. `total` supports explicit `last_reset` changes and downward corrections. A price (GBP/kWh) is different from accumulated cost (GBP). See [sensor statistics and reset semantics](https://developers.home-assistant.io/docs/core/entity/sensor/#how-to-choose-state_class-and-last_reset).

## Main sources

| Selected source | Observed shape and behavior | Result |
|---|---|---|
| Grid import: Octopus `…2700007165105_current_accumulative_consumption` | kWh, energy, total, explicit daily `last_reset`; numeric and current | Matches the exact Home Mini source recommended by Octopus integration documentation. |
| Grid import cost: `…2700007165105_current_accumulative_cost` | GBP, monetary, total, daily reset; can decrease with tariff corrections | Correct accumulated-cost source, rather than a unit price. |
| Grid export: `sensor.current_accumulative_consumption_export_electricity_21l4421345_2700009249389` | kWh, energy, total, non-resetting integral of the live positive-export power sensor | Correct shape. Its source was unavailable because deployed YAML joined `state:` into the availability template. Fixed. Keep this source, as requested. |
| Export rate: `…2700009249389_export_current_rate` | GBP/kWh, measurement; 0.12 throughout retained minute history | Correct for the current-price option. HA creates `…2700009249389_compensation_2`, in GBP, monetary/total with explicit resets. Rebuilding energy history also requires rebuilding this compensation history. |
| Roof PV: `sensor.solar_generation_meter_daily_energy` | kWh, energy, total_increasing; increases during generation and resets to 0 at local midnight | Correct. Ten observed daily resets behaved as expected. |
| Battery out: `…garage_inverter_total_energy_discharged` | kWh, energy, total_increasing; 555→826 kWh in retained history; no decreases | Correct assignment to energy **from** battery. Values have 1 kWh granularity. |
| Battery in: `…garage_inverter_total_energy_charged` | kWh, energy, total_increasing; 575→866 kWh; no decreases | Correct assignment to energy **to** battery. Values have 1 kWh granularity. |
| Battery SOC: `…garage_inverter_remaining_battery_capacity` | %, battery, measurement; observed 2–100% | Correct. Configured battery capacity is 32 kWh. |
| Gas use: `…7629106410_current_accumulative_consumption_kwh` | kWh, energy, total, explicit daily reset | Correct: gas can be supplied as energy, not only as gas volume. This is the documented Home Mini gas choice. |
| Gas cost: `…7629106410_current_accumulative_cost` | GBP, monetary, total, daily reset | Correct documented accumulated-cost choice. |

The electricity and gas selections match [Octopus’s Home Mini Energy setup](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/setup/energy_dashboard/). Its delayed previous-consumption data must be read through `octopus_energy:…` external statistics to preserve the actual consumption timestamps; ordinary previous-day sensor states would put the data on retrieval dates. That external history is used only where minute history is missing.

Solar daily totals are supported by [ESPHome’s total daily energy sensor](https://esphome.io/components/sensor/total_daily_energy/). Battery roles were checked against [HA battery guidance](https://www.home-assistant.io/docs/energy/battery/) and the installed [Solis integration](https://github.com/hultenvp/solis-sensor).

## Live power sources

- Battery originally selected `…battery_power_inverted`, an unavailable restored helper whose prior statistics ended on 27 August. It now selects the actual Solis `…battery_power` directly, following the stated positive-discharge convention. **The subsequent browser audit below contradicts that sign assumption; it must be checked against SOC and charge/discharge history before treating this selection as correct.** No inversion is currently applied.
- Grid now selects `sensor.octopus_energy_electricity_21l4421345_2700007165105_current_demand`: W, power, measurement, positive import / negative export, updated approximately every minute.
- Solar now selects `sensor.solar_generation_meter_power`: W, power, measurement. These grid and solar power inputs were previously absent from Energy preferences.
- The existing export-power sensor retains its name and formula `max(-current_demand, 0)`. It now also declares `state_class: measurement`. Its source availability is respected.
- Export integration uses the requested **left** method with a one-minute maximum subinterval. Left uses the preceding sample; it is not mathematically guaranteed to underestimate every waveform. See [HA Integral](https://www.home-assistant.io/integrations/integration/).

## Individual devices and water

Every electricity device below has kWh / energy / total_increasing and sum statistics. These are cumulative counters, not instantaneous consumption samples. See [HA device-energy guidance](https://www.home-assistant.io/docs/energy/individual-devices/).

| Device / selected entity | Integration | Audit result |
|---|---|---|
| Dishwasher / `sensor.dishwasher_socket_energy` | Integral | Shape correct; numeric, increasing. |
| Washing machine / `sensor.washing_machine_energy` | SmartThings | Shape correct; 523.1→526.7 kWh. Selected power is a W/measurement derivative template, so it is an estimate from a coarse energy counter. |
| Fridge / `sensor.fridge_socket_energy` | Integral | Shape correct; three 0.01 kWh downward corrections around reloads. These are small counter/rounding corrections, not daily resets. |
| Connor’s desk / `sensor.connors_desk_socket_energy` | Integral | Shape correct; available but unchanged at 5.04 kWh during retained history. |
| Kettle / `sensor.kettle_socket_energy` | Integral | Shape correct; 149.65→152.69 kWh. |
| Living room TV / `sensor.living_room_tv_socket_energy` | Matter | Shape correct; 110.353→111.701 kWh. |
| Dehumidifier / `sensor.dehumidifier_socket_energy` | Matter | Shape correct; available, unchanged at 22.437 kWh; selected power also has correct W/measurement shape. |
| Coffee machine / `sensor.coffee_machine_socket_energy` | Matter | Attributes correct, but energy and power are unavailable. Last hourly statistics: 21 August, 17:00 UTC. No numeric readings in retained history. |
| Connor’s PC / `sensor.connor_s_pc_socket_energy` | Matter | Shape correct; available, unchanged at 29.551 kWh; selected power is W/measurement. |
| EV charger / `sensor.ev_charger_energy_meter_energy` | Shelly | Shape correct; cumulative physical meter, 2618.50409→2893.57867 kWh. |
| Irrigation / `sensor.lawn_irrigation_volume_total` | Integral | m³ / total and sum statistics, but **missing `device_class: water`**. HA validation reports this. Unchanged at 13.92 m³. |

Device integration references: [SmartThings](https://www.home-assistant.io/integrations/smartthings/), [Matter](https://www.home-assistant.io/integrations/matter/), [Shelly](https://www.home-assistant.io/integrations/shelly/). Irrigation requirements: [HA water](https://www.home-assistant.io/docs/energy/water/). The unavailable coffee plug and irrigation classification are separate from the electricity-export failure; they were not changed by this repair.

## Battery consumption versus re-export

For each statistics interval, HA calculates:

`home use = grid import + solar + battery discharge − grid export − battery charge`

It then estimates destinations using an allocation order: account for excess grid import charging the battery, solar charging, solar export, battery export, remaining grid charging, then household consumption from remaining solar, battery, and grid. Thus battery discharge exported to the grid is excluded from battery energy used by the home.

The solar self-consumption gauge estimates whether stored energy originated from solar or grid using a last-in-first-out stack within the selected period. Battery energy carried in from before that period has unknown provenance. These are accounting assumptions, not separately measured wires or an exact arbitrage-profit ledger. Coarse battery counters and different update times can affect small-interval attribution. Source: [installed frontend’s energy calculations](https://github.com/home-assistant/frontend/blob/20260826.4/src/data/energy.ts).

## Recovery method and limits

The repair utility is `scratch/backfill_export_statistics.py`, with a read-only preview by default and a required complete SQLite backup before applying changes while HA is stopped.

- Reconstruct export-power history from each retained demand reading, leaving unavailable demand unavailable.
- Prefer minute demand for energy integration wherever complete; aggregate that result into HA’s native five-minute and hourly statistics.
- Use timestamped Octopus export/compensation history where older raw readings have been purged. Existing history is retained for gaps where neither is available.
- Four incomplete hours on 11 September need metered hourly totals; only the missing part of their timing can be estimated. Very short unavailable gaps bracketed by import readings are explicitly reported as estimated zero export.
- Preserve the live integral and compensation state at the cutover, and repair cumulative statistics consistently to prevent a subsequent jump.
- Import, gas, PV, and battery energy statistics are not rewritten. Correcting export changes HA’s calculated home-consumption and battery-flow results automatically when it reloads the statistics.

Raw readings are retained for approximately ten days here. HA’s five-minute/hourly statistics are separate from raw states; a template reload alone cannot rebuild them. See [HA statistics storage](https://data.home-assistant.io/docs/statistics/).

## Applied repair

The database repair committed successfully on 20 September 2026, covering 10 November 2025 22:00 UTC through 20 September 2026 19:45 UTC. SQLite `quick_check` passed.

- Export-power history: 14,446 reconstructed minute-source rows inserted; 829 existing rows corrected against demand.
- Export-energy and compensation: 15,066 hourly rows and 6,156 five-minute rows written across the two series.
- Source coverage: 252 hours of retained minute-demand reconstruction; 7,089 hours of Octopus historical statistics; 193 older gap hours retaining existing data rather than claiming recovery.
- Restored local-day export: 13 September **26.803020 kWh**; 19 September **35.550135 kWh**; 20 September through cutover **35.371394 kWh**. Corresponding 20 September export compensation: **£4.244567**.
- Full pre-repair recorder backup on HA: `/config/export-repair-20260920/recorder-before.sqlite`. Applied report: `/config/export-repair-20260920/applied.json`. Original template and Energy preferences are backed up in the same directory.

The live-derived export source, original sensor name, and original cumulative counter state were retained. The temporary direct-meter alternative is disabled and is not selected in Energy preferences.

### Post-restart verification

SSH reauthorization succeeded and HA restarted successfully. Grid, export, solar, gas, and Solis battery sources are available, and all four main Energy-source validation groups pass.

The Energy API reports 19 September export of **35.550135 kWh / £4.266016** and 20 September export of **35.371394 kWh / £4.244567**. New recorder intervals at 19:45 and 19:50 UTC continued from repaired cumulative sums without a jump: export **2291.634212 kWh**, compensation **£276.787065**, while preserving the running integral state of **2369.44 kWh**.

All **14,446** demand-matched reconstructed power rows were checked against `max(-demand, 0)`: **zero mismatches**. Deployed configuration confirms `method: left` and a one-minute maximum subinterval. Database integrity and both reconstruction behavior tests passed. The only remaining Energy validation findings are the previously unavailable coffee-machine plug and irrigation's missing water device class.

### Water-sensor follow-up

After committing the export repair as `f9676c9`, added a persistent `homeassistant.customize` entry declaring `sensor.lawn_irrigation_volume_total` as `device_class: water`. Reloaded core customization and refreshed the entity without restarting HA. Its value remained **13.92 m³**, with `state_class: total`, and Energy water-source validation now passes. The original configuration is backed up on HA as `/config/configuration.yaml.before-water-class-20260920`. The coffee-machine plug remains the unrelated outstanding availability issue.

## Browser chart audit after water commit `4455e2d`

Opened the built-in Energy dashboard in the internal browser and inspected Summary, Electricity, Gas, Water, and Now on 20 September. No new live configuration or statistics changes were made during this inspection: 1Password sign-in timed out and SSH signing failed, so independent history verification and correction remain pending reauthorization.

### Power chart sign mismatch

The user's Tuesday 02:12 tooltip shows battery +4.53 kW, grid +5.28 kW, and home consumption 9.81 kW. At 08:52 it shows solar +0.770 kW, battery −4.952 kW, grid −4.917 kW. HA's [installed power-chart implementation](https://github.com/home-assistant/frontend/blob/20260826.4/src/panels/lovelace/cards/energy/power-sources-graph-data.ts) sums signed solar, battery and grid and clamps negative consumption to zero. It therefore needs positive battery discharge and negative charging.

Inverting the battery values would yield plausible household loads of **0.75 kW** and **0.805 kW**, respectively; the current configuration instead gives 9.81 kW and a negative sum clipped to zero. The Now page reproduces the inflated charging peaks and clipped household power. The battery controller's existing `battery_power_sign: positive_means_charging` setting corroborates the inversion hypothesis. Still verify rising SOC/charged-counter against positive raw power, and falling SOC/discharged-counter against negative raw power before applying it. My earlier selection accepted the stated sign convention without resolving this contradictory controller setting.

If confirmed, use Energy's `power_config: {stat_rate_inverted: sensor.garage_inverter_telemetry_garage_inverter_battery_power}` so HA manages the inversion helper. Do not simply select a stale `_inverted` helper as a direct source. The raw source's recorded sign is not corrupt; assess the generated helper's historical coverage separately before promising that old charts are repaired.

### Other cards and their limits

| Cards inspected | Observation / accounting |
|---|---|
| Electricity usage, energy distribution, totals | 71.14 kWh imported + 12.33 solar + 34 discharged − 35.37 exported − 34 charged = **48.10 kWh home use**. This aggregate identity balances; these cards use energy counters, not the incorrectly signed power source. It does not independently certify meter accuracy. |
| Grid energy balance / net-import gauge | **71.14 − 35.37 = 35.77 kWh**. Gross imports and exports include legitimate battery arbitrage. |
| Costs | **£5.53 import − £4.24 export = £1.29** electricity; gas £0.93; combined £2.22. Battery charging is not billed again as a separate source. |
| Solar production | **12.33 kWh**, from the daily energy sensor; independent of battery power sign. |
| Individual devices / detail | EV charger dominates, roughly 30 kWh. These are breakdowns of home consumption, not extra loads added to the home total. Negative untracked intervals appear in the detail graph; coarse 1 kWh battery counters and asynchronous measurements can cause interval mismatch. Exact contributing intervals still need numeric verification. Coffee-machine data remains unavailable. |
| Energy-flow Sankey | Shows both grid and battery on the source and destination sides because each can receive and supply energy over the selected day. Repeated labels are expected. Flow destinations use the allocation assumptions described above, not measured energy provenance. |
| Self-consumed solar gauge | **13%**; uses interval allocation and an in-period battery provenance estimate. Export-heavy operation plus coarse counters makes this less robust than solar production or net grid totals. |
| Self-sufficiency gauge | **0%** is explained by the installed frontend's formula `100 * (1 - min(1, gross_grid_import / home_consumption))`. It includes imported energy later re-exported in the numerator. This is a limitation for arbitrage, not evidence of absent solar contribution. [Source](https://github.com/home-assistant/frontend/blob/20260826.4/src/panels/lovelace/cards/energy/hui-energy-self-sufficiency-gauge-card.ts). |
| Low-carbon gauge / distribution | **85% / 60.5 kWh**. The gauge uses estimated fossil grid energy over gross imports plus non-exported solar; it does not track the carbon provenance of battery re-exports. [Source](https://github.com/home-assistant/frontend/blob/20260826.4/src/panels/lovelace/cards/energy/hui-energy-carbon-consumed-gauge-card.ts). |
| Gas consumption / totals | Consistently **7.88 kWh / £0.93**. Independent of electricity flow accounting. |
| Water flow | No data for the selected day. The classified irrigation cumulative value has stayed at 13.92 m³; classification alone does not create water usage. |

Card roles checked against [HA Energy card documentation](https://www.home-assistant.io/dashboards/energy/). Outstanding work: authenticate, prove battery sign from synchronized history, apply the appropriate power configuration, inspect inverted-history coverage, and refresh/verify the power charts. Preserve the requested minute-derived export integration and its left method.

### Battery sign confirmed after authentication retry

Authentication succeeded. On 15 September, local 00:00–05:00 hourly mean raw battery power was +4.47 to +4.83 kW; the charged counter rose by 24 kWh, the discharged counter did not change, and mean SOC rose from 29.5% to 84.2%. During 06:00–11:00, raw power was −4.27 to −4.75 kW, charging remained zero, discharge increased by 21 kWh, and mean SOC fell from 84.4% to 31.0%. The sensor is unequivocally **positive charging / negative discharge**.

Updated Energy's battery `power_config` to `stat_rate_inverted` referencing the original Solis power sensor. HA now manages `sensor.garage_inverter_telemetry_garage_inverter_battery_power_inverted`; initial verification showed raw −186 W and corrected +186 W. Battery energy counters, SOC, grid inputs and all costs were preserved.

The managed helper had only 20 hourly statistics versus 714 for the original sensor. Prepared the one-off `scratch/repair_battery_power_statistics_20260920.py` to reconstruct both hourly and retained five-minute helper statistics from the original source. It creates a full recorder backup, copies timestamp/mean weight, negates mean, swaps and negates min/max, and verifies every reconstructed row. Original raw power history remains correctly recorded in its native sign convention.

### Applied battery power repair and final verification

The repair completed with **714 hourly** and **3,003 five-minute** rows and **zero mismatches**. Hourly source coverage starts 21 August 2026 18:00 UTC; five-minute source coverage starts 10 September 03:15 UTC. No history is claimed before the source coverage. Full 760.8 MB recorder backup: `/config/battery-power-repair-20260920/recorder-before.sqlite`; repair report: `/config/battery-power-repair-20260920/applied.json`. Both database integrity checks passed. HA was stopped for the offline repair and restarted automatically afterward.

After restart, the API verified 12 historical hours of inverted mean/min/max against the source. Solis recovered with raw power **−191 W**, inverted power **+191 W**, SOC **18%**, charged energy **866 kWh**, and discharged energy **827 kWh**. The refreshed internal-browser Now view showed **319 W** household consumption, battery discharge feeding the home, historical charging below zero and discharge above zero. The large remaining household peaks coincide with the EV charging periods visible in the device-detail chart. Electricity energy totals and costs remained unchanged on reload.

The battery sign and missing helper statistics are now repaired. The self-sufficiency/solar-provenance limitations and coarse-interval attribution caveats described above remain properties of the dashboard accounting; they are not repaired by changing power sign. The existing minute-derived export source and left integration method were preserved.
