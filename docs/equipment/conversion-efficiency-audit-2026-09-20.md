# Conversion efficiency evidence — 20 September 2026

Equipment: Solis S5-EH1P6K-L (HA device registry) and new Fogstar Energy FE51.2V-32-LVFB (user confirmation and repository equipment documents), 51.2 V, 628 Ah, 32.1536 kWh.

## What is actually published

- Solis's current [S5-EH1P(3–6)K-L datasheet](https://www.solisinverters.au/uploads/files/202602/S5-EH1P%283-6%29K-AU-Flyer-V2%2C7.pdf), efficiency section on page 2: maximum efficiency >97.1%, EU efficiency >96.5%. These are not stated as separate AC-to-battery and battery-to-AC efficiencies or an AC round-trip value. Do not square them to model battery cycling.
- Discover Energy Systems' [Solis battery-sizing integration document](https://docs.discoverenergysys.com/805-0033-lynk-ii-installation-manual/release/minimum-battery-system-capacity-2), footnote 2, cites 94.9% battery charge/discharge efficiency for an S5-EH1P(3–6)K-L-AU datasheet. This is a manufacturer's integration document, but a second-hand quotation of a regional Solis specification. I could not corroborate the figure or its per-direction test conditions in the Solis datasheet above. Treat it as supporting context, not a verified exact specification for this UK unit.
- Fogstar's [V4 technical specification](https://cdn.shopify.com/s/files/1/1347/0997/files/32kWh_FE_51.2V_TechSpec_V4_H_FS.pdf?v=1769779399) and [2026 heating/fire-suppression manual](https://cdn.shopify.com/s/files/1/1347/0997/files/51.2V_32KWH_USER_MANUAL_2026_FireSupression_Heating_458852e0-bb80-4158-b79d-01c60d96ca3f.pdf?v=1782471745) do not publish a numerical battery energy round-trip efficiency with test conditions.
- Fogstar's online 99% "charge efficiency" recommendation is a [Victron battery-monitor setting for Drift/Drift PRO/ECO leisure batteries](https://fogstar.zendesk.com/hc/en-gb/articles/18255295688092-Fogstar-and-Victron-FAQ-s), not a measured kWh round-trip rating for this 32 kWh battery. Do not substitute it.
- The new battery manual describes temperature-controlled heating during charging (start setting 0°C, recovery 10°C; configurable). Heating should be treated as a conditional auxiliary load, not a universal fixed efficiency reduction. No heater power or measured standby consumption was established in this audit.

## Implication

The repository's 95% charge and 95% discharge assumptions remain estimates, not manufacturer-verified installation efficiencies. At 30 kWh AC input, those two conversions give 27.075 kWh AC output and 2.925 kWh loss, before cell storage losses and auxiliaries. If 94.9% were valid independently in both directions, the analogous result would be 27.018 kWh output and 2.982 kWh loss—only about 0.057 kWh different. This conditional calculation does not resolve the much larger untracked residual.

The prior 4–6 kWh/day total-system allowance has not become a verified specification through this research. No configuration changed.

More precise installed-system results require time-aligned AC energy into/out of the inverter and DC energy into/out of the battery, separated by direction. Measure battery round-trip over cycles returning to the same SOC, preferably over multiple cycles because the current Solis energy counters are quantized to 1 kWh. Whole-house grid energy alone cannot isolate inverter losses while other loads and PV vary. Hourly power snapshots also cannot establish an exact battery efficiency.

## Expanded search and chemistry-level estimate

Expanded searches covered exact Fogstar model/628Ah identifiers, manufacturer and related Seplos equipment, certification reports, owner measurements, Solis S5 test reports and LiFePO4 cell research. No verified efficiency test for the exact new Fogstar pack was located. A Fogstar 16.1 kWh/Victron owner report gives about 74% AC round-trip efficiency, but measures different hardware and includes inverter/auxiliary losses; it is not a Fogstar cell-efficiency specification. Related Seplos MB56 single 628Ah-cell claims do not identify the cells in this 2P16S Fogstar pack.

Useful primary chemistry-level evidence:

- [Second-Life Assessment of Commercial LiFePO4 Batteries Retired from EVs, Batteries 2024, 10(9), 306](https://www.mdpi.com/2313-0105/10/9/306/html): reports CALB LFP energy round-trip efficiency around 94–95% at 0.6C charge / 1C discharge and around 98% at 0.2C. Coulombic efficiency is nearly 100%, illustrating why Ah efficiency must not be used as kWh efficiency. The indexed research text was available; a subsequent direct fetch was rate-limited.
- [Yagci et al., Electrical and Structural Characterization of Large-Format Lithium Iron Phosphate Cells Used in Home-Storage Systems (2021)](https://onlinelibrary.wiley.com/doi/abs/10.1002/ente.202000911): measured 180Ah CALB/Sinopoly cells; efficiency improves at lower C-rate and higher temperature, reaching 98% at 35°C and C/10.
- [Victron Lithium Smart manufacturer technical data](https://www.victronenergy.com/media/pg/Lithium_Battery_Smart/en/technical-data.html): specifies 92% battery round-trip efficiency for that product family. Therefore no universal 98% chemistry constant applies to every LFP pack and operating condition.

For this new 32.1536 kWh pack cycling at about 5 kW (~0.16C), **97% battery-only DC round-trip efficiency** is a defensible provisional modelling assumption at moderate temperature, with **95–98%** as a working sensitivity band, not a guaranteed bound or Fogstar specification. For 30 kWh entering the battery terminals: 0.9 kWh battery loss at 97%; 0.6–1.5 kWh across that band. If a model requires symmetric per-direction factors, sqrt(0.97) is approximately 0.9849 each way; do not apply 97% twice.

Combining 95% inverter conversion each way with 97% battery round-trip gives 0.95 × 0.97 × 0.95 = 87.5425% AC round-trip before separate auxiliaries. For 30 kWh AC input that is 26.26275 kWh output and 3.73725 kWh total conversion/storage loss. This differs from the previous 30 kWh DC-terminal example because the measurement boundary differs.

Do not add estimated internal cell losses as a household Energy device when battery in/out are already measured at its DC terminals: those losses are already excluded by the battery-flow subtraction. A separate informational battery-loss sensor can be useful, while an Energy household-device loss estimate should cover conversion/auxiliary losses outside the battery terminal boundary. No sensor or efficiency settings changed during this research.
