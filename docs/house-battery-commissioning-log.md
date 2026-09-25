# House battery commissioning log

> Historical design/evidence. The current runtime contract is [native TOU schedule control](house-battery-control.md), updated 11 September 2026. Earlier Self-Use fallbacks and peak-shaving handovers are superseded.


This is the chronological evidence log for live Solis battery-control
experiments. Record the exact controls, authoritative readback and physical
power-flow outcome for every experiment. Home Assistant state alone is not
proof that the inverter acted.

## Evidence conventions

- The inverter is a Solis S5-EH1P6K-L, model ID `3105`, firmware `4D0051`.
- Battery power is positive while charging and negative while discharging.
- A control write is proven only when its value persists in SolisCloud or a
  subsequent direct Solis API read.
- Charge/discharge behaviour is proven only by physical power flow: battery
  power plus whole-site Octopus demand/export, ideally followed by SOC movement.
- Manual experiments must first stop the controller and stale-heartbeat sentinel
  so there is one writer. The superseded control-disable helper no longer exists.

## Established findings

| Finding | Evidence | Status |
| --- | --- | --- |
| Forced discharge works | A discharge slot produced approximately the inverter's full discharge/export power, rather than only the roughly 740 W house-load peak-shaving response. | Proven |
| Grid charging works with Feed-In Priority and Grid Peak Shaving disabled | A manually configured native charge slot produced `5.025 kW` battery power and `91.7 A` battery current in fresh SolisCloud telemetry. | Proven |
| Peak Shaving does not prevent scheduled discharge/export | Forced export was observed with Peak Shaving enabled. | Proven |
| Normal operation exports surplus PV rather than charging the battery | At the 17% reserve boundary in `RESERVE_FOLLOW`, fresh telemetry showed battery power around 0 W while whole-site demand remained net export by approximately 1.9 kW. Reserve planning may offset concurrent load with PV but must not bank forecast surplus for later demand. | Proven |
| Grid Feed-in Power Limit can remain disabled | The DNO-approved export limit equals the site's installed inverter capacity, so no lower Solis software limit is required. The earlier export blocker was an enabled limiter set to 0 W / 0 A. | Commissioned |
| Legacy TOU bit is not the TOU-v2 activation mechanism | Direct CID 636 writes of `114` and `98` were accepted but normalized immediately to `112` and `96`, stripping bit 1. | Proven |
| A cross-midnight interval was not the only charge failure | `21:56-07:00` did not produce charging, and changing the same slot to the same-day interval `21:56-23:59` also did not produce charging. | Proven |
| Immediate HA control state is not device proof | HA and direct control reads showed slot 1 enabled with the requested current, target and time, while physical battery power remained slightly negative and whole-site demand reflected the EV only. | Proven |
| TOU-v2 uses discrete six-slot controls | The deployed Solis Cloud Control integration detects CID 6798 value `43605` and uses slot CIDs 5916-5987; it deliberately hides the legacy global Time Of Use switch. | Proven from deployed integration source |
| Dynamic reserve and load-following floor are separate controls | Forced-discharge slot targets stop export at the quantized model reserve. Global Battery Reserve remains enabled at the fixed 10% safety floor so Peak Shaving can consume that planned reserve for house load. | T0049 deployed; load following proven, later forced-export boundary proof pending |

## Experiment history

### Reserve-discharge commissioning

- Intent: prove that a scheduled discharge slot can export at inverter power.
- Outcome: full scheduled discharge/export was observed.
- Conclusion: slot scheduling and forced discharge are available on this
  inverter. Peak Shaving does not block forced export.

### Legacy TOU-bit tests

- CID 636 `112 -> 114`: API returned success; immediate authoritative read
  normalized to `112`.
- CID 636 `96 -> 98`: API returned success; immediate authoritative read
  normalized to `96`.
- Conclusion: do not require or attempt to preserve the legacy TOU bit on this
  TOU-v2 firmware.

### Initial charge-slot tests

- Slot 1 controls read back as enabled, `100 A`, target `100%`.
- Cross-midnight interval `21:56-07:00`: no physical charging.
- Same-day interval `21:56-23:59`: no physical charging.
- Feed-In Priority with grid charging allowed, both with and without battery
  reserve: no physical charging.
- Whole-site demand remained approximately the EV demand rather than rising by
  another inverter-sized load; battery power remained slightly negative.
- Conclusion: the failure was not explained by the interval crossing midnight,
  reserve mode, or the legacy TOU bit.

### 2026-08-23 known-good-mechanism comparison

Baseline at approximately 21:49 UTC:

- Controller guard asserted; controller health `fail_safe`.
- Intelligent Octopus dispatch active; EV demand approximately `7.5 kW`.
- Battery SOC `73%`; battery power approximately `-0.24 kW`.
- Direct CID 636 read `33`: Self-Use, grid charging allowed, reserve off, legacy
  TOU off, Peak Shaving bit off.
- Direct CID 5916 read `0`: charge slot 1 disabled.
- No battery alarm; BMS charge-current capability reported available.

Staged while the slot remained disabled:

- CID 5946: `00:00-23:59`.
- CID 5948: `100 A`.
- CID 5928: `100%`.
- All writes returned Solis API code `0` and immediate direct readback matched.

Direct leaf-control result:

- CID 5916 was enabled and its readback persisted.
- From 21:56:47 to 21:59:06 UTC, whole-site demand remained approximately
  `7.49-7.54 kW`, battery power remained approximately `-0.24 kW`, and SOC
  remained `73%`.
- The slot was disabled again.
- Conclusion: enabling CID 5916 after writing the time, current and target leaf
  controls did not cause physical charging.

Grouped SolisCloud editor result:

- The web editor rejected the original `00:00-23:59` charge interval because a
  stored, disabled discharge interval overlapped it. This demonstrates that the
  grouped editor validates all stored intervals, including disabled slots.
- Charge slot 1 was changed to `22:00-23:59`. The grouped editor then accepted
  the enabled slot and closed normally.
- Direct readback showed CID 5916 enabled, `22:00-23:59`, `100 A`, target
  `100%`, and TOU-v2 marker CID 6798 still `43605`.
- Neither Self-Use (CID 636 value `33`) nor Feed-In Priority (value `96`)
  produced physical charging. Whole-site demand remained approximately the EV
  load and battery power remained slightly negative.
- The advanced Solis telemetry timestamp later stalled, but fresh Octopus demand
  independently showed that an inverter-sized grid charge had not started.
- Cleanup restored slot 1 disabled and Self-Use value `33`.

### Quick Control differential probe

The Solis mobile app's known-good **Quick Control > Force Charge** action is a
diagnostic hint, not the intended runtime control mechanism. The controller must
continue to use native charge slots. Capture relevant controls immediately before
and after a Quick Control action to discover the prerequisite or mode change that
the unsuccessful slot configuration is missing, then reproduce that prerequisite
with a native charge slot.

### 2026-08-23 experiment invalidation and root causes

The negative charge results above are not valid evidence that TOU-v2 charging is
unsupported:

- The custom integration was unloaded, but
  `automation.house_battery_independent_watchdog` remained enabled. With the
  controller entities unavailable, it triggered every minute and repeatedly
  applied the fail-safe script. At 22:34:00 UTC it triggered again; CID 5916 was
  observed changing from enabled back to disabled between samples. This
  invalidated manual slot experiments by racing their writes.
- The first future-boundary test wrote `22:32-22:44` as though the register used
  UTC. Solis schedule values are inverter-local wall-clock times. The inverter
  was around 23:32 BST, so that boundary had already passed by one hour.
- After the watchdog was explicitly turned off, slot 1 was armed for the correct
  local interval `23:37-23:49` and remained enabled across the boundary. However,
  SolisCloud then reported the inverter offline. Whole-site import did not rise,
  so the cloud configuration was never proven to have reached the inverter.
- The Solis API accepted and echoed configuration while the inverter was
  offline. Therefore control API readback is cloud/control-plane evidence only;
  it is not proof of device execution.
- Mobile **Quick Control > Force Charge** was also ineffective during the same
  offline period and produced no persistent register delta or physical charge.

Cleanup disabled CID 5916 and verified it off. At that historical point the
legacy controller remained unloaded, its guard was asserted and its watchdog
was paused. Those superseded surfaces were later removed. The subsequent valid
online charge proof is recorded below.

### 2026-08-24 proven native grid-charge configuration

At `00:47 BST`, SolisCloud readback showed the following configuration while
the battery was physically charging:

| Control | Readback |
| --- | --- |
| Feed-In Priority | Enabled |
| Allow Grid Charging | Enabled |
| Battery Reserve | Enabled |
| Reserved SOC | 17% |
| Grid Peak Shaving | Disabled |
| Retained Grid Peak Shaving limit | 100 W |
| Charge slot 1 | Enabled, `00:24-00:59` inverter-local time, target 100%, current 100 A |
| Charge slots 2-6 | Disabled, `00:00-00:00`, current 0 A, target 100% |
| Discharge slots 1-6 | Disabled, `00:00-00:00`, current 0 A, target 100% |

Fresh SolisCloud telemetry timestamped `2026-08-24 00:46:56 BST` reported:

- battery power `+5.025 kW` (charging);
- BMS battery power `+4.962 kW`;
- battery current `91.7 A`;
- battery voltage `54.8 V`;
- battery SOC `73%`; and
- no battery alarm.

A second fresh sample at `00:56:57 BST` reported `+5.028 kW`, `91.6 A`,
SOC `76%`, and today's charged energy `1.0 kWh`. This proves sustained physical
charging and SOC movement throughout the scheduled interval.

The first fresh sample after the `00:59` slot end, timestamped `01:01:57 BST`,
reported battery power `-0.209 kW` and whole-site grid power approximately
`+0.099 kW`. This proves the forced charge stopped at the configured end time
and ordinary low-import operation resumed.

Conclusion: TOU-v2 native charge slots work on this inverter when Feed-In
Priority and Allow Grid Charging are enabled and Grid Peak Shaving is disabled.
This is the working reference configuration for controller implementation.

The Grid Peak Shaving maximum-grid-power limit is manually commissioned at
100 W (0.1 kW) and remains unmanaged. Runtime owns only the enable switch:
reserve following enables it, while bounded charge/discharge slots disable it
after their native slot is authoritatively armed. See the
[SolisCloud remote-control setting reference](https://solis-service.solisinverters.com/en/support/solutions/articles/44002638862-solis-cloud-remote-control-settings-desktop-version)
and the
[peak-shaving operating overview](https://www.solinteg.com/seo-blog/what-is-peak-shaving-commercial-energy-costs.html).

The ownership boundary is:

- Feed-In Priority is the manually commissioned healthy-mode baseline. Healthy
  runtime operation must verify it and need not rewrite it on every evaluation.
  If fail-safe changes the mode to Self-Use, recovery must restore the
  commissioned Feed-In Priority baseline once.
- Allow Grid Charging, Battery Reserve and its target, and
  all six charge/discharge slot fields are runtime-owned.
- Grid Peak Shaving's enable switch is runtime-owned; its 100 W limit is an
  installation invariant and is never discovered or written at runtime.
- Enabled directions are reconciled against the one logical intent. After a
  controller-owned direction is confirmed off, its time and current may be
  reset once as best-effort housekeeping; there is no all-slot normalization
  sweep.
- Every prospective charge/discharge schedule is checked against all configured
  direction enables before mutation. Active intervals are half-open `[start,
  end)`, so adjacent boundaries do not overlap. A logical interval crossing
  inverter-local midnight uses two adjacent native slots.
- Grid Feed-in Power Limit is disabled as a one-time commissioned setting; the
  runtime does not enable it or write maximum power/current values.

## Current runtime architecture and remaining evidence

T0026 supersedes the historical guard, broad fail-safe script and independent
all-controls watchdog described in earlier experiments. The release candidate
has one event-driven controller and one Solis writer/lock. Best-effort starts use
bounded retries; important stops persist until authoritatively off. Temporary
input or telemetry loss is `DEGRADED` and recovers passively. Fifteen minutes of
continuous degradation latches a mode-only Self-Use fail-safe. The remaining
crash sentinel can write only Self-Use after a stale heartbeat and recheck.

Control-plane readback remains provisional. The authenticated live release gate
must still prove the exact local-midnight representation and two-slot charge and
discharge behavior, a full-SOC discharge/recharge cycle, restart/fail-safe/
shutdown behavior, and 24 hours without overlap or next-day slot recurrence.

## 2026-08-24 lean-controller cutover

Pre-deployment readback at approximately `08:44 BST` proved the legacy guard
asserted, storage mode `Self-Use`, and all twelve charge/discharge directions
authoritatively off with `00:00-00:00`, `0 A` housekeeping values. The legacy
fail-safe had left Grid Peak Shaving enabled. A two-minute read-only WebSocket
capture recorded two complete 190-entity snapshots before mutation.

Ansible staged the lean component and configuration, removed only the battery
component's stale `__pycache__`, deleted the managed legacy guard/fail-safe
files, and passed `ha core check` before restarting Home Assistant. Deployment
completed with `ok=143`, `changed=9`, `failed=0`, `unreachable=0`.

Post-restart verification proved:

- Home Assistant Core `2026.8.3` and a successful second `ha core check`;
- exactly `__init__.py`, `config.py`, `controller.py`, `model.py`, `planner.py`,
  `solis.py`, `sensor.py`, and `manifest.json` in the component source root;
- the old guard entity absent and the old script/watchdog present only as inert
  restored `unavailable` states;
- the new stale-heartbeat sentinel enabled;
- controller health `healthy` with an advancing heartbeat;
- commissioned Grid Peak Shaving `off`, Feed-In Priority restored, Allow Grid
  Charging `on`, Battery Reserve `on`, and reserve target `17%`; and
- one and only one native direction enabled: discharge slot 2,
  `09:08-23:30`, `100 A`, target `17%`.

Fresh telemetry at approximately `09:08 BST` reported battery power
`-4.914 kW`, current `-93.6 A`, voltage `52.5 V`, and SOC `78%`. The Octopus
current-demand meter simultaneously reported `-5.802 kW`, proving whole-site
export rather than ordinary house-load peak shaving. This is accepted physical
proof that the deployed `RESERVE_DISCHARGE` intent and native discharge slot are
effective with Grid Peak Shaving disabled.

The requested observability surface is live under the clean entity IDs
`sensor.house_battery_energy`, `sensor.house_battery_reserve_target`, and
`sensor.house_battery_reserve_balance`. Initial values were respectively
`25.079808 kWh`, `5.21536 kWh`, and `19.864448 kWh`; the arithmetic agrees with
`32.1536 kWh × 78%`, the 17% reserve target, and actual minus target.

### Cutover defects found before soak

The clean 24-hour soak is blocked pending two narrow fixes found by the first
live minute boundary:

1. Active reserve-discharge intent used the current minute as its start. The
   desired native interval therefore changed from `09:08-23:30` to
   `09:09-23:30` at the next backstop and strict reconciliation stopped an
   otherwise-correct owned slot. Continuity must retain the original start only
   while the same owned direction/current/target/end remains active and `now`
   remains inside its half-open interval.
2. The first `turn_off` timed out after the Solis switch optimistically changed
   to `off`, making the stop ambiguous. Later forced calls completed, but an
   idempotent off switch emitted no newer HA revision, so proof could never
   complete. A completed forced `turn_off` whose captured state was already
   exactly `off` may accept the unchanged exact-off state; this exception must
   not apply to starts or other targets.

The deployed control integration itself retries for 30 seconds and each HTTP
request can take 30 seconds. The controller's independent 10-second service cap
can therefore cancel a legitimate retrying call. One finite whole-write bound
must cover the integration's approximately 60-second worst case plus readback.

The evidence collector captured eleven duplicate stop calls by `09:17 BST`, no
mode writes, no overlap, and a fresh controller heartbeat. This capture is kept
as commissioning evidence and is not eligible as the clean 24-hour soak.

## 2026-08-24 telemetry recovery and reserve-discharge continuity

The Solis Inverter telemetry integration was returned to pristine pinned
v4.0.1. Live checksum and deployment-marker verification proved that the local
poll-recovery overlay was no longer installed. Pristine v4.0.1 reproduced the
same login/discovery failures while SolisCloud returned HTTP 502 responses to
the independent control integration, exonerating the removed normal-polling
changes as the cause of that outage.

Repeated manual telemetry reloads exposed a separate upstream lifecycle defect:
failed-discovery callbacks are not retained or cancelled on unload, so each
reload can leave another staggered login retry chain. A clean Home Assistant
Core stop/start removed all leaked callbacks. Manual telemetry reloads are
therefore prohibited during commissioning; use one clean Core restart only when
an operator reset is genuinely required.

After the clean restart, the controller began fresh in `DEGRADED` rather than
the previous latched `FAIL_SAFE`. Pristine telemetry discovered successfully,
populated at `12:30:39 BST`, and refreshed again at `12:40:52 BST`. The greater
than ten-minute sample interval proves the integration is slow under SolisCloud
degradation but still completes; a 60-second whole-update timeout would have
cancelled this valid late result.

The controller recovered passively and selected `RESERVE_DISCHARGE`. Live state
proved Feed-In Priority, discharge slot 2 enabled with `12:31-23:30`, `100 A`,
target `17%`, and every other direction off. The slot and schedule remained
unchanged across more than three minute boundaries. From `12:31:52` through
`12:40:52 BST`, controller health remained healthy with no stop/start churn.
Whole-site export remained approximately `4.8-6.8 kW`; the fresh `12:40:52`
telemetry sample reported SOC `89%` and battery power `-4.920 kW`. This accepts
the live reserve-continuity and physical-discharge gates from T0035.

## 2026-08-24 Intelligent dispatch and cheap-charge continuity

An Intelligent dispatch beginning at approximately `14:39 BST` correctly
changed the fused tariff state to `BONUS_CHEAP`. The controller stopped
reserve discharge before enabling charge, so no charge/discharge overlap was
observed. Before T0038, the active charge schedule chased the current minute,
toggling slot 1 seventeen times during an 81-sample observation. Controller
health was degraded in 78 samples and Solis control writes recorded eleven
timeouts. The slot churn interrupted charging even though the dispatch
continued.

T0038 extends the existing active-slot continuity rule only to an exact
`CHEAP_CHARGING` charge segment. Independent implementation and safety reviews
accepted the focused change; all 100 component tests passed. Ansible deployed
it with `ok=140`, `changed=4`, and no failures.

After the required Core restart, telemetry moved from `unavailable` to
`unknown`, then produced a fresh measurement and the controller recovered
passively from `DEGRADED/IDLE` to `HEALTHY/CHEAP_CHARGE`. Charge slot 1 was
enabled at `15:15–16:00`, `100 A`, target `100%`; all discharge slots remained
off. The schedule stayed exactly `15:15–16:00` through the `15:16`, `15:17`,
and `15:18` minute boundaries with healthy controller state. Whole-site demand
rose from approximately `7.6 kW` to `12.7 kW`, proving about `5 kW` of battery
charging alongside the EV. Dispatch-end stop proof remains pending.

## 2026-08-24 Solis text-entity midnight boundary

Fresh read-only controller evidence from `23:44–23:48 BST` showed
`CHEAP_CHARGE` attempting to write charge schedules ending in `23:xx-24:00`.
The Solis text entity accepts hours `00–23`, so the `24:00` end representation
was rejected. Both charge slots remained off; this interval supplies no
physical charging proof.

The deployed boundary is therefore commissioned as `23:59`. The split remains
two native local schedules, with the explicit one-minute gap `[23:59, 00:00)`
accepted as a Solis text-entity limitation. No schedule encoding redesign is
needed. Physical proof that the first segment stops and the next-day segment
becomes effective remains part of the T0034 acceptance.

## 2026-08-25 post-midnight charge recovery and T0043 boundary

The old `24:00` text-entity representation was rejected through midnight.
At `00:00`, the controller recovered with charge slot 1 at `00:00-05:30`.
By `00:04`, fresh passive telemetry showed SOC rising from `18%` to `19%`,
battery charge power of `5.014 kW`, and whole-site demand of approximately
`5.87 kW`. This proves effective post-midnight physical charging.

It does not prove prearmed split continuity: the first segment had already
failed at the old `24:00` boundary, so the later slot was recovered after
midnight rather than continuously armed across the boundary.

T0043 therefore gives only actionable one-segment `STANDARD_CHEAP` charge
intents a stable, minute-ceiled start while the validated source retains the
prior phase history. Fused history may omit the prior day at rollover; in that
case the adapter may reuse one already-enabled allocated cheap slot when its
owner, direction, values, native end, and active half-open schedule match
exactly. An inactive past slot may be stopped while the active slot continues.
Bonus charge remains positional and now-based with its existing native lease
clipping, and full-SOC cycle timing remains independently now-based. The
exact-start `23:59` bonus empty-first-segment edge is deferred and is not
broadened; T0042's one-minute boundary remains in force.

## 2026-08-25 reserve forecast and stable bonus authority

T0045 and T0046 were deployed together through the full Ansible playbook.
Configuration validation passed, Home Assistant restarted, and the play
completed with `ok=140`, `changed=4`, and no failures.

The corrected reserve model produced a total target of approximately
`5.284 kWh`: `3.215 kWh` absolute 10% safety floor, `2.000 kWh` configured
margin, and `0.068 kWh` modelled household forecast. Live entities exposed the
total, usable and forecast values separately. The Power dashboard was saved
and read back with `Reserve (Forecast)` immediately after `Reserve (Usable)`.

An active Intelligent Dispatch provided live T0046 acceptance. Across four
samples spanning a minute boundary, charge slot 1 remained fixed at
`19:26-19:30`, the lease deadline remained `19:30 BST`, controller state stayed
`HEALTHY/CHEAP_CHARGE`, no operation was pending, and Peak Shaving remained
off. The previous every-minute stop/recreate churn did not recur.

At the half-open dispatch boundary, health entered `DEGRADED` while the charge
stop was being confirmed; the slot changed to off within 19 ms. Health first
recovered 3.4 seconds later. A further 1.76-second transition covered the
staged return to reserve following. By `19:30:28.552 BST`, the controller was
stably `HEALTHY/RESERVE_FOLLOW`, all slots were off and Peak Shaving was on.
This is the intended truthful in-progress health signal, not fail-safe
escalation.

## 2026-08-27 reserve-floor coupling diagnosis

Read-only live evidence showed battery SOC and the global Battery Reserve SOC
both at 21%, with all native slots off and the controller reporting
`HEALTHY/RESERVE_FOLLOW`. Exact battery energy was `6.752256 kWh`; the reserve
target was `6.44077738538028 kWh`; `Reserve (Usable)` was
`3.22541738538028 kWh`; and the exact balance was `0.311478614619722 kWh`.
The control-domain balance was zero because both battery and target quantized to
21%.

House demand was approximately 323 W while fresh battery output was only
approximately 36 W. The forced-discharge target was behaving correctly, but
reusing it as the global Battery Reserve floor prevented the planned reserve
from serving house load. T0049 therefore keeps the dynamic target on discharge
slots and fixes global Battery Reserve at the configured 10% safety floor.

Live protection readback during the diagnosis was: Over-discharge SOC 10%,
Force Charge SOC 7%, and Battery Recovery SOC 11%. No control was written during
the diagnosis. Deployment and physical load-following proof remain pending.

T0049 was subsequently deployed through the full Ansible playbook with
`ok=140`, `changed=4`, no failures, successful Home Assistant configuration
validation and a Core restart. Live readback was initially blocked by the local
1Password CLI session and was not inferred from deployment success.

After 1Password API access was restored, live state proved the global Battery
Reserve SOC changed from 20% to 10% while the independently calculated dynamic
reserve remained 20%. Feed-In Priority, Battery Reserve and Peak Shaving were
on, all 12 slot directions were off, and the controller reported
`HEALTHY/RESERVE_FOLLOW`. Battery SOC had fallen from the pre-deployment 21% to
20%, which the old coupled 21% floor prevented.

The first two post-restart whole-site import samples were 355 W and 302 W while
the controller was starting. After the 10% floor was applied, five non-transient
samples were 128, 132, 128, 132 and 138 W, close to the commissioned 0.1 kW
Peak Shaving allowance; one 274 W transient was recorded. Fresh initial Solis
telemetry reported battery power `-36 W`. This accepts the primary T0049
load-following correction. A later active reserve-export occurrence must still
prove its native target remains the dynamic reserve rather than 10%.

## 2026-08-31 full-SOC cycle diagnosis and local rolling-pair implementation

The overnight sequential controller again discharged successfully but failed
to recharge. Static charging first reached 100% SOC. Cycle discharge then ran
for 10 minutes at approximately 4.4 kW and reduced SOC to 98%. The controller
subsequently armed repeated fresh 10-minute recharge intervals with Feed-In
Priority, Allow Grid Charging on and Grid Peak Shaving off, but battery and
site power showed no material charging.

The earlier paired experiment was recovered from `/config/t0047-evidence/` and
found to have exited before sampling because its runner referenced an unset
`label` variable. Its sample file contains zero records, so it did not reject
paired native operation.

T0047 now implements the narrow rolling schedule locally: discharge/recharge,
then recharge/discharge, then discharge/recharge. The active half is retained
at each boundary while only the expired direction is rolled forward. All
intervals are adjacent and half-open; recharge is enabled before discharge;
and no discharge is pre-armed unless its following recharge also fits inside
the trusted cheap window. Local component tests pass. Deployment and physical
proof remain pending, and no live control was written for this change.

## 2026-09-01 rolling-cycle and active-charge proof

Recorder evidence from the first deployed rolling-pair night showed that the
cycle scheduler and native configuration mostly worked. The reliable one-minute
Octopus whole-site demand sensor recorded:

- approximately 3.7 kW export during the first `03:10-03:20 UTC` cycle
  discharge;
- approximately 0.24 kW import, with no forced charge, during the first
  `03:20-03:30 UTC` recharge;
- approximately 4.2 kW export during the next discharge;
- approximately 5.9 kW import during the next recharge; and
- matching approximately 4.2 kW export / 5.9 kW import steps for the following
  two discharge/recharge pairs.

The first recharge was not late API programming. Its `04:20-04:30`
inverter-local charge slot was configured at `03:10:21 UTC` and enabled at
`03:10:23 UTC`, almost ten minutes before its boundary. Feed-In Priority,
Allow Grid Charging, 100 A current, 100% target SOC and Peak Shaving off all
matched the later successful pairs. Other slot directions were neutral before
the first pair. The distinguishing observation was headroom: SOC was reported
at 99% during the failed first recharge and 98% by its end, whereas the first
successful recharge followed the next discharge to 96%. The exact inverter or
BMS recharge acceptance threshold near full SOC is not yet proven; 99% did not
charge and 96% did.

A separate live standard-cheap test proved that a charge interval whose start
was already in the past becomes physically effective. Charge slot 1 was enabled
at `17:48:53 UTC` for `18:48-19:00` inverter-local time, with Feed-In Priority,
Allow Grid Charging on, 100 A, 100% target and Peak Shaving off. Octopus demand
was 7.948 kW at `17:48`, 7.947 kW at `17:49`, and 13.506 kW at `17:50`, then
remained around 13.5 kW. This proves an inverter-sized charge step within about
67 seconds of Home Assistant readback despite stale Solis telemetry. The slot
turned off at `18:00:01 UTC`; the `18:01` demand sample was -3.025 kW, proving
the native end boundary also took effect.

An off reserve-discharge direction retained a stale `07:58-23:30`, 100 A
schedule across a Home Assistant restart. It was neutralised and later briefly
reintroduced during the live test. Charging had already started before the
first neutralisation, and reintroducing the disabled overlap after charging had
started did not stop it. It was restored to `00:00-00:00`, 0 A at the end of
the experiment; after the cheap boundary the healthy controller legitimately
reused it for reserve discharge. This stale configuration is therefore not
evidence for the missed first rolling recharge and does not justify a broad
all-slot write sweep.

At `2026-09-01 18:38:26 UTC`, the live cycle duty helper was changed from 10
minutes to 15 minutes. Home Assistant read back `15.0 min`; controller health
remained `healthy` and its action remained `RESERVE_FOLLOW`. The longer first
discharge should create more headroom before the paired recharge while keeping
each native direction bounded to 15 minutes if a cheap period ends unexpectedly.
The next acceptance observation is a complete discharge/recharge pair starting
from 100% SOC during a trusted cheap interval, with both physical directions
confirmed from the Octopus whole-site demand sensor.

## 11 September 2026: simplified controller deployment

The native-slot controller contract is now documented in
[house-battery-control.md](house-battery-control.md). The Solis TOU experiment
remains preserved in stash `15c0b2f0ebb94436f72a661664a48410b4803b54` and is
absent from the running HA instance.

Merged main commit `eb6ef8a` (managed integration pins and the Octopus export
template correction) while preserving the controller implementation. The full
Ansible deployment completed with 198 successful tasks, 48 changes and no
failures. All six updated component source markers were verified, including
Octopus v19.0.1; ten deployed battery runtime/configuration files matched local
SHA-256 checksums. The focused controller, evidence, watchdog and timeout-patch
suite passed all 170 tests. Unchanged EV test failures were separately reproduced
on the pre-merge main baseline.

Octopus rates recovered after restart. At 23:30:12 BST, controller health was
healthy, action CHEAP_CHARGE, and the native schedule was confirmed through HA.
The notification-only watchdog was enabled at 23:30:31 BST. Direct inverter
reads after activation showed storage mode 112, actual peak shaving 0, global
reserve 10%, enables bitmap 3, SOC 12%, battery voltage 52.9 V and charging
current 94.7 A (approximately 5.0 kW). Whole-site demand rose from 122 W at
23:30 to 5,915 W at 23:31. This confirms physical charging, not just optimistic
HA entity state. Overnight observation through 06:00 BST on 12 September is
still required to verify reaching full SOC and a complete discharge/recharge
cycle; neither is claimed here.

Known bonus-rate issue discovered during startup: the 23:00–23:30 import
interval reported 6.9p/kWh and BONUS_CHEAP while the intelligent dispatch
binary sensor was off. The retained direct-dispatch gate rejected the interval
with `dispatch source is not on`. Dispatch state and adjusted tariff intervals
have different semantics, so this veto is too restrictive. The gate was not
changed in this deployment; its correction remains outstanding.

### Bonus-rate correction prepared; deployment deferred

The deployed controller was committed as `726c7fc`. Version 0.2.1 removes both
the planner's direct-dispatch start veto and the controller's direct-dispatch
stop path. Validated adjusted tariff intervals now remain authoritative after
the EV dispatch turns off. Existing provenance, profitability, tariff-change
reconciliation and bounded bonus lease checks remain in place.

Regression tests reproduced both failures before the fix. The focused battery
suite passes 172 tests afterward, including continued charging with dispatch
off and stopping after tariff withdrawal while dispatch remains on. This fix
is **not deployed**: the user requested batching further deployments so the
static off-peak overnight commissioning run remains undisturbed. Live HA still
runs the 0.2.0 code from `726c7fc`; deploying 0.2.1 and verifying a bonus interval
remain pending.

### Overnight observation outcome, 12 September 2026

The read-only observer ended at 06:00 BST; the scheduled follow-up was paused
after its final review. No inverter settings or deployed files were changed
during monitoring. The bonus-rate fix remains deferred.

Starting from 12% SOC, the battery reached a maximum reported 94% during the
standard cheap window. Repeated device samples showed approximately 4.5–5.1 kW
charging. Native slots and physical charging were observed before and after
local midnight; the known 23:59–00:00 gap was not continuously sampled.

At 05:30 BST, HA showed the charge slot disabled and no enabled direction.
At 05:33 BST, reserve discharge was enabled for 05:30–23:30 with a 23% target,
matching the forecast reserve. A device sample timestamped 05:32:03 BST showed
approximately 4.94 kW discharge, independently supporting the direction change.
This is surplus export after charging, not a full-SOC discharge/recharge cycle.

The 117 polls after native-slot capture was added had no observer read errors
or gaps over 301 seconds. Device telemetry sometimes lagged: the largest
observed device-sample age in those polls was approximately 19 minutes, within
the existing 30-minute threshold. The final observation at 05:57:09 BST showed
a healthy controller in RESERVE_DISCHARGE, no pending error, and a latest
device sample from 05:47:02 BST reporting 90% SOC and approximately 4.95 kW
discharge. Those final physical values were about ten minutes old.

Charging, midnight rollover, end-of-window charge-slot cleanup and subsequent
reserve export were verified. Reaching 100% and completing a full-SOC
discharge/recharge pair remain unverified; overnight commissioning is therefore
partial. Evidence is retained in `/tmp/house-battery-overnight-20260911.jsonl`
and `/tmp/house-battery-overnight-20260911.md` on the observing computer.

## 23 September 2026: Battery Saving disabled — initial evidence

**Status at preparation: user-reported change; see 25 September follow-up below.** Connor
reported disabling Battery Saving in Solis control on 23 September. The exact
change time and subsequent authoritative setting readback have not been
captured. No agent control write was made. Do not yet add this change to the
established findings or fixed commissioning requirements.

### Purpose and controls

Investigate persistent grid import during normal battery load following.
[Solis describes Battery Saving](https://usservice.solisinverters.com/support/solutions/articles/73000670488-battery-energy-storage-export-power-settings-explained)
as powering the inverter's own operating needs from the grid instead of the
battery. This is distinct from Battery Reserve, the 10% SOC floor, and Export
Calibration. The documentation describes the general feature; its exact effect
on this S5-EH1P6K-L and recorded firmware `4D0051` remains to be demonstrated.

The pre-change HA record showed Feed-In Priority, Battery Reserve enabled at
10%, maximum discharge current 100 A and Export Calibration at -30 W. The
commissioned configuration has EMS and actual Grid Peak Shaving disabled;
the latter must be checked independently of the legacy HA peak-shaving switch.
Battery Saving's prior state was not independently read. The only change
reported for this investigation is disabling Battery Saving.

### Pre-change evidence

A read-only HA recorder analysis on 23 September selected 682 one-minute
samples (11.37 hours) from the preceding seven days. Selection required:

- Controller action `IDLE`.
- Reported solar power below 1 W and EV charger power below 30 W.
- Battery SOC above 15% and DC discharge strictly between 40 and 600 W.
- Device telemetry timestamp no more than 15 minutes old and not in the future.
- Numeric readings available for each selected power/SOC input.

| Measurement | Median | Mean |
| --- | ---: | ---: |
| Octopus whole-site grid import | 129 W | 138.5 W |
| Solis meter grid import (sign inverted from telemetry) | 119 W | 123.7 W |
| Battery DC discharge (sign inverted from telemetry) | 202 W | 204.9 W |

The 10th–90th percentile range of Octopus import was 126–134 W. Samples used
the latest recorded value at each minute; cloud telemetry may repeat and is
not synchronous with the utility meter. These are descriptive selected-period
statistics, not a continuous seven-day energy total or calibrated efficiency
measurement. The query output was recorded in the investigation conversation;
a standalone raw-data export was not retained.

The [11 September direct-register investigation](solis-load-following-investigation-2026-09-11.md)
also found means of 165.4 W battery discharge, 45.8 W inverter AC output and
119.2 W grid import over 17 passive samples. The apparent DC/AC difference
suggests losses or internal consumption, but is not an independent measurement
of inverter overhead. Persistent import and conversion loss must be evaluated
separately.

### Original verification plan (before the 25 September calibration change)

- Record the change time if recoverable, current firmware and authoritative
  Battery Saving readback, including when and how the setting was read.
- Confirm Export Calibration remains -30 W and record other relevant controls.
  Keep calibration and operating mode unchanged during the comparison so their
  effects are not mixed with Battery Saving. Passive observation requires no
  competing schedule writer or further control changes.
- Capture fresh post-change intervals after dark with `IDLE`, no active forced
  charge/discharge slot, no EV charging and SOC comfortably above reserve.
  Compare similar house loads and repeat across more than one settled interval.
  Record interval boundaries, sample ages and exclusions; omit the transition
  while telemetry still describes the pre-change state.
- Compare whole-site utility-meter import/export, Solis meter power and raw DC
  battery discharge. Use synchronized AC-output readings where available; do
  not substitute the fixed-95%-efficiency accounting template for measured AC
  output. Check whether reduced import is accompanied by increased battery
  discharge and whether sustained unwanted export appears.
- Report the import reduction and any battery-discharge increase separately.
  If energy/cost savings are estimated, use observed eligible operating hours,
  actual tariffs and the cost of replacement battery energy/losses. Do not
  extrapolate the nighttime 129 W baseline to 24 hours every day without evidence.

### Result and documentation promotion

Pending: setting readback; post-change observation periods; grid import/export
and battery-power comparison; effect on operating overhead; commissioning
decision. A lower grid reading alone does not prove improved efficiency.

If reduced residual import is verified without disrupting normal operation,
add Battery Saving disabled to the fixed commissioning section of
[house-battery-control.md](house-battery-control.md), link this evidence and
update its residual-import description with the measured result. Describe it
as a manually commissioned setting unless a separate implementation adds a
controller check. Do not claim the controller enforces it. If the effect is
absent or inconclusive, retain that outcome here instead of promoting the
setting as a proven fix.


## 25 September 2026: Export Calibration -30 W to 0 W

**Outcome: short-window physical import reduction observed; calibration left at
0 W.** Battery Saving remains disabled per Connor's report. Its independent
readback and a matched before/after test remain outstanding. Connor reported
that disabling it reduced import by roughly 100 W, leaving around 50 W;
this is consistent with the earlier 129 W median but is not a controlled
measurement of the setting's individual contribution.

At 23:20:25 BST (22:20:25 UTC), the authorized HA `number.set_value` service
changed `number.garage_inverter_control_export_calibration` from -30 W to 0 W.
The service completed and HA state read 0 W. No direct register readback was
performed. Physical utility-meter response supports an actual effect, rather
than relying only on optimistic HA state. No other inverter controls or
controller schedules were changed by this experiment.

The controller remained `IDLE` through the observation window. Recorded context
was Feed-In Priority, SOC 19%, no solar and EV standby around 2.4 W. The baseline
excludes the earlier restart/transition around 23:11 BST. The normal cheap-charge
boundary is 23:30 BST; all readings below precede that boundary.

| Utility-meter readings (BST) | Import W | Summary |
| --- | --- | --- |
| 23:12–23:20, nine updates | 55, 53, 47, 48, 53, 63, 55, 54, 48 | Median 53 W; mean 52.9 W |
| 23:21–23:23, three updates | 32, 30, 31 | Median/mean 31 W |

The first new reading fell 16 W (48 to 32 W); the window means differ by
21.9 W. Repeated polling of each unchanged minute reading is not counted as an
independent measurement. The result is close to, but does not yet meet, the
user's desired settled grid-power band of +/-25 W. That is an investigation
target, not a published manufacturer guarantee.

HA battery telemetry changed from -280 W to -305 W (25 W more DC discharge).
Connor separately observed an approximately 35 W increase. Solis meter telemetry
changed from -47 W import to 0 W, while the utility meter still showed 30–32 W
import. Device sample timestamps were 22:11:10.940 and 22:21:10.522 UTC; HA
received them at 22:12:14 and 22:21:30 respectively. These sparse, asynchronous
samples do not establish an exact power balance, meter offset or inverter loss.
The cloud AC-output entity reported 0 W during battery discharge and was not
used to calculate efficiency.

Evidence: [selected HA history and observations](solis-protocol-fixtures/export-calibration-2026-09-25.json).
The read-only observer finished at 23:23:55 BST. The API credential tempfile was
removed after evidence capture. No additional calibration adjustment or return
to -30 W was performed; there is no A/B/A repeat or long-duration validation.

Current manual operating settings are Battery Saving disabled (user reported)
and Export Calibration 0 W (HA confirmed, physical response observed). Retain
both for further observation. The controller neither checks nor enforces these
two settings. Remaining work is authoritative setting readback, longer matched
observations, gross import/export energy accounting and confirmation that cheap
charging and subsequent load following behave normally. Do not interpret the
import reduction as proof of reduced inverter overhead or extrapolate it into
annual savings without measured operating hours and battery energy costs.
