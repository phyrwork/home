# Untracked electricity audit — 20 September 2026

Previous changes were already committed in `33de31e`; the worktree was clean at the start of this audit. Reloaded the built-in Electricity dashboard in the internal browser.

## Result

The displayed residual is reproduced by the selected energy counters. It is not explained by the repaired battery-power sign or by coarse hourly allocation alone. **Correct subtraction does not prove complete metering or consistent AC/DC measurement boundaries.**

HA calculates household energy as import + solar + battery discharge − export − battery charge, then subtracts configured device energy. The device-detail graph separately displays positive residual and negative over-reporting intervals. Source: [installed frontend implementation](https://github.com/home-assistant/frontend/blob/20260826.4/src/panels/lovelace/cards/energy/energy-devices-detail-graph-data.ts).

| September day | Home kWh | Tracked devices kWh | Net untracked kWh |
|---|---:|---:|---:|
| 13 | 63.064 | 50.348 | 12.716 |
| 14 | 45.988 | 32.620 | 13.367 |
| 15 | 49.013 | 36.730 | 12.284 |
| 16 | 29.973 | 18.882 | 11.090 |
| 17 | 21.220 | 7.938 | 13.283 |
| 18 | 41.890 | 28.940 | 12.951 |
| 19 | 41.787 | 26.014 | 15.773 |
| 20, incomplete | 49.230 | 34.564 | 14.666 |

Complete-day mean: **13.066 kWh/day**. Summing hourly mean signed grid power + solar power − raw battery power, then subtracting the same tracked device energy, gives **13.095 kWh/day** over the same seven days. This uses a different aggregation of the power inputs, not independent calibrated meters. Individual days differ by several kWh because battery counters have 1 kWh resolution and power telemetry is sampled, but the persistent weekly residual remains.

## Residual by battery operating state

Using complete hourly power statistics for 13–19 September and subtracting configured device energy for each corresponding hour:

| Raw battery hourly mean | Hours | Mean residual power | Median |
|---|---:|---:|---:|
| Charging above 3 kW | 40 | 833 W | 753 W |
| Discharging above 3 kW | 39 | 691 W | 660 W |
| Magnitude below 0.5 kW | 65 | 265 W | 259 W |
| Other / transition | 24 | 590 W | 635 W |

A constant 265 W amounts to 6.36 kWh/day. Residual above that baseline accounts arithmetically for roughly another 6.7 kWh/day in this sample. This is a descriptive decomposition, **not proof of a 6.7 kWh/day inverter loss**: appliance use can correlate with cheap-rate battery charging, and measurement boundaries/timing need confirmation. Conversion and inverter standby losses are expected to appear as residual if grid energy is measured on the AC side while battery energy is measured at the battery. Do not remove that residual or invent a loss sensor from this correlation.

## Concrete tracking defect

The desk integral's deployment source is `sensor.connors_desk_socket_power`, but that entity is absent from the retrieved live states. The actual live power entity is `sensor.connor_s_desk_socket_power`, reporting 4.6 W during inspection. The selected desk energy entity `sensor.connors_desk_socket_energy` remains frozen at **5.04 kWh**, with zero increments for every inspected day.

Prepared the one-line correction in `deployment/config.yaml`. It has **not been deployed**: SSH key signing failed and the access skill requires user reauthorization. Its historical contribution is not yet quantified, and it should not be claimed to explain the entire residual. Next: inspect live rendered config, deploy the corrected source, and determine recoverable desk history from retained source states.

Other observations:

- The selected coffee-machine power and energy sensors are unavailable, so its consumption is missing.
- PC and dehumidifier power and energy both report zero increments during the week; this can be legitimate inactivity and is not proof of failure.
- Microwave and media-panel power entities are unavailable and are not represented by selected device energy counters. Determine whether these are obsolete entities or currently used devices before assigning missing consumption to them.
- The EV meter's energy counter and integrated hourly power agree closely (e.g. 48.514 versus 48.513 kWh on 13 September), so there is no evidence here of a large EV undercount.
- Solar daily energy agrees reasonably with the capped solar power series. Raw solar power has severe spikes on 19–20 September, but those spikes are not reflected in the selected daily-energy counter. Do not integrate the uncapped raw solar power to explain the residual.
- The local-Tuya power template lacks `state_class: measurement`, so those power entities do not have historical mean statistics. Retained raw history is required to quantify the missing desk energy.

No HA settings or database records were modified in this audit. API authentication succeeded; SSH authentication failed. Cached API credentials were removed at the end of the inspection.

## Follow-up: Glow pulse double counting

The retrieved device state identifies **Home Assistant Glow 5.0.0 / ESPHome 2026.5.3**, with pulse rate **1000 imp/kWh** and Internal Filter **1000 µs (1 ms)**. [Glow 5.0.0](https://glow-energy.io/blog/release-5.0.0/) introduced a persistent runtime Internal Filter entity; it overrides old YAML filter settings after boot. [Official guidance](https://glow-energy.io/docs/configuration/internal_filter/) recommends increasing gradually, with up to 10000 µs (10 ms) as a starting range. No setting has been changed in this follow-up.

**Correction to the solar comparison above:** [Glow's installed-release configuration](https://github.com/klaasnicolaas/home-assistant-glow/blob/5.0.0/components/pulse_meter.yaml) derives Daily Energy by integrating pulse-derived power. Total Energy comes from the pulse count. Neither provides an independent reference against photodiode double counts. HA's 2400 W cap applies to a separate power template, not the selected Daily Energy counter. Thus plausible daily values and agreement with capped power do not establish pulse accuracy. Every excess kWh of reported solar adds one kWh to calculated household and untracked consumption.

Reported solar averages 7.461 kWh/day for 13–19 September. If it were exactly twice the true production, the average false residual contribution would be 3.731 kWh/day, leaving about 9.336 kWh/day unexplained. This is a hypothetical sensitivity calculation, not a finding that the factor is two. Validate against the physical generation meter's register increment over the same interval, with its printed imp/kWh setting checked. Compare pulse-total and Daily Energy increments separately to distinguish extra counted pulses from integration artifacts.

To separate the low-battery-flow estimate from solar uncertainty, selected only the 19 complete hourly intervals with absolute mean battery power below 500 W and reported solar below 1 W. Mean grid import was **521.4 W**, battery discharge **197.4 W**, tracked devices **438.0 W**, leaving **280.8 W** net untracked. The earlier 265 W average included daylight hours and was not a direct measurement of idle appliances. The dark-period residual cannot be explained by daytime PV overcounting; it already subtracts tracked fridge/EV/etc. It may include missing desk/coffee tracking, other unmetered loads, inverter losses and meter/timing error. It is not evidence of a single 281 W appliance.
