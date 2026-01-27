# Energy ROI Report (Batteries, PV, Heat Pump)

## Methodology
- **Electricity**: Half-hourly import/export data from Octopus API (2025-01-01 to 2026-01-01).
- **Tariffs**: Intelligent Go import (Octopus product `INTELLI-VAR-24-10-29`) + Flux export (`FLUX-EXPORT-23-02-14`).
- **Battery dispatch**: Optimized per half-hour to minimize cost, with a **6 kW export cap**, charge/discharge efficiency 95%/95%.
- **Battery degradation**: Linear fade to **80%** at **8000 cycles**; savings scaled by capacity retention over 10 years.
- **PV**: PVGIS hourly typical-year profile for Northstowe (CB24). Converted to half-hour, scaled by kWp, with **0.4%/yr** degradation.
- **Heat pump**: Cosy 6 worst-case **SCOP 2.8 @65°C**. Gas heating + hot water estimated by subtracting July daily baseline (cooking) from each day’s total; cooking moved to electricity 1:1 kWh. Gas standing charge removed when heat pump used.
- **Finance comparison**: 10-year net vs **4% real return** on unspent capex.

## Input Data

### Tariffs
- Import: Intelligent Go (`INTELLI-VAR-24-10-29`)
- Export: Flux export (`FLUX-EXPORT-23-02-14`)
- Export cap: **6 kW**

### Battery
- Fogstar Energy 48V 16.1 kWh LiFePO4 (usable **14.651 kWh** @91% DoD)
- Price: **£1,849.99** each
- Inverter: Solis 6 kW hybrid (S5-EH1P6K-L) **£757.90**
- Install/DNO: **£500**
- Charge power: **5.12 kW** (100A @ 51.2V)
- Discharge power: **6 kW** (limited by inverter)

### Heat Pump
- Cosy 6 capex: **£2,424**
- SCOP (worst case): **2.8**
- Requires hot water cylinder (Octopus page notes inclusion if not already present)

### Gas (2025 totals from user)
- Monthly costs (standing + usage) sum: **£502.27**
- Monthly kWh totals sum (2025): **6,419.376 kWh**
- Gas conversion applied: **11.186 kWh/m3**

### PV
- Roof: 4.2 m wide × 9.3 m long, pitch ~50°, azimuth 152°
- Panel: 445 W, 22.3% eff, 1.762×1.134 m, temp coeff −0.30%/°C, degradation 0.4%/yr
- Good face (SE): **7.12 kWp** (16 panels)
- Both faces: **14.24 kWp**
- Current PV: **2.4 kWp** (assumed on good face)
- Capex (mid): **£85/panel**, **£1,400 scaffold per elevation**
  - Good face: **£2,760**
  - Both faces: **£5,520**
- PVGIS yield (Northstowe):
  - Good face: **~1,050 kWh/kWp/yr**
  - Bad face: **~471 kWh/kWp/yr**

## Scenarios
Axes:
- PV: **none**, **good face replace**, **both faces**
- Batteries: **1 / 2 / 3**
- Heat pump: **yes / no**

Total: **18 scenarios**

## Top 5 scenarios (ranked by 10-year net vs 4% real return)
1) **PV good face + 2 batteries + no heat pump**
   - Capex: **£7,717.88**
   - Avg savings: **£2,401/yr**
   - Payback: **~3.21 yrs**
   - 10-yr net vs 4% invest: **+£12,587**

2) **PV good face + 1 battery + no heat pump**
   - Capex: **£5,867.89**
   - Avg savings: **£2,056/yr**
   - Payback: **~2.85 yrs**
   - 10-yr net vs 4% invest: **+£11,877**

3) **PV both faces + 2 batteries + no heat pump**
   - Capex: **£10,477.88**
   - Avg savings: **£2,725/yr**
   - Payback: **~3.84 yrs**
   - 10-yr net vs 4% invest: **+£11,745**

4) **No PV replacement + 2 batteries + no heat pump**
   - Capex: **£4,957.88**
   - Avg savings: **£1,903/yr**
   - Payback: **~2.60 yrs**
   - 10-yr net vs 4% invest: **+£11,694**

5) **PV both faces + 1 battery + no heat pump**
   - Capex: **£8,627.89**
   - Avg savings: **£2,427/yr**
   - Payback: **~3.56 yrs**
   - 10-yr net vs 4% invest: **+£11,497**

## Key Findings & Insights
- **Good-face PV replacement is the best incremental PV investment**; the bad face yields ~45% of the good face per kWp.
- **2 batteries** is the sweet spot for ROI; 3 batteries add capex faster than savings.
- **Heat pump (worst-case SCOP 2.8)** reduces ROI across scenarios but remains net-positive in most cases.
- **Scenario 4 → Scenario 1 upgrade** (add good-face PV later):
  - Extra capex **£2,760**, extra savings **~£498/yr**
  - Simple payback **~5.54 yrs**
  - 10-yr net gain vs no-upgrade **~£2,219**
  - 10-yr net gain vs 4% invest **~£893**

## Notes & Limitations
- PV uses **PVGIS typical-year** (not your specific 2025 weather).
- Heat pump modeled with **worst-case SCOP** and assumes full electric cooking.
- Gas baseline uses **July daily average** as cooking-only; adjust if you want a different baseline method.
- Export cap fixed at **6 kW**.
