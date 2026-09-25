# Battery avoided-import audit — 25 September 2026

Read-only investigation of Home Assistant recorder history. All times below are
Europe/London (BST). Today's coverage is midnight–11:40. Earlier screening covers
the sensor's creation on 20 September at 23:16 through midnight on 25 September.
No configuration, counters, or historical statistics were changed.

## Finding

The 11:09–11:19 rise is strongly supported as a stale-battery-telemetry artifact,
not evidence of a new household appliance consuming 4 kW. Its recorded value is
**0.64209 kWh / £0.194319**, or **35.2%** of today's £0.551980 increase.
That is the value of the suspect interval, not a proven exact correction amount.
Incomplete appliance coverage and the missing battery samples prevent an exact
reconstruction of the true power throughout that interval.

The counter read £4.994659 at inspection, up from £4.442679 at midnight. The
corresponding energy counter rose from 17.119398 to 19.286784 kWh.

## Calculation and failure mechanism

The deployed-repository definition uses:

```
AC discharge = max(-DC battery power, 0) * 0.95
avoided import = AC discharge - min(AC discharge, max(-grid demand, 0))
cost rate = avoided import / 1000 * current import tariff
```

Both cumulative sensors use left integration with a one-minute maximum
subinterval. Their templates check that inputs are numeric, but do not check
sample freshness or align battery and grid measurement times. A numeric battery
reading can therefore continue to accumulate value after the physical battery
changes operation. A more frequent integral update does not refresh its source.

Sources: `deployment/files/templates/house_battery_accounting_power.yaml`,
`deployment/files/templates/power_cost_rates.yaml`, and the
`house_battery_total_avoided_import_{energy,cost}.yaml` integration sensors.

## Today's 11:00 event

| BST | Recorded evidence |
|---|---|
| 11:01:50 | Battery updates to **−4,225 W**; device measurement timestamp is **10:59:31**. SOC is 23%. |
| 11:08:40 | Grid still reports **6,168 W export**. |
| 11:09:40 | Export falls to **2,040 W**. Unchanged battery reading creates **1,973.75 W avoided import**. |
| 11:10:40 | Export falls to **17 W**. Unchanged battery reading creates **3,996.75 W avoided import**. |
| 11:10–11:19 | Solar stays around **1.98 kW**; export stays around **14–32 W**; calculated avoided import stays around **3.99 kW**. |
| 11:19:50 | Battery finally updates to **+1,590 W charging**, measured at **11:19:30**. SOC is 21%. Avoided import immediately becomes zero. The same Solis update reports **42 W export**, agreeing with near-zero grid flow. |
| 11:19:56 | Controller transitions from `RESERVE_DISCHARGE` to `IDLE`. Its attributes confirm SOC 21% and battery charging at 1.59 kW. |
| 11:20:45 | Grid export returns to **1,666 W**. |
| 11:29:42 | Battery telemetry catches up to **0 W**. |

The HA battery value was held for 18 minutes, spanning almost 20 minutes between
device measurement timestamps. The second delay, after 11:20, shows that charging
telemetry can also remain held after physical grid flow has changed.

During the main spike the kettle was effectively off, dishwasher power was zero,
washing-machine power was zero and its energy unchanged, EV charger power was
about 2.4 W, fridge about 3 W, desk about 10 W, and TV about 1 W. Coffee and
microwave power entities are unavailable, so these are not exhaustive submeters.

The combined evidence fits the battery reaching its discharge target and
temporarily absorbing solar, followed by the controller clearing forced slots.
The precise physical transition time is inferred from grid history, not captured
by a battery sample. At the fresh charging sample, approximately 1.97 kW solar
minus 1.59/0.95 kW battery charging minus grid export leaves roughly **250 W**
for household load under the configured efficiency model. This is a consistency
check, not an independent calibrated household meter.

## Other increases today

| Period BST | Recorded avoided-import value | Attribution |
|---|---:|---|
| 00:00–00:03:44 | ~0.15p | Small carry-in interval; not a significant isolated step. |
| 04:34–04:35 and 05:04–05:05 | <0.04p combined | Brief controller/telemetry transitions; too small to drive the concern. |
| 04:38:58–04:48:59 | 0.65p | Mostly steady ~500 W residual while cycling/exporting; a short tail coincides with discharge changing to recharge. No specific appliance proves the residual. |
| 05:08:59–05:31:20 | 2.26p | Mainly a stop-transition artifact: controller requests stopping discharge at 05:08:59, but −4,949 W remains until 05:14:01. From 05:10:06 it falsely implies ~4.70 kW avoided import despite grid importing ~268 W and no matching appliance load. |
| 05:34:04–07:50:47 | 29.13p | Mostly steady ~500 W, later falling as solar rises. Includes a suspicious 06:24:52–06:29:05 bump to ~992 W: export falls ~500 W before battery discharge updates from 4,945 to 4,355 W. Extra contribution over the prior baseline is roughly 1p. Household baseline and conversion-model error are not separately measured. |
| 08:14:04–08:17:07 | 3.54p | **Real kettle event:** its plug reports ~2.8 kW from 08:13:25–08:15:40. Direct integration of plug power is ~0.103 kWh; avoided-import accounting records ~0.117 kWh. A real load is established, but timing/model error remains in the attributed amount. |
| 11:09:40–11:19:50 | 19.43p | Strong stale-telemetry evidence described above. |

The cumulative counter's first increase for an interval can occur after power
first becomes nonzero, because its integration update accrues the preceding
interval. Event times above use power history, not just the first cost update.

## Earlier significant rises

For reproducible screening, selected every local 15-minute bin with at least
**5p** increase; merged adjacent selected bins and inspected ten minutes either
side. The amounts below are the increases in those selected bins, not corrected
appliance costs or complete event integrals. Smaller historical events were not
exhaustively attributed.

| Date / selected bins BST | Increase | Evidence and attribution |
|---|---:|---|
| Sep 21, 12:00–12:15 | 9.34p | **Stop/polling artifact:** controller IDLE 11:58:56; battery remains −4,234 W until 12:08:27, then −36 W. About 2.25 kW false avoided import meanwhile; no tracked high-power appliance. |
| Sep 21, 20:15–21:00 | 32.39p | **Kettle and dishwasher:** kettle ~2.8–2.9 kW at 20:23–20:27, dishwasher ~2.1 kW heating episodes from 20:24 and 20:45. Real loads confirmed; battery five-minute sampling distorts the exact allocation. |
| Sep 22, 07:45–08:00 | 5.65p | **Kettle:** ~2.8 kW from 07:47:38, concurrent with ~3.22 kW avoided-import peak. |
| Sep 22, 11:15–11:30 | 7.83p | **Same midday artifact:** battery −4,239 W held from 11:06:24 until 11:18:22, then +1,532 W charging. ~4 kW avoided-import pulse starts 11:14:30; controller IDLE 11:18:23. |
| Sep 22, 14:15–14:30 | 6.27p | **Unattributed:** battery samples support ~2.1–2.4 kW discharge at 14:11/14:16, dropping at 14:19; no matching tracked appliance. Could be unmetered load, with unresolved sample-duration error. |
| Sep 22, 17:30–17:45 | 7.61p | **Kettle, with substantial timing distortion:** plug high from 17:28:49 through at least 17:32:07; battery's ~3.17 kW sample arrives 17:32:33 and is held until 17:37:16. |
| Sep 22, 18:15–18:45 | 33.17p | **Mixed/unresolved:** sustained battery discharge ~2.2–3.0 kW. EV ~7.45 kW from 18:27 explains part only; earlier load has no tracked appliance match. |
| Sep 22, 22:15–23:00 | 30.45p | **Dishwasher heating:** ~2.1 kW episodes beginning 22:16 and 22:39, corresponding to ~2.65–2.70 kW avoided import including baseline. |
| Sep 22, 23:15–23:30 | 5.79p | **Dishwasher:** heating starts 23:26. Just after this window a charging transition also holds stale discharge until 23:32:17. |
| Sep 23, 07:15–07:30 | 6.12p | **Kettle:** ~2.8 kW at 07:27–07:29 with ~3.2 kW avoided import. |
| Sep 23, 11:00–11:15 | 7.75p | **Stop/polling artifact:** controller IDLE 11:07:01; battery −4,231 W persists until 11:12:02, then −36 W. ~3.9 kW avoided-import pulse starts 11:08:11. |
| Sep 23, 22:30–22:45 | 5.60p | **Unattributed:** brief ~4.08 kW avoided-import reading at 22:36:35 with battery held near −4.3 kW; no matched tracked appliance. Cannot prove the implied load. |
| Sep 24, 19:30–19:45 | 8.33p | **Partly EV / earlier load unresolved:** battery ~2.4 kW discharge at 19:38/19:43, EV ~7.46 kW starts 19:43:10. EV does not explain the earlier load. Charging starts later, with battery update +5.019 kW at 19:48:44. |

## Using native cumulative energy instead

There is a real problem to fix. Native accumulated measurements are preferable
to re-integrating sparse cloud power samples when their precision and timestamps
fit the accounting task. But they are not an automatic substitute for this
particular avoided-import calculation:

- **Grid net cost:** tariff-aligned differences in native import and export
  energy counters can give a robust cost basis. Check each counter's provenance:
  the selected `current_accumulative_consumption_export_electricity_...` helper
  here is itself an integral of grid power, not an independent hardware counter.
- **Battery totals:** today's native lifetime discharge counter advances only
  in **1 kWh increments**; charge increments were 1 or 2 kWh between recorded
  updates. These avoid assuming that an old instantaneous power persists, but
  quantization is coarser than today's entire suspicious **0.642 kWh** event.
  From 11:01:50 to 11:19:50 discharge changed 990→991 kWh. That can include
  discharge before the transition and does not validate discharge throughout the
  missing interval. Whole-kWh readings also cannot precisely allocate energy
  across tariff boundaries.
- **Avoided import is an allocation:** it needs battery discharge and grid export
  aligned over sufficiently short intervals. Clipping a coarse-period or lifetime
  subtraction is not equivalent to summing clipped instantaneous differences.
  Example: one interval has 1 kWh battery discharge serving the house and no
  export; a later interval has 1 kWh solar export and no battery discharge.
  Subtracting the two periods' total export from total discharge gives zero,
  despite 1 kWh of avoided import in the first interval.

Recommended design direction: use native grid counters for grid-cost accounting
where available, and obtain frequent, timestamped battery/AC-flow measurements
or finer-resolution native energy counters for battery attribution. Reconcile
longer-term totals against the existing native battery counters. Until then,
make the estimate's stale-input periods explicit rather than silently asserting
that the last battery power continues. A freshness cutoff alone cannot reconstruct
missing energy, and switching to trapezoidal integration does not reveal the
physical transition time.

Historical cost corrections need their own explicit, evidence-based method;
the suspect intervals identified here should not simply be deleted wholesale.

## Decision after discussion

Document the limitation and retain the current sensors and history for now.
Delayed accounting is acceptable, but preserving household-use attribution needs
finer, time-aligned measurements. Delayed delivery can work if the original
timestamps and interval detail survive; coarse cumulative totals alone cannot
provide eventual correctness for this split. Daily netting was not adopted.
The [accounting guide](power-dashboard-cost-accounting.md#known-limitation-battery-measurement-timing)
records this as a known limitation, not a completed accounting fix.
