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

## Control

# Solis (S5 EH1P 6kW) + Fogstar 48V (1–3x 16.1kWh) + Octopus IOG/Flux
## Home Assistant local Modbus arbitrage control plan (conceptual)

**Context**
- Location: UK
- Inverter: Solis hybrid 6kW (S5-EH1P6K-L / RHI-6K-48ES-5G family)
- Battery: Fogstar Energy 48V 16.1kWh (1–3 units)
- Tariff: Intelligent Go import + Flux export
- Export cap: 6 kW
- Goal: maximise arbitrage income while maintaining reliable household supply
- Control: local Modbus (no cloud dependency)
- Assumption: Home Assistant already has Octopus rate sensors + Modbus access to inverter registers

---

## 1) Typical HA integrations for Solis (what people commonly use)

You have three “typical” options in Home Assistant:

### A) **Solis Modbus (HACS custom integration)**
- A dedicated Solis Modbus integration exists (often installed via HACS) designed to reduce Modbus overhead by batching register reads.  [oai_citation:0‡GitHub](https://github.com/Pho3niX90/solis_modbus?utm_source=chatgpt.com)
- Good fit if you want: many entities pre-defined, faster updates, and easier discovery than hand-written YAML.

### B) **Home Assistant Core `modbus` integration (DIY register map)**
- HA’s built-in Modbus integration is the baseline approach.  [oai_citation:1‡Home Assistant](https://www.home-assistant.io/integrations/modbus/?utm_source=chatgpt.com)
- Community projects provide YAML examples / register maps specifically for Solis hybrids (often via Solis datalogger sticks and Modbus/TCP).  [oai_citation:2‡GitHub](https://github.com/fboundy/ha_solis_modbus?utm_source=chatgpt.com)
- Good fit if you want: maximum control/visibility and you’re comfortable managing register maps.

### C) **Solarman local collector integration (custom)**
- Some Solis setups use a Solarman data logger/collector and integrate locally via the “Solarman” HA component.  [oai_citation:3‡GitHub](https://github.com/StephanJoubert/home_assistant_solarman?utm_source=chatgpt.com)
- Good fit if you already have Solarman hardware in “direct mode” and want local network access without RS485 wiring.

> **Recommendation for this project:** prefer **A (solis_modbus)** or **B (core modbus)** because your control requirement includes **writing settings** (charge/discharge, TOU windows, mode flags). Projects and discussions around Solis Modbus + writing registers are common in the HA community.  [oai_citation:4‡Home Assistant Community](https://community.home-assistant.io/t/solis-inverter-modbus-integration/292553?utm_source=chatgpt.com)

---

## 2) What inverter controls exist (conceptual “knobs”)

Solis hybrids typically expose controls via Modbus / settings that map to:
- **Storage / work mode** (e.g. Self-Use vs Feed-in priority / time-of-use variants)
- **Allow grid charge** enable/disable
- **Time-of-use (TOU) charge/discharge windows**
- **Charge/discharge current/power limits**
- **Battery reserve / minimum SoC**
- **Export limitation / zero export / export cap**
- **“Update/apply” operations** (some configs require writing a group and then an “apply/update” flag)

Important: exact register addresses vary by model/firmware; implementers typically bind these to HA entities (selects/numbers/switches) via solis_modbus or core modbus.

Solis support + HA community docs highlight:
- storage mode combinations including self-use / feed-in priority / TOU / allow grid charge concepts 
- “Zero export” / export limiting function affects whether export (including battery export) is allowed  [oai_citation:5‡solis-modbus.readthedocs.io](https://solis-modbus.readthedocs.io/?utm_source=chatgpt.com)
- TOU control concepts are commonly used to define charge/discharge periods  [oai_citation:6‡GitHub](https://github.com/Moondevil-ha/solis-modbus-smart-charging?utm_source=chatgpt.com)

> **Practical interpretation used in this plan**
- **“Discharge to grid”** is achieved by enabling a mode that permits export from battery (e.g. feed-in priority and/or TOU discharge windows) and ensuring **zero-export is not enabled**.
- **“Discharge to home only / discharge at all”** is achieved by switching to self-use and/or setting discharge limits to 0 and/or removing discharge windows.

---

## 3) Data model: merged rate sensors (import + export)

Create **one merged rate sensor per direction** with a continuous sorted list of half-hour slots covering now → +36h (or +48h).

### `sensor.rates_import_merged`
- attribute `rates`: list of `{start, end, value}`

### `sensor.rates_export_merged`
- attribute `rates`: list of `{start, end, value}`

Also provide convenience sensors:
- `sensor.import_price_now`
- `sensor.export_price_now`
- `sensor.rate_slot_index_now` (index into merged rates)

Implementation detail is up to you (template sensor concatenating today+tomorrow then sorting).

---

## 4) Entities required (minimal + practical)

### Telemetry (inputs)
- `sensor.battery_soc` (%)
- `sensor.battery_power` (W)
- `sensor.grid_power` OR split import/export
- `sensor.pv_power` (optional for visibility; not used for planning)
- `sensor.house_load_power` (optional; not used for planning)

### Derived sensors (templates)
- `sensor.grid_import_power = max(grid_power, 0)` (adjust sign convention)
- `sensor.grid_export_power = max(-grid_power, 0)` (adjust sign convention)
- `sensor.export_headroom_w = max(export_cap_w - grid_export_power - export_buffer_w, 0)`

### Helpers (inputs)
- `input_boolean.arb_enable`
- `input_select.arb_mode` = `auto / hold / charge / discharge`

Battery constraints:
- `input_number.soc_floor_base_pct` (e.g. 20)
- `input_number.soc_floor_extra_max_pct` (e.g. 15)
- `input_number.soc_min_hard_pct` (e.g. 15)
- `input_number.soc_max_pct` (e.g. 92)
- `input_number.capacity_kwh_total` (16.1 * units * usable_fraction)

Power constraints:
- `input_number.max_charge_w`
- `input_number.max_discharge_w`
- `input_number.export_cap_w` (= 6000)
- `input_number.export_buffer_w` (e.g. 300)

Economics:
- `input_number.wear_cost_p_per_kwh` (degradation proxy)
- `input_number.min_margin_p_per_kwh` (deadband)
- Optional: `input_number.min_import_while_exporting_w` (e.g. 200W)

Schedule storage / debug:
- `input_text.arb_schedule_json`
- `input_text.arb_last_reason`

EV guard:
- `binary_sensor.ev_charging_now` (true if EVSE power > threshold or charger reports charging)

---

## 5) Control strategy (rule-based, no PV/load prediction)

### Goals
- Discharge (sell) energy **only** in the best export-price slots.
- Charge from grid **only** in off-peak/cheap slots.
- Never breach export cap.
- Avoid silly behaviour: importing while exporting due to forced discharge.
- Preserve a dynamic reserve floor for “house usage / uncertainty”.

### 5.1 Dynamic reserve floor (time-to-next-off-peak)
Without consumption prediction, reserve can still adapt by time-to-next-cheap-charge:

Inputs:
- `soc_floor_base_pct`
- `soc_floor_extra_max_pct`
- `T_scale_hours` (e.g. 12h)
- `T_to_next_offpeak_hours` (computed from merged import rates and chosen off-peak definition)

Function:
- `soc_floor_dynamic = soc_floor_base + soc_floor_extra_max * clamp(T_to_next_offpeak / T_scale, 0, 1)`

Hard limits:
- `soc_floor_dynamic >= soc_min_hard_pct`
- `soc_floor_dynamic <= soc_max_pct`

### 5.2 Off-peak window definition (charging)
Simple options:
- cheapest `M` import slots in next 24h (e.g. 6–10 slots)
- OR import price <= threshold (e.g. <= X p/kWh)
- OR a known IOG cheap schedule feed (if you expose it)

### 5.3 “Sell plan” = optimal discharge of currently available energy
Compute available energy:
- `E_available_kWh = max(0, (soc_now - soc_floor_dynamic) / 100 * capacity_kWh_total)`

Sell slot capacity at full planned discharge:
- `P_sell_kW = min( export_cap_w, max_discharge_w ) / 1000`
- `E_slot_kWh = P_sell_kW * 0.5`

Pick discharge slots:
1. Take export slots in the horizon (e.g. next 24h), sort by export price descending.
2. Allocate `E_available_kWh` into those slots in order:
   - mark slot as `DISCHARGE` if allocation > 0
3. Stop when allocation exhausted.

This yields “optimal times to discharge remaining capacity” with no PV/load assumptions.

### 5.4 “Charge plan” sized to profitable upcoming discharge
Only charge in off-peak slots. Target SoC is computed from profitable selling opportunities:

1. Identify discharge candidate slots (e.g. top K export slots) in the next “peak window(s)”.
2. Profitability gate vs off-peak:
   - `spread = export_price_peak - import_price_offpeak`
   - require `spread >= wear_cost + min_margin`
3. Total profitable sell energy capacity:
   - `E_sell_cap_kWh = (# profitable discharge slots) * E_slot_kWh`
4. Target energy above reserve:
   - `E_target_above_reserve = min(E_sell_cap_kWh, (soc_max - soc_floor_dynamic)/100 * capacity_kWh_total)`
5. Convert to SoC target:
   - `soc_target = soc_floor_dynamic + 100 * E_target_above_reserve / capacity_kWh_total`
   - clamp to `[soc_floor_dynamic, soc_max]`

---

## 6) Actuation rules (real-time, event-driven)

### Important: EV charging override
If `binary_sensor.ev_charging_now == true`:
- **Disable discharge** (strong guard against importing while exporting)
  - set discharge limit = 0 OR remove discharge windows OR switch to self-use mode that prevents export-priority
- Only allow grid charging if currently in off-peak slots and SoC < soc_target.
- Rationale: EV load dominates; you don’t want the battery to fight it (even if off-peak).

### DISCHARGE slot behaviour
Condition to discharge:
- current half-hour slot is marked `DISCHARGE`
- `soc_now > soc_floor_dynamic + hysteresis` (e.g. +1%)
- `NOT ev_charging_now`

Set discharge power target:
- start with `P_target_w = min(max_discharge_w, export_headroom_w)`
- “don’t import while exporting” guard:
  - if `grid_import_power > min_import_while_exporting_w` then reduce or stop discharge
- apply ramp-rate/hysteresis to avoid oscillation.

### CHARGE slot behaviour
Condition to charge:
- current slot is off-peak
- `soc_now < soc_target`
- (optionally) `arb_enable == true` and mode auto/charge

Set charge power target:
- `P_charge_w = min(max_charge_w, site_import_cap_if_any)`
- stop charging at `soc_target` (or soc_max).

### IDLE / self-consumption behaviour
- Otherwise set to self-use / no forced discharge.
- Keep export cap always enforced.

---

## 7) Automations / scripts (conceptual HA structure)

### 7.1 Planner script: `script.arb_recalculate`
Triggers:
- merged import/export rates update
- every 30 minutes (slot boundary)
- manual helper changes (soc floor, wear cost, etc.)
- SoC changes by > 2% (optional)

Actions:
1. Compute `soc_floor_dynamic` (time-to-next-offpeak).
2. Compute discharge slot list by allocating `E_available_kWh` into best export slots.
3. Compute `soc_target` for charging (based on profitable peak slots).
4. Store outputs into `input_text.arb_schedule_json` plus a few derived debug states.

### 7.2 Control loop: `automation.arb_actuate`
Triggers:
- every 30–60 seconds (backstop)
- SoC change
- grid power / export headroom changes
- EV charging state changes

Actions:
- If `arb_enable` false -> set safe mode (no forced discharge)
- If `arb_mode == hold` -> no forced charge/discharge
- Else:
  - apply EV override logic
  - else apply DISCHARGE/CHARGE/IDLE actuation per schedule + guards
- Write Modbus controls (mode + discharge/charge limits + TOU enable/apply if used)

### 7.3 Safety automation: `automation.arb_safety`
Triggers:
- export exceeds cap for > N seconds
- SoC below hard minimum
- inverter fault / overtemp
- Modbus comms unavailable

Actions:
- immediately disable discharge, revert to self-use, log + notify

---

## 8) Test plan (short)

### Phase 1 — Observability / correctness
1. Validate merged rates lists: sorted, continuous across midnight, no dupes.
2. Validate sign conventions for grid import/export power and headroom.

### Phase 2 — Dry run
3. Run planner only; inspect schedule JSON:
   - discharge slots are the highest export price intervals
   - soc_target increases when peaks are profitable vs off-peak
4. Enable actuator in “no-write” mode (log only) for a day.

### Phase 3 — Live actuation with clamps
5. Enable writes with very low max charge/discharge limits (e.g. 500W).
6. Confirm:
   - export cap never breached
   - during EV charging, discharge stays off
   - discharge only happens in chosen slots and respects reserve

### Phase 4 — Edge cases
7. Start near reserve: verify discharge never drops below floor (hysteresis works).
8. Rate changes: verify planner updates schedule without rapid flip-flopping.
9. High PV export during discharge slot: verify headroom clamp prevents exceeding 6kW export.

---

## Notes / implementation choices

- Prefer implementing controls via **solis_modbus HACS** (entity-based) or **core modbus YAML** with known register maps.  [oai_citation:7‡GitHub](https://github.com/Pho3niX90/solis_modbus?utm_source=chatgpt.com)
- Some Solis configurations require “write a group, then apply/update” semantics; plan for that in your actuator. 
- Avoid cloud-only SolisCloud control if you want true local autonomy.