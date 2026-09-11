# Solis load-following investigation — 11 September 2026

Result: Feed-in Priority supplied a roughly 2 kW oven load step with grid peak shaving OFF, then reduced output when the oven was switched off. This demonstrates the requested behavior on the current configuration, although residual grid import remains and the earlier failure is not explained conclusively. No peak-on comparison, firmware update, or support submission was performed. Final SOC was 12%; discharge testing was stopped. HA controller and watchdog remain disabled.

## Controller isolation

- Legacy `solis_cloud_control` entry remains disabled and not loaded; Solis TOU is absent.
- House battery controller YAML include commented out in live `/config/configuration.yaml`.
- Live configuration backup: `/config/configuration.yaml.before-load-following-20260911T203052Z`.
- `automation.house_battery_stale_heartbeat_sentinel` turned off before restart, stopping its running actions.
- Configuration check passed. HA restart was delayed by the controller shutdown routine attempting to use missing control entities. Core logged stop/final-write timeouts, then started again at about 21:35 BST. At 21:36:08 BST the API confirmed the controller absent from loaded components, watchdog off, and no active HA inverter writer.
- This is a temporary live configuration change; the workspace deployment configuration remains unchanged. Do not redeploy it during testing.

## Initial observations

21:29–21:35 BST:

- S5-EH1P6K-L, model 3105, reported firmware 4D0051; no PV.
- Feed-in Priority, mode word 112; grid charging allowed; reserve enabled at 10%.
- Grid peak setting 43483 = 0 (off); stored cap 100 W.
- All twelve TOU slots disabled, enable word 43707 = 0.
- Maximum charge/discharge currents 100 A; BMS allowances 280 A each.
- SOC 10%; 51.6 V; 0.6 A / 30 W discharge; BMS fault words both zero.
- Input meter-placement word 33250 = 2, indicating Grid in the legacy protocol. Meter type/placement word 43140 = 260 (0x0104); the SolisCloud device-info code maps the low byte 4 to Eastron Standard 1P Meter, matching the configured family.
- Meter phase A and total power both -329 W (import), from live input words 33257–33264.
- Export-control holding word 43073 = 0, cap word 43074 = 0; corresponding EPM input words 33247–33249 zero. Export limiting appears disabled.
- Battery sample reported house load 268 W and inverter AC grid-port power -85 W. Meter and battery blocks were read separately several seconds apart, so do not infer an exact energy balance.
- Cloud protection settings: ForceCharge SOC 7%, OverDischarge SOC 10%. Direct holding 43018 = 7; 43010 = 100, whose scaling differs from the older protocol and needs exact-device mapping before interpreting.

At the initial 10% SOC, it was too close to reserve to draw conclusions about normal load following. Original preparation plan (subsequently shortened by the user): temporary scheduled charge, target 20% as accepted by this firmware, manually supervised stop at 16%, hard end after 30 minutes. Restore the slot before baseline testing. Stop discharge testing at 14% SOC or on loss of reliable telemetry; peak must be left off.

## Documentation checked

- [Solis S5-EH1P-L manual v1.3](https://www.bimblesolar.com/docs/Solis_Manual_S5_EH1P_3_6_K_L.pdf), printed pages 69–70: Feed-in Priority supports loads from PV, then battery, then grid; outside defined TOU periods it returns to ordinary Feed-in Priority logic. Meter setup distinguishes Grid and Load placement.
- [Solis Smart Control v1.0, March 2025](https://solarshop.baywa-re.lv/en/documents/solis-inverters-s6eh3p8k02nvydl-s6-eh3p8k02-nv-yd-l-0/381/96.pdf), slide 15: identifies live battery, home load, inverter AC port and meter input registers, and lists S5-EH1P(3–6)K-L among supported products. Remote dispatch is a distinct control interface.
- [Older hybrid protocol translation](https://www.photovoltaikforum.com/core/attachment/202315-modbus-solis-rhi-5k-pdf/): useful as a cross-check of meter input encodings, not authoritative for every setting on this firmware.

## Evidence

- `solis-protocol-fixtures/load-following-initial-controls-2026-09-11.json`
- `solis-protocol-fixtures/load-following-events-2026-09-11.jsonl`

## Charging preparation

At 21:36:48 BST, charge slot 6 was enabled after its full parameters were read back: target 20%, current 100 A, 21:35–22:06. Peak stayed off, mode stayed 112, reserve stayed 10%. Initial slot values were `[100, 0, 495, 0, 0, 0, 0]`, all slots initially disabled. The supervised charge stops at 16% or before the scheduled deadline, then restores the slot.

At 21:37:24 BST live battery charging was 5,011 W / 95.1 A, SOC 10%, no BMS faults. At 21:37:55 SOC reached 11%.

Read-only checks at 21:38 BST: newer remote dispatch main switch 44100 = 0, its system-limit switches 44102 = 0; older dispatch block 43132–43136 all zero. Input 34502 = 0xAA55 advertises the newer interface. Separate force-charge grid-power setting 43027 = 50 (previously established as 500 W); left unchanged.

## Controls kept distinct

- Feed-in Priority: storage mode word 43110 = 112 (with reserve/grid permission bits).
- Self-Use: different storage-mode selection; not selected during the initial inspection or charging preparation.
- Peak-Shaving work-mode flag: legacy integration refers to 43110 bit 11. Earlier exact-device testing did not establish this as the working grid-peak control. It remains untouched in this investigation.
- Grid Peak Shaving within this device's Feed-in Priority settings: independently commissioned CID 9023, holding 43483 bit 7, with CID 9024 cap at 43488. This is the control designated for the later off/on/off comparison.
- Separate force-charge grid-power setting: 43027 = 50, previously mapped to 500 W; unchanged. A similarly worded label must not be treated as proof it is the same switch as either peak-shaving control.

At 21:39:02 BST, during preparation, the meter showed 5,850 W import while the inverter AC port drew 5,607 W and reported house load was 235 W. These close-in-time readings support correct meter sign and grid placement. Battery charging was 5,014 W; the AC/battery difference is not interpreted as a calibrated efficiency measurement.

At approximately 21:42 BST a live FC04 response failed the requested frame shape/length check. The raw failed response was not captured by that initial helper version, so the exact mismatch is unknown. Its cleanup disabled charge slot 6 and restored its original values, independently verified at 21:42:22 BST (all enables 0, mode 112, peak 0). The diagnostic helper now records invalid frames and allows one read-only retry; two invalid responses still abort the charge. No load-following experiment had begun.

Fresh live data at 21:42:58 and 21:43:27 BST showed SOC 12%, no faults, about 197 W battery discharge with peak off, mode 112, all schedules off. Inverter AC output was 80–88 W against about 205–210 W reported house load. This is only a post-charge observation near reserve, not the formal load-response baseline.

Charging resumed at 21:43:33 BST after re-verifying ownership and all original controls. Temporary slot: target 20%, 100 A, 21:42–22:13; supervised stop still 16%. At 21:44:08 charging was 5,013 W, SOC 12%, no faults.

## User-directed early charging stop

The user said 12% was sufficient. The charge monitor was interrupted to execute its cleanup; by then SOC had reached 13%. Charge slot 6 was disabled at 21:48:03 BST and its original values fully restored/read back at 21:48:13 BST. Mode 112, all TOU slots off, grid peak off, reserve 10%, and original current limits were preserved. The monitor exited after verified cleanup; no charging process remains active. The earlier 16% preparation target is superseded by the user's instruction. Keep the next measurements brief and do not deliberately approach the 10% floor.

## Baseline, peak off

At 21:48:45 BST controls were independently verified: Feed-in Priority mode 112, peak 0, all TOU enables 0, original slot 6 restored, reserve 10%, maximum currents unchanged. Three live paired samples followed:

| BST | SOC | Battery discharge W | Inverter AC output W | House load W | Meter import W |
| --- | ---: | ---: | ---: | ---: | ---: |
| 21:48:47 | 13% | 197 | 84 | 201 | 118 |
| 21:49:01 | 13% | 192 | 72 | 188 | 124 |
| 21:49:15 | 13% | 193 | 72 | 193 | 116 |

No faults. The inverter is supplying some AC load with peak off; this is not yet proof of dynamic load following because the house load hardly varied. Next step is a brief known load on/off response with the same settings. No peak-on comparison has been performed. HA controller/watchdog remain disabled.

## Passive observation and oven step — completed

The user first chose natural load observation. From 22:07:27 to 22:11:26 BST, 17 paired live samples with no configuration writes showed:

| Measurement | Range W | Mean W |
| --- | ---: | ---: |
| House load | 156–181 | 167.4 |
| Battery discharge | 155–186 | 165.4 |
| Inverter AC output | 36–65 | 45.8 |
| Meter import | 111–128 | 119.2 |

SOC remained 13%, BMS faults zero. Controls before and after remained mode 112, all TOU slots off and grid peak off. Natural load variation was too small for a strong dynamic conclusion.

The user then offered to switch the oven on briefly. A read-only recorder verified ownership and original controls, then signalled readiness. Its next read timed out; a retry returned an unrelated FC06 acknowledgement (`0106AA5A000149C1`) to an FC04 request, with an old embedded timestamp. That response was rejected, not treated as telemetry or proof of a current write. The recorder stopped and verified unchanged controls. The user was told to switch the oven off if already on. A subsequent valid read captured the oven load, followed by the user's on/off confirmations. No invalid response is used in the table below.

| BST | State | SOC | House load W | Battery discharge W | Inverter AC output W | Meter import W |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 22:11:44 | Before oven | 13% | 211 | 212 | 87 | 112 |
| 22:12:30 | Oven on | 13% | 2221 | 2106 | 1996 | 186 |
| 22:13:01 | Switch-off transient | 12% | 208 | 2260 | 2036 | -1184 (export) |
| 22:13:19 | Settled after off | 12% | 194 | 202 | 80 | 109 |

Each paired sample reads battery/load/inverter registers first, then meter registers about a second later. Registers may also update at different times internally. In particular the transient row must not be treated as a simultaneous energy balance. It establishes a sampled export transient after the load fell; the sparse/error-interrupted capture cannot establish exact response latency, transient duration, or exported energy.

At 22:13:06 BST controls were again verified: mode 112, all TOU slots off, grid peak 0, reserve 10%, max charge/discharge 100 A, original charge slot restored. SOC was 12% and no BMS faults were present in the final read. The oven was confirmed off. All diagnostic recording processes finished.

### Conclusion and limits

The approximately 2.01 kW increase in reported house demand was met by a 1.91 kW increase in inverter AC output, while grid import increased only about 74 W between the selected samples. Output then returned to the initial low level when the oven was switched off. **Feed-in Priority can therefore dynamically support the metered load with Grid Peak Shaving OFF on this inverter and firmware.** No meter, export-limit, reserve, mode, or discharge-permission setting needed changing for this result; the battery was first charged above reserve and all scheduled slots were disabled.

This supports outcome A for the requested operating behavior with the recorded settings. It does not prove why the earlier observation differed, or that peak shaving is irrelevant in all conditions. Reserve/SOC state remains a plausible contributor to the earlier observation, not a demonstrated historical cause. The roughly 0.1–0.2 kW residual import and short export transient remain separate characteristics to investigate if tighter grid balancing is required. No peak-on comparison was performed after SOC reached the agreed stopping margin.
