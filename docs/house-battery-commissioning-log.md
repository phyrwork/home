# House battery commissioning log

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
| Grid Feed-in Power Limit can remain disabled | The DNO-approved export limit equals the site's installed inverter capacity, so no lower Solis software limit is required. The earlier export blocker was an enabled limiter set to 0 W / 0 A. | Commissioned |
| Legacy TOU bit is not the TOU-v2 activation mechanism | Direct CID 636 writes of `114` and `98` were accepted but normalized immediately to `112` and `96`, stripping bit 1. | Proven |
| A cross-midnight interval was not the only charge failure | `21:56-07:00` did not produce charging, and changing the same slot to the same-day interval `21:56-23:59` also did not produce charging. | Proven |
| Immediate HA control state is not device proof | HA and direct control reads showed slot 1 enabled with the requested current, target and time, while physical battery power remained slightly negative and whole-site demand reflected the EV only. | Proven |
| TOU-v2 uses discrete six-slot controls | The deployed Solis Cloud Control integration detects CID 6798 value `43605` and uses slot CIDs 5916-5987; it deliberately hides the legacy global Time Of Use switch. | Proven from deployed integration source |

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

Grid Peak Shaving is commissioned disabled and is not a runtime dependency.
Charge slots require it disabled; battery discharge/load following does not
require it. See the
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
- Grid Peak Shaving is disabled as a one-time commissioned setting and is not
  runtime-managed.
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
