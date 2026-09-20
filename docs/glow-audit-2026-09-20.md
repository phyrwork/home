# Glow configuration audit — 20 September 2026

Read live HA device/state registry and energy statistics; checked the installed Glow 5.0.0 pulse-meter configuration against its upstream source.

## Configuration and changes

- Device: Home Assistant Glow 5.0.0, ESPHome 2026.5.3, IP 192.168.1.152.
- Installed roof capacity helper: 2400 W. This is configured capacity, not physical verification of the panels.
- Pulse calibration: **1000 imp/kWh = 1 Wh/pulse**. One pulse per 100 Wh would mean 10 imp/kWh, a factor of 100 different. Preserved the existing calibration pending confirmation of the physical generation meter's printed pulse constant.
- Internal Filter: changed from **1000 µs (1 ms)** to **10000 µs (10 ms)**. Read back and confirmed 10000. Glow persists this setting. No firmware upgrade required.
- Upstream Glow 5.0.0 uses ESPHome pulse_meter without overriding its default EDGE filter mode. A 10 ms edge filter rejects closely spaced repeated edges; it is not a 2.5 kW power limit. At 1000 imp/kWh, 2.5 kW gives one genuine pulse every 1.44 seconds. The accepted edge interval remains comfortably below that.
- HA already capped `sensor.solar_generation_meter_power` at the installed 2400 W. Changed the template to permit 100 W headroom and clamp negative readings to zero: 0–2500 W with current installed capacity. This clips spikes rather than averaging consecutive measurements. It cannot remove all duplicated pulses, particularly plausible-looking ones below the cap.
- Deployed only that template, backed up as `/config/templates/solar_generation_meter_power.yaml.before-glow-audit-20260920`, and reloaded templates without restarting HA. Verified live numeric state and local boundary checks at −1, 0, 1200, 2450, 2500 and 900000 W.
- No energy history or pulse calibration was changed. Firmware-side pulse filtering can affect future pulse-total and daily-energy readings; the HA graph cap affects neither energy counter.

## Energy accounting

The selected Roof PV energy source is Glow Daily Energy: an integral of its pulse-derived power. Glow Total Energy instead counts pulses directly. Both share the same photodiode and are vulnerable to false pulses.

For seven complete local days, 13–19 September:

- Daily Energy sum: **52.22993 kWh**.
- Pulse Total Energy increments: **51.80500 kWh**.
- Difference: **0.42493 kWh**, about 0.82% of the pulse total, or 0.061 kWh/day.

On 20 September, the readings were **12.33377 versus 12.01100 kWh**, a 0.32277 kWh difference. Thus integrated-power artefacts appear small compared with the roughly 13 kWh/day untracked residual. This does not rule out systematic double counts affecting both series.

Validate the physical generation meter's register increase against both Glow counter increases over the same daylight interval. That also distinguishes pulse duplication from the power-integration discrepancy. It was dark during deployment, so only configuration acknowledgement and template behavior were verified; effectiveness against false solar pulses remains unmeasured.

## Rough battery/inverter loss allowance

HA identifies the inverter as **Solis S5-EH1P6K-L**. The repository's battery controller currently assumes 95% charge and 95% discharge efficiency. These are model assumptions, not measured efficiencies. Two 95% conversions compound to 90.25% before battery losses/auxiliaries: around 3 kWh conversion loss for about 30 kWh throughput. A rough planning allowance of **4–6 kWh/day total system losses** for that cycling volume is reasonable to investigate, allowing additional cell/cable and standing consumption. It is not a measured result or the manufacturer's battery-path efficiency rating; peak PV-conversion efficiency does not establish battery round-trip efficiency.

Crucially, total system losses are not all necessarily in HA's household residual. If battery charge/discharge energy is measured at the DC battery terminals, internal battery loss appears in the difference between energy charged and subsequently discharged at equal SOC. The household balance subtracts those terminal flows, so conversion/auxiliary losses outside that boundary contribute to residual, while internal battery loss must not be added again. Verify counter measurement boundaries before introducing a loss estimate into Energy accounting.

The 281 W dark, low-battery-flow residual remains unexplained. Do not label all of it inverter standby or assume the full 13 kWh/day is battery loss.

## References

- [Glow 5.0.0 release and runtime setting precedence](https://glow-energy.io/blog/release-5.0.0/)
- [Glow filter adjustment guidance](https://glow-energy.io/docs/configuration/internal_filter/)
- [Installed Glow pulse and energy configuration](https://github.com/klaasnicolaas/home-assistant-glow/blob/5.0.0/components/pulse_meter.yaml)
- [ESPHome EDGE/PULSE filter semantics](https://esphome.io/components/sensor/pulse_meter/)
