# Power dashboard cost accounting

Added 20 September 2026. These totals start when deployed, without backfill.
All six new cumulative sensors use HA's Integral platform, left integration and a
one-minute maximum subinterval. GBP/h integrates to GBP; W integrates to kWh.
Totals survive restarts. Signed balances use `state_class: total`, not
`total_increasing`, so decreases are not mistaken for meter resets.

## EV and feed

- EV charging cost: charger power × current import price. This deliberately values
  all EV electricity at import price, including any battery/solar contribution.
- Feed net cost: grid imports × import price − grid exports × export price.
  This excludes standing charges and is a live estimate, not a supplier bill.
- Net cost excluding EV: the cumulative feed net cost minus cumulative EV cost.
  This template derives from the two existing integrals so the totals reconcile
  exactly, without an independently integrated difference or historical backfill.
  Negative means export income exceeds the estimated non-EV energy expenditure.
- The EV section is named **EV Battery** and shows the petrol comparison basis.
  At deployment Tesco Bar Hill was £1.569/L, giving £0.5946/kWh with multiplier
  0.378947. Tesco's own feed timestamp was **29 April 2026 11:18:22**, and HA's
  retained history showed no price changes. Exposing the feed timestamp makes the
  stale upstream price visible; this change does not replace the data supplier.

## Battery: separate energy and monetary ledgers

Use Solis raw DC battery power (positive charging, negative discharging) and signed
grid demand (positive import). Assume 95% inverter efficiency in each direction:

`AC charge = max(battery_DC, 0) / 0.95`

`AC discharge = max(-battery_DC, 0) * 0.95`

`battery export = min(AC discharge, max(-grid_demand, 0))`

| Ledger | Power integrated for energy | Monetary rate integrated for cost |
| --- | --- | --- |
| Avoided household import | AC discharge − battery export | avoided-import power × import price |
| Arbitrage | battery export only | battery export × export price − AC charge × import price |

Powers are in W, so monetary rates divide by 1,000 to obtain GBP/h. Price changes
update cost-rate templates independently of power changes. Negative tariff prices
remain signed. Invalid source readings make rate sensors unavailable rather than
silently substituting a zero price.

All charging expenditure is assigned to arbitrage; avoided-import value is gross.
Adding both cost totals gives the combined estimated benefit. Arbitrage energy counts exported battery energy only and never subtracts charging. There
is no FIFO/matched-cycle cost allocation or valuation of initial/stored inventory.
Charging is valued at import price even when solar supplies it. Battery export
attribution is marginal alongside solar, rather than measured energy provenance.
Internal battery losses are already reflected in the measured DC flows.

## Entities

- `sensor.ev_charging_total_cost`
- `sensor.electricity_total_net_cost`
- `sensor.electricity_total_net_cost_excluding_ev` (derived cumulative balance)
- `sensor.house_battery_total_avoided_import_energy`
- `sensor.house_battery_total_avoided_import_cost`
- `sensor.house_battery_total_arbitrage_export_energy`
- `sensor.house_battery_total_arbitrage_cost`

The four battery totals share power templates, keeping energy attribution
separate from tariff valuation. The Power dashboard is HA storage-managed; its
cards are updated through the Lovelace API rather than deployed YAML.

Validation: `scratch/test_power_cost_rates.py` checks import/export tariff signs,
charging expenditure, household/export discharge splitting, solar export limits,
energy conservation and unavailable inputs. Integration behavior follows the
[HA Integral documentation](https://www.home-assistant.io/integrations/integration/).
The petrol source is [Tesco's published feed](https://www.tesco.com/fuel_prices/fuel_prices_data.json).

Deployment verification: full Ansible run completed with no failed tasks; all eight
calculation tests passed. The live dashboard shows all seven totals. During the
initial charging interval, the original net arbitrage energy and cost decreased
with charging. The energy definition was subsequently corrected to export-only;
charging now changes arbitrage cost but adds no arbitrage energy. The non-EV balance reconciled
with the cumulative feed and EV costs. No recorder backfill was performed.

Final display labels (sensor names and entity IDs unchanged):

- Feed: **Net Cost**, **Net Cost (no EV)**.
- House Battery: **Avoided Import**, **Import Saving**, **Arbitrage Energy**,
  **Arbitrage Saving**.
- EV Battery: **Total Cost**, plus **Fuel Basis** with **Equivalent Rate**,
  **Pump Price**, and **Updated**.

Export-only correction: the Arbitrage Energy card uses a fresh integral identity
so it starts at zero without inheriting the retired net-energy counter
`sensor.house_battery_total_arbitrage_energy`. That old history is not rewritten.
Avoided import and signed arbitrage cost remain unchanged. Tests cover charge-only,
household-only discharge, mixed household/export discharge and PV-only export.

Live export-only verification passed: during charging, Arbitrage Energy stayed at
0 kWh and Arbitrage Saving decreased, with both avoided-import totals at zero.
All nine calculation tests passed. Activation used a one-time Core restart; no
new deployment restart rule was added. The retired net-energy entity is disabled.
