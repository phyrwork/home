# House battery control: native TOU schedules

This is the current runtime contract, superseding historical mode switching and
peak-shaving handovers described in the commissioning log and task cards.
The [11 September load-following test](solis-load-following-investigation-2026-09-11.md)
proved that this inverter supplies changing house demand in Feed-In Priority
with actual Grid Peak Shaving off and no active TOU slots. A short
[25 September calibration test](house-battery-commissioning-log.md#25-september-2026-export-calibration--30-w-to-0-w)
reduced mean settled utility import from about 53 W to 31 W after Battery Saving
had been disabled; later readings reached 12–18 W import. Longer observation
remains pending; the earlier export transient was not re-tested.

## Fixed commissioning and ownership

The S5-EH1P6K-L is commissioned in Feed-In Priority, with Allow Grid Charging on,
Battery Reserve enabled at 10%, EMS disabled, and actual Grid Peak Shaving off.
As of 25 September, Battery Saving is disabled (user reported) and Export
Calibration is 0 W (HA confirmed with an observed physical import reduction).
These are manual settings: the controller does not check or enforce them, and
authoritative device readback and longer validation remain pending.
Runtime writes are restricted to TOU time/current/target/enable entities in the
legacy `solis_cloud_control` integration. The controller checks its readable
commissioning prerequisites before new operations; it does not repair them.

The legacy peak-shaving entity represents storage-mode register 43110 bit 11.
The working Grid Peak Shaving control on this inverter is 43483 bit 7 (CID
9023). Verify the latter independently during commissioning. It has no runtime
entity mapping in this controller.

The controller has exclusive schedule ownership: it observes all twelve
charge/discharge directions, stops conflicting active directions, and uses the
configured allocations for its three purposes. Do not run another schedule
writer concurrently. `solis_tou` is not installed or deployed.

## Economic plan

- The 10% global reserve is fixed. Forecast house requirements and the 2 kWh
  configured margin are additional reserve protected from **forced export**.
- The forecast horizon is the next standard cheap refill, not a speculative
  bonus dispatch. Forecast PV offsets concurrent load but is not banked as
  future stored energy. Dynamic discharge targets round upward to common
  supported slot increments, with a 2% SOC stopping margin. The same margin prevents rearming export
  at the stopping boundary; the native slot target itself is unchanged.
- Cheap import opportunities charge toward 100%. Prices and profitability are
  separate from household charging permission. All charge/recharge intervals
  must fit within guaranteed 23:30–05:30 Europe/London off-peak or the currently
  qualified half-hour. A bonus period qualifies when smart-control state and EV
  power above 50 W overlap with fresh evidence from that same period. Power uses
  HA `last_reported` (maximum age two minutes); smart state uses the successful
  Octopus dispatch retrieval timestamp (maximum age five minutes). Both must be
  observed after controller startup. Charge Cap is assumed enabled.
- Qualification survives the EV stopping, Boost starting or a subsequent source
  failure until the half-hour ends. The next period requires new evidence.
  Source changes and minute/boundary wakeups reevaluate the guard; no historical
  latch is restored. Ordinary leases retain their shorter 15-minute/component
  limits. Cycle recharge is gated independently of discharge. The controller
  stops unauthorized native charge slots even when planning/telemetry is
  unavailable and rechecks permission before enabling prepared schedules.
  See [billing rules and evidence](intelligent-octopus-battery-charge-guard.md).
- Action priority is eligible authorized charging, then export of surplus above
  the control reserve, then idle. A cheap period never blocks surplus export.
  Permission changes can preempt ordinary export or charging; direction changes
  still require a confirmed stop. Full-SOC cycling remains enabled in cheap
  periods, using the existing duration helper and adjacent discharge/recharge
  schedule. Recharge is armed before discharge; the inverter clock performs
  their handover. Midnight splitting retains the commissioned 23:59 boundary
  and its explicit one-minute gap.
- `IDLE` means no forced slot is needed; the inverter follows house demand.
  There is no `RESERVE_FOLLOW` action or mode-changing fallback.

## Reconciliation and failures

One serialized worker compares the desired schedule with HA observations.
It prepares disabled slots, enables recharge before discharge, and leaves
unchanged settings alone. Disabled slots are not reset just for housekeeping.
Starts retry at 0/15/60 seconds for an unchanged generation, then at one-minute
intervals while still authorized. Stops take precedence, retry with capped
backoff, and never select a different operating mode.

Communication health is separate from economic intent. Last good telemetry
and capabilities can support tariff/forecast planning through read gaps.
Cached observations are never sent to the writer. Missing controls prevent
new slot preparation or confirmation. Fresh telemetry uses the existing
30-minute maximum device-sample age and one-minute future-skew tolerance;
a successful HA lookup does not renew the device timestamp.

Without fresh SOC, no new discharge is authorized. A confirmed cycle pair can
finish on the native clock, retaining its armed recharge; further discharge
legs wait for fresh SOC. Known expiry and withdrawn tariff authority still
require cleanup. Fresh SOC below the safety floor stops forced discharge;
cheap charging is allowed to recover a low battery.

A successful service plus matching HA state is HA-level confirmation, not
independent physical readback. Following a timeout/error, the adapter records
the post-failure state and blocks dependent changes until a later report.
It requests a coalesced entity refresh, at most once per minute when necessary.
Optimistic state emitted by the failing service cannot itself clear this block.

Diagnostics retain their entity IDs and last known values. Attributes expose
`telemetry_timestamp`, `telemetry_age_seconds`, `telemetry_stale`, and
`schedule_confirmed_at`; the heartbeat also exposes `last_controls_read_at`.
Action describes the desired schedule, while health/error/pending-operation
attributes describe whether application is complete. No `fail_safe` mode exists.

## Startup, shutdown, and deployment

Startup reconciles observed slots against the current plan before enabling
new ones. Shutdown has a 30-second total budget, including the running worker
and slot cleanup. It reports incomplete cleanup and does not change mode.
The existing heartbeat sentinel issues one HA persistent notification identity
for a stale heartbeat and dismisses it on recovery. Its ten-minute startup
grace, three-minute stale threshold and one-minute checks are retained; it
never writes inverter settings.

Native TOU times repeat daily. An end time is not permanent expiry: an outage
can leave a slot armed to recur tomorrow. Pending stops and shutdown failure
must remain visible, with reconciliation on recovery. There is no claim of
physical stop confirmation when the API cannot provide one.

For rollout, keep the controller/watchdog disabled until legacy entities and
commissioned settings are checked. Install the component and YAML together,
check HA configuration, and enable the include only when ready. Run the full
Ansible deployment after targeted iteration. Validate actual charging/export
and a full cycle when SOC and the tariff window permit. For rollback, disable
the controller and perform bounded slot cleanup without changing mode or peak.

## Preserved experiment

All tracked and untracked experimental Solis TOU work was saved in Git stash
`15c0b2f0ebb94436f72a661664a48410b4803b54`, named
`solis-tou-experiment-before-controller-simplification-20260911`.
It includes the complete component, deployment changes and investigation files.
Do not pop it into the simplified branch; use a separate branch when revisiting
it. Only the reviewed legacy telemetry 10-to-45-second timeout deployment patch
and the load-following evidence were carried into this implementation.
