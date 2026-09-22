# Intelligent Octopus Go: household battery charging guard

Recorded 2026-09-22. The agreed guard is implemented in controller version
0.2.2; deployment verification is recorded in the commissioning log. Historical
investigation stages below describe the evidence available at each stage.
Settled billing records have not been audited.

The subsequent live input inspection is recorded below. The concrete
[implementation and verification plan is T0051](tasks/T0051-iog-settlement-charge-guard.md).

## Billing requirement

For this home's Intelligent Octopus Go (IOG) 4-rate tariff:

- Household electricity is unconditionally off-peak from **23:30 inclusive to
  05:30 exclusive, Europe/London local time**. No EV charge is required.
- Outside that window, a schedule alone is insufficient. Actual
  Octopus-controlled qualifying smart charging must occur in the relevant
  30-minute settlement period to establish the household discount.
- Once qualified, the entire half-hour is cheap, even if the EV stops early.
  Qualification does not carry into the next half-hour.
- Manual/Boost charging must not establish bonus household eligibility.
- Octopus's published explanation also limits bonus household pricing to
  charging within the EV's six-hour cheap allowance, measured midday to midday.
  Actual charging alone therefore does not establish eligibility either.

Source: Octopus Energy, [Smart charging and Charge Cap explained](https://octopus.energy/blog/intelligent-octopus-go-smarter-charging-for-a-greener-grid/),
published 7 May 2026, checked 22 September 2026. Its four rules and half-hour
billing/unplugging examples support these requirements. Prices should come from
the account tariff, not a hard-coded advertised rate.

Example supplied for this investigation:

| Time | EV activity under an 18:00–19:00 schedule | Household billing |
| --- | --- | --- |
| 18:00–18:12 | Qualifying smart charge | 18:00–18:30 qualifies |
| 18:12–18:30 | Full, no charging | Same qualified half-hour remains cheap |
| 18:30–19:00 | Full, no charging | Normal household rate |

The configured home battery capacity is 32.1536 kWh. Exposure is material:
illustratively, 5 kW for 90 minutes imports 7.5 kWh; extra cost is that energy
multiplied by the difference between peak and off-peak rates.

## Existing control path and gap

1. `deployment/templates/templates/octopus_fused_day_rates.yaml.j2` fuses
   Octopus rate events. An `is_intelligent_adjusted` interval at the event minimum
   becomes `BONUS_CHEAP`; an unadjusted minimum with two or three distinct prices
   becomes `STANDARD_CHEAP`. These labels do not themselves prove billing
   eligibility. The four billing categories should not be confused with four
   distinct numerical prices in this rate feed.
2. `deployment/files/house_battery_control.yaml` connects that fused import
   sensor to the Python controller. It configures no EV charging evidence input.
3. `deployment/files/custom_components/house_battery_control/planner.py`
   validates rate provenance and profitability. `_dispatch_source` reads source
   identity and retrieval time, not proof of an actual charge. `build_plan`
   explicitly allows adjusted rates to authorize charging after dispatch stops.
4. Ordinary bonus charging has a 15-minute lease, bounded by its rate component.
   `controller.py` reconciles native Solis schedules and handles lease cleanup
   and subsequent plans. A lease limits duration; it does not establish that
   the relevant settlement period qualified. Subsequent leases can rely on the
   same unproven rate information.
5. Full-SOC cycling uses paired discharge/recharge schedules in `_select` and
   `_cycle_schedule`. It also consumes the cheap-window model. Guarding only
   the ordinary `CHEAP_CHARGE` branch would leave a recharge route unprotected.
6. `deployment/files/automations/house_battery.yaml` is a notification-only
   heartbeat sentinel, not the charging decision-maker. The car battery
   automations manage EV targets and smart-charge settings separately.

The existing planner test
`test_adjusted_bonus_rate_authorizes_charge_after_dispatch_turns_off` and the
controller's dispatch-state parametrization deliberately encode the existing
behavior. Off can be legitimate within a previously qualified half-hour, but
those tests do not require evidence that the half-hour ever qualified.

Historical task cards, including T0039 and T0046, describe earlier lease and
authority decisions. They are not evidence that a scheduled bonus period is
billable off-peak under these requirements.

## Proposed guard for discussion

Separate **candidate price opportunities** from **permission to import for
battery charging**. The guard is an additional constraint on all grid-charge
intents; it does not replace profitability checks or inverter safety checks.

1. During the guaranteed overnight window, permit the normal household
   off-peak path independently of EV state. Anchor this permission to the
   explicit local-time window, not merely the `STANDARD_CHEAP` label.
2. Outside it, start each settlement period unqualified. The agreed local
   qualification rule is an observed temporal intersection, at any time within
   that half-hour, of `intelligent_state == SMART_CONTROL_IN_PROGRESS` and
   `sensor.ev_charger_energy_meter_power > 50 W`. User-reported charger idle
   draw is 2–3 W. Exactly 50 W does not qualify. No minimum charging duration
   or independent dispatching-binary-sensor condition is required. Scheduled
   dispatch, low adjusted price, plugged-in status or a smart-charge-enabled
   switch alone cannot qualify it. Charge Cap is enabled; the rule does not
   independently reconstruct the six-hour allowance.
3. Latch a successful qualification for that exact half-hour. Record the period
   start/end, evidence time/source and eligibility basis. An ordinary subsequent
   stop, zero power or schedule removal does not erase that historical fact.
   Contradictory evidence that the original qualification was invalid must
   revoke it. Existing economic checks may still choose to stop charging.
4. End every bonus native charging segment at or before the qualified period
   end, retaining the shorter existing lease/component bound where applicable.
   Clear qualification at :00/:30 and wait for evidence from the new period.
   Continuous EV charging can qualify successive periods, but an old positive
   sample cannot. Do not pre-arm a future unqualified bonus half-hour.
5. Apply settlement qualification to the **recharge portion**, not the
   discharge portion, of full-battery cycles (user clarification, 2026-09-22).
   Every recharge segment must be covered by qualified bonus periods or the
   guaranteed overnight window. Discharge need not fall in a qualified
   half-hour; its existing reserve and economic constraints still apply. Do not
   pre-arm recharge in an unqualified future bonus period. Preserve the current
   recharge-before-discharge enable ordering unless explicitly redesigned.
   Whether to discharge speculatively before recharge can be authorized is a
   separate policy question, not permission to pre-arm unqualified recharge.
6. Missing, stale, ambiguous or manual-charge evidence means no new bonus
   permission. After restart, default to unqualified unless dated trustworthy
   evidence can be restored for the current period. Never restore a bare boolean.

Use UTC instants for period identity and ordering, with Europe/London for the
overnight policy. Cover both repeated autumn half-hours and the spring clock
change. At 05:30 fresh bonus qualification is required; at 23:30 the guaranteed
window begins regardless of the EV.

The local guard cannot anticipate later qualifying activity: wait for evidence
even though billing may retrospectively discount earlier minutes. A short gap
at a half-hour boundary is preferable to importing on assumed future charging.

### Agreed authorization interface

`charge_is_authorized(start, end)` checks complete coverage of the half-open
charging interval `[start, end)` by the union of:

- guaranteed overnight windows, independent of either EV signal; and
- the current half-hour, if its qualification latch has been set by the
  simultaneous smart-state and above-50-W observation described above.

The predicate must consult the latched evidence, not require the smart state or
power to remain active on every call. A later transition to `BOOSTING` cannot
establish a new qualification, but does not erase a preceding qualifying smart
charge in the same half-hour. Separate observations of smart state and charging
that never overlap do not qualify. Evaluate when either input reports and on
settlement boundaries; only temporally valid observations can establish overlap.
Unknown/unavailable/stale data cannot establish a new qualification.

The subsequently agreed simpler design wakes the controller on changes to either
sensor and uses existing timed wakeups, including settlement boundaries. On each
wake, evaluate both current values and their independent evidence timestamps.
Both observations must belong to the current half-hour; a still-fresh observation
from the previous half-hour is insufficient. Do not use `last_changed` as the
freshness timestamp: a repeated `SMART_CONTROL_IN_PROGRESS` report is relevant
just as a repeated above-50-W power report is. Neither input's report refreshes
the other's evidence timestamp. Verify actual source freshness rather than
treating a republished cached value as a new measurement. Do not require both
values to change or both reports to arrive simultaneously.

Separate report listeners are not needed solely to wake the planner if timed
wakeups inspect current report timestamps. They remain an option to reduce
qualification latency, not part of the agreed minimum implementation. Expire the
latch at each settlement boundary, independently of sensor changes.

Only the current bonus period needs to be retained for control. Future bonus
periods remain unauthorized; future static overnight windows are known. Native
charging segments must fit within this coverage, including cycle recharge.
Discharge segments are not subject to this predicate. Recheck before enabling
a charge slot to reject permission that expired during schedule preparation.

## Evidence and enforcement still to resolve

User-confirmed on 2026-09-22: Charge Cap is enabled. Boost has not been used,
but future Boost use must be supported and must not qualify a bonus period.
Charge Cap reduces the risk of smart charging beyond the cheap allowance; it
does not prove actual charging, smart-control attribution or per-period billing
eligibility. The guard must not assume the setting can never change.

The [Octopus integration's Intelligent entity documentation](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/intelligent/)
provides concrete attribution candidates (checked 2026-09-22):
`sensor.octopus_energy_<device>_intelligent_state` distinguishes
`SMART_CONTROL_IN_PROGRESS` from `BOOSTING`; the former includes scheduled
activity and still needs physical charging evidence. The
`binary_sensor.octopus_energy_<device>_intelligent_dispatching` sensor is
documented to exclude bump charging, while its treatment of planned versus
started dispatches depends on integration settings. A separate
`switch.octopus_energy_<device>_intelligent_bump_charge` exposes bump control.
Read-only SSH inspection confirmed Octopus integration version 19.0.1 and all
three entities registered and enabled for device
`00000000_0009_4000_8020_00000007d6b4`. The installed `current_state.py` reads
`result.dispatches.current_state`; the bump switch derives its state from the
source of a current planned dispatch. The current-state sensor is therefore
the selected attribution signal, with charger power providing physical evidence.
Dispatching is not an additional qualification requirement. Source retrieval freshness and
behavior during a real smart/Boost transition remain to be validated.

- The repository already references `sensor.ev_charger_energy_meter_power` in
  `deployment/files/templates/power_cost_rates.yaml`. Verify its actual source,
  timestamps, reporting cadence and failure behavior. The chosen threshold is
  strictly above 50 W, based on user-reported 2–3 W idle draw. Power demonstrates
  charging, not who requested it or which rate applies.
- Validate the selected smart-state signal during transitions and determine
  whether the confirmed Charge Cap setting is observable. The selected rule
  relies on Charge Cap for allowance protection; record this operational
  assumption rather than treating smart-control state as allowance proof.
- Choose signal freshness and evidence retention to implement the agreed overlap.
  Avoid attributing delayed samples to their arrival half-hour. A cumulative
  energy delta spanning a boundary cannot by itself locate the charging on
  either side of that boundary.
- If these signals cannot establish qualification reliably, use overnight-only
  grid charging as the conservative fallback. Local telemetry is a proxy for
  Octopus's billing decision; validate it against settled half-hour records.
- Enforce deadlines in native inverter times, not just an HA timer. Measure
  inverter clock error, end-time semantics and stop latency; choose an early
  stop margin if needed. Revalidate permission before enabling a delayed write.
  Test cleanup of already-armed ordinary and cycle recharge slots at rollout.
- Native TOU slots repeat daily. A bounded end time protects the immediate
  boundary but does not prevent next-day recurrence during an extended outage.
  Retain explicit stop/recovery diagnostics; an absolute outage guarantee needs
  independent expiry/enforcement beyond the existing notification sentinel.
- Expose the authorization reason, settlement period, evidence timestamp,
  deadline and blocked reason separately from scheduled cheap-rate diagnostics.
  Cost sensors using current rates remain estimates, not qualification evidence.

## Acceptance cases for implementation

- Overnight charging with the EV unplugged, full or unavailable.
- Scheduled bonus with no EV draw; no battery grid charge.
- The 18:00–18:12 example: permission survives to 18:30, never into 18:30–19:00.
- Continuous smart charging across a boundary: only new-period evidence renews.
- Smart charging begins at 18:20: battery waits, then may charge until 18:30.
- Boost-only charging, exactly 50 W or lower, stale positive power, missing
  attribution and non-overlapping smart/power observations cannot qualify.
- A smart-state/power overlap followed by zero power or Boost retains the
  already-qualified half-hour. Confirm Charge Cap handles allowance exhaustion;
  the local predicate does not independently measure allowance usage.
- Restart, delayed/out-of-order samples, DST, 05:30 and 23:30 transitions.
- Ordinary lease renewal, paired-cycle recharge, slow/failed writes and HA loss
  near the boundary cannot program charging beyond the authorized deadline.
- Reconcile predictions against Octopus settled household half-hours; report
  unverified physical stops and recurring-slot outage limitations explicitly.

## Recorder investigation, 22 September 2026

Read-only SQLite inspection covered the seven days ending approximately 20:40
Europe/London. These are recorder observations, not a complete event capture.

- `sensor.ev_charger_energy_meter_power` is an enabled Shelly entity. There were
  12,683 recorded state rows, including 712 above 50 W. Recent charging readings
  were approximately 7.44–7.45 kW with changed readings approximately 14–29 seconds
  apart in the displayed sample. For example, 7,442.2 W was updated at 20:36:12
  and last reported at 20:36:16 on 22 September. This provides evidence of
  unchanged reports as well as changed values. The attributes have no device
  measurement timestamp.
- Intelligent State had only 38 recorded rows. One
  `SMART_CONTROL_IN_PROGRESS` row began at 18:58:30 on 17 September and had a
  final `last_reported_ts` of 04:13:22 on 18 September. Requiring a value change
  within each half-hour would therefore incorrectly reject continuing smart
  control. Another such row began at 18:52:54 on 21 September and reported as
  late as 00:10:11 on 22 September.
- Recorder rows expose `last_updated_ts` and `last_reported_ts`, but do not give
  a separate row for every unchanged report. The current smart-state row began
  at 17:52:44 on 22 September and its stored `last_reported_ts` was null at query
  time. This cannot establish its live HA `last_reported`, nor prove absence of
  intervening reports. Do not derive complete per-half-hour freshness coverage
  from these rows alone.
- Installed Intelligent State reads `result.dispatches.current_state` from its
  coordinator and exposes no retrieval timestamp attribute. The coordinator
  uses `always_update=True`. Its code retains an independent `last_retrieved`
  and explicitly distinguishes a refresh from cached data when processing
  started dispatches. Consequently, HA report time alone has not been verified
  as proof of fresh Octopus retrieval.
- The dispatches and settings `data_last_retrieved` entities are both disabled
  by the integration in the entity registry. No entities were enabled or changed.

Conclusion: changed-value timestamps are incompatible with the proposed guard;
report/source timestamps are required. The history contains repeated reports,
but full compatibility with strict in-slot source freshness remains unverified.
Further source/live inspection was blocked by an SSH signing-agent authentication
failure; 1Password sign-in also did not complete and was cancelled. Resume with
working authentication to confirm live reporting cadence and successful source
retrieval timestamps before implementation. No runtime changes were made.

### Follow-up: retrieval entity enablement

At the user's request, authentication was retried successfully and the dispatches
retrieval entity's registry `disabled_by` was changed from `integration` to null
through the HA websocket API on 22 September 2026. The settings retrieval entity
was not changed. The newly enabled entity did not appear in live states within
the initial verification wait; loading and live-value verification remain pending.
A follow-up credential read failed with `promptError`, and the sign-in retry
returned `authorization prompt dismissed`. No Octopus reload was performed.

Additional installed-source inspection confirms a one-minute coordinator tick
and a normal three-minute dispatch API refresh interval. Successful retrievals
advance `last_evaluated`, which supplies this coordinator's `last_retrieved`;
the inspected cached/failure paths preserve the previous value. Use the retrieval
sensor's timestamp **state value**, rather than Intelligent State's HA
`last_reported`, as the smart-state source freshness evidence. Listen for its
changes as well as the two condition sensors, or rely on the minute wakeup to
notice advancement. Strict in-slot qualification can wait several minutes after
a boundary for the next successful retrieval.

Enablement was verified successfully at 21:23:59 Europe/London on 22 September
after authentication was restored. The entity was already loaded by this point;
no explicit reload was needed. Its state was `2026-09-22T20:22:50+00:00`
(21:22:50 local), with `attempts: 1` and `last_error: None`. Its HA
`last_reported` was 20:23:50 UTC, demonstrating the distinction between source
retrieval time and a later entity report. Intelligent State was
`SMART_CONTROL_IN_PROGRESS`, but charger power was 3.1 W: that observation alone
does not establish a qualifying smart charge. The source timestamp is now
available for the proposed guard. The temporary API credential file was removed
after verification; battery-controller runtime code remains unchanged.

### Incident captured before 21:30 and projected forward

The user identified a continuing 21:01–00:30 IOG schedule after EV charging had
stopped. A read-only state/history capture at approximately 21:29 confirmed:

- EV power 7,464 W at 21:09:14, falling to 2.8 W at 21:11:17 and 3.1 W at
  21:11:19; it was still 3.1 W in the captured current state.
- Intelligent State remained `SMART_CONTROL_IN_PROGRESS`, with dispatching on
  and successful retrieval at 21:28:50. The planned schedule actually included
  21:01–21:30, 21:30–00:30 and an additional 00:30–01:00 SMART segment.
- The battery controller requested `CHEAP_CHARGE`, with SOC 49% and reported
  battery power +4,536 W. The observed 21:00 half-hour included actual EV draw;
  the regression target is subsequent half-hours without renewed EV charging.

Raw local evidence: `scratch/iog_ev_stopped_20260922.json`. The reduced fixture is
`deployment/files/custom_components/house_battery_control/tests/scenarios/projections/iog_ev_finished_2026_09_22.yaml`.
At the user's request, future EV power is forward-filled to 3.1 W. All future
reports and retrievals are explicitly synthetic; this is not a claim about what
the live EV actually did after capture. The four projected bonus half-hours
21:30–23:30 must reject charging, while 23:30 returns to unconditional overnight
permission. Test both observed 49% SOC and synthetic 100% SOC (cycle recharge).

Before implementation, the executable projection reproduced eight unauthorized
bonus-charge cases as strict expected failures; both overnight cases passed.
With the guard implemented, all ten projection cases pass normally. Two
sequential guard/planner/Solis-adapter replays additionally verify latch survival,
expiry, native-slot cleanup and overnight resumption. The full affected test set
passes 224 tests. See [T0051](tasks/T0051-iog-settlement-charge-guard.md) and the
[commissioning log](house-battery-commissioning-log.md) for deployment results
and natural live scenarios that remain to be observed.
