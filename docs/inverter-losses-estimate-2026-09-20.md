# Garage inverter losses estimate

Entity: `sensor.garage_inverter_losses_estimated`, displayed as **Garage inverter
losses (estimated)**. Energy uses the display-only label **Garage Inverter**;
the sensor entity ID and friendly name are unchanged. Its cumulative kWh state is derived from the Solis lifetime
DC battery charge and discharge energy counters:

`charged_kWh * (1 / 0.95 - 1) + discharged_kWh * (1 - 0.95)`.

The template documents the 95% assumed conversion efficiency in each direction,
matching the battery controller's present assumptions. This is an estimate, not a
measured loss or a manufacturer battery-path efficiency specification. Internal
cell losses are excluded because HA's household energy balance already subtracts
DC charging and adds DC discharging. There is no separate fixed standby allowance.
See [equipment research](equipment/conversion-efficiency-audit-2026-09-20.md).

The source counters report whole kWh, so this cumulative sensor advances in steps.
Unknown source values make it unavailable instead of resetting it to zero.
Changing efficiency constants requires rebuilding the historical statistics to
avoid a fictitious consumption increment or reset.

`scratch/backfill_inverter_losses.py` combines matching source timestamps in both
hourly and five-minute recorder statistics. Historical states use source states;
historical sums use source sums to preserve recorder's reset accounting. It
requires identical timestamp coverage, validates monotonic sums, takes a complete
SQLite backup before writing, and checks database integrity afterward. Run only
with HA Core stopped; restart Core even if the script fails. It refuses to reuse
an existing backup directory. Raw state-event history is not reconstructed.

For 13–19 September, source charging was 222 kWh and discharging was 211 kWh.
Estimated inverter losses are 22.234211 kWh, or 3.176316 kWh/day. Against the prior
13.066 kWh/day untracked average, this leaves approximately 9.89 kWh/day unexplained.
Adding this consumer reallocates untracked usage; it does not change total home
consumption or import/export costs.

Deployment and backfill completed successfully. Backfilled 715 hourly records from
21 August 2026 18:00 UTC and 3,019 five-minute records from 10 September 03:15 UTC.
Backup: `/config/inverter-losses-backfill-20260920/recorder-before.sqlite`; result
report: `applied.json` in the same directory. The new consumer is selected in
Energy preferences. Full Ansible deployment and SQLite integrity validation passed.
