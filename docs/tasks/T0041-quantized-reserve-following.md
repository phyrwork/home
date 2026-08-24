# T0041 — Quantized reserve following and slot handover

Status: Approved — implementation pending

Depends on: T0001, T0003, T0026, T0030, T0039, T0040

This card supersedes T0001/T0026/T0029/T0030 statements that Grid Peak Shaving
is commissioned off, unmanaged, or excluded from normal runtime entities. The
existing fail-safe, shutdown fallback, and crash sentinel still never write
Peak Shaving. The only newly owned normal-runtime control is
`switch.garage_inverter_control_grid_peak_shaving`; its power-limit setting
remains manually commissioned and unmanaged.

## Objective

Make reserve decisions in the same integer-SOC domain that Solis accepts, and
hand the battery directly between forced slots and normal house-load following.

Normal load following is a real controller action, `RESERVE_FOLLOW`, not idle:

- Storage Mode remains Feed-In Priority;
- Battery Reserve remains enabled at the dynamic reserve SOC;
- Grid Peak Shaving is enabled; and
- no charge or discharge slot is enabled.

Grid Peak Shaving's maximum grid-power value remains manually commissioned at
0.1 kW. Runtime owns only the enable switch and must never write the limit.

## Defect and live evidence

The planner produced an exact reserve target of `5.4649418 kWh`. For the
32.1536 kWh battery, 17% is `5.466112 kWh`. With displayed SOC at 18%, the
controller repeatedly armed a 100 A discharge slot targeted at 17% and reported
`RESERVE_DISCHARGE`, while fresh battery power remained approximately 0 W and
the house imported from the grid.

The controller compared estimated battery energy with the exact model result,
but the inverter could act only on the upward-rounded integer-SOC target. The
approximately 1.17 Wh exact difference was not a physically actionable reserve
surplus.

The preceding transition also proved that stopping the forced discharge slot
while Grid Peak Shaving was off left the battery idle rather than load-following.

## Canonical reserve control domain

Keep two deliberately distinct reserve values:

1. `exact_reserve_energy_kwh`: the reverse-planned model result, including the
   10% safety floor and configured reserve margin. It is diagnostic only.
2. `control_reserve_soc_percent` and `control_reserve_energy_kwh`: the
   upward-quantized native target and its energy equivalent. These drive all
   control decisions.

Derive the control target by converting exact reserve energy to SOC and choosing
the smallest supported percentage at or above it that is representable by both
the Battery Reserve SOC capability and every configured reserve-export slot
target capability. Clamp to the safety floor and the common capability range;
report planning unavailable if no common value exists. Convert the resulting
SOC back to energy. This common quantizer is the sole reserve control domain;
the adapter must not independently round it to different values.

Native target writes, reserve-export eligibility and target-completion checks
must all use this quantized target. Preserve the existing reserve target and
balance sensor values as the exact model result requested for observability.
Expose the quantized control target and balance as attributes on those existing
sensors; do not add entities. In the model, retain `reserve_energy_kwh` and
`reserve_balance_kwh` as exact values and add explicitly named
`control_reserve_energy_kwh` and `control_reserve_balance_kwh` values.

Use a stateless boundary:

- start reserve export only when reported SOC is strictly above the quantized
  control target;
- stop it at or below that target; and
- keep the independent 10% safety stop stronger than either rule.

Do not add durable hysteresis, restart state, or an assumed extra percentage
point. Live verification may justify a separate follow-up change if integer SOC
chatter is actually observed.

For the captured example, the control target is 17% / 5.466112 kWh. The exact
1.17 Wh difference must not independently create a slot or a positive control
balance.

## Runtime states

The desired physical states are:

| Action | Peak Shaving | Native slot |
|---|---:|---|
| `RESERVE_FOLLOW` | on | none |
| `RESERVE_DISCHARGE` / `CYCLE_DISCHARGE` | off | bounded discharge slot |
| `CHEAP_CHARGE` | off | bounded charge slot |
| `FAIL_SAFE` / shutdown | irrelevant after Self-Use proof | existing important slot stops continue |

All state is reconstructed from current inputs and authoritative Solis control
readback. Do not add a transition journal, helper entities, a second writer, or
new guard/watchdog machinery.

Add the Grid Peak Shaving switch to the deployment mapping and existing Solis
state, revision/CAS boundary, managed-entity set and serialized adapter lock.
Keep its readback independent of `SolisPersistentState`: an unknown Peak Shaving
state may degrade ordinary planning/starts but must not hide an otherwise valid
Storage Mode readback or prevent fail-safe/shutdown from proving Self-Use.
Unknown or unavailable Peak Shaving blocks ordinary starts; it is never assumed
on or off. One reconciliation pass performs at most one control write and
verifies it by reread.

The manually commissioned 0.1 kW value is an installation invariant documented
and live-verified at commissioning time. Do not add runtime discovery or
validation machinery for a setting not exposed by the integration.

## Stateless make-before-break handovers

The controller derives the next single write from current desired action and
observed Peak Shaving/slot state. A restart therefore resumes the same handover
without stored transition state.

### Forced slot to reserve following

1. Enable Grid Peak Shaving and prove it on.
2. Stop the active slot and prove it off.
3. Report `RESERVE_FOLLOW` only when both conditions are authoritative.

Every forced-slot exit is important because its price/lease window ended, its
target was reached, or a safety rule fired. Create stop debt immediately. The
debt may record one in-memory, best-effort Peak-Shaving-on handover attempt:

1. if the switch is already proved on, stop immediately;
2. if the switch is authoritatively off with a valid revision, attempt one
   bounded Peak-Shaving-on write and reread;
3. if the switch is unknown/unavailable, skip the write and stop on the next due
   pass without treating it as on or off;
4. whether an attempted write fails, times out, or remains unknown, the next due
   pass stops the slot without another Peak Shaving prerequisite; and
5. the absolute minimum-SOC stop and restart-created stop debt bypass even this
   one attempt.

This preserves one-write-per-pass and normally avoids an import gap, while a
failed Peak Shaving write can delay an important stop by at most one bounded
service attempt. The existing `StopDebt` may gain one boolean recording this
ephemeral attempt; add no separate handover object or persistent state.

This single pre-stop write is the sole exception to T0030's generic rule that
known stop debt is serviced before an ordinary start/control write. The debt
already exists and remains highest priority; after the one bounded attempt the
controller services it directly. No other start or policy reconciliation may
overtake stop debt.

### Reserve following to a forced slot

1. Keep Grid Peak Shaving on.
2. Program changed slot fields while the slot is disabled.
3. Enable and prove the bounded slot.
4. Disable Grid Peak Shaving and prove it off.
5. Report the forced action only after the handover completes.

If programming or enabling fails, remain in `RESERVE_FOLLOW`. If disabling Peak
Shaving fails after the slot is armed, leave the bounded slot intact and retry
only the Peak Shaving handover while the intent remains eligible. Peak Shaving
suppresses charge, so control-plane arming alone is not proof of physical charge.
The existing slot lease and important-stop rules must stop the slot when its
price window, lease or target ends, even if the handover never completed.

### Direct forced-direction change

1. Enable and prove Grid Peak Shaving.
2. Stop and prove the old slot off.
3. Program and enable the new bounded slot.
4. Disable and prove Grid Peak Shaving.

Important-stop priority remains stronger than this ordinary sequence.

The old direction first creates stop debt and receives the same single
best-effort Peak-Shaving-on attempt. Once it is proved off, the new direction
uses the normal forced-slot entry sequence.

## Restart and unavailable-input reconstruction

Before planning, inspect every authoritative enabled configured direction. Its
native local schedule is a physical bound available without tariff, forecast,
telemetry, Peak Shaving, or remembered ownership. When authoritative
inverter-local time is also available and the current minute lies outside that
half-open schedule, create important stop debt. An unknown enable, schedule, or
inverter clock is reread and never written speculatively; already-known stop
debt remains active while any of those inputs is unavailable.

An enabled slot still inside its native bound is preserved until valid current
inputs can prove it conflicting, target-complete, lease-invalid or otherwise
unwanted. Do not persist or resurrect the previous desired intent. This scan is
also the restart reconstruction for memory-only expiry state.

## Failure, fail-safe and shutdown

- A failed Peak Shaving handover is `DEGRADED`; it does not introduce a new
  fail-safe trigger.
- Existing bounded start retries, scoped T0040 escalation, and unbounded
  important-stop retries remain unchanged.
- Target, safety, native-schedule and lease stops are important even when Peak
  Shaving, tariff, forecast or telemetry inputs are unknown or unavailable.
- Existing fail-safe and shutdown continue to select and prove Self-Use and to
  reconcile observed enabled slots off. Self-Use is the physical fallback;
  Peak Shaving need not be normalized during fallback.
- The crash sentinel remains mode-only and must not become a second Peak Shaving
  or slot writer.

## Required local tests

### Planner

- Preserve `5.4649418 kWh` as the exact target while deriving 17% /
  `5.466112 kWh` as the control target.
- Prove every reserve eligibility, completion and native target comparison uses
  the quantized domain.
- At reported SOC 17% with target 17%, plan `RESERVE_FOLLOW` and no discharge
  slot; above 17%, permit reserve export.
- Prove a raw difference smaller than one SOC step cannot create a phantom
  reserve action.
- Preserve the independent 10% safety floor.
- Distinguish exact and control targets/balances in diagnostics without adding
  helper entities.
- Prove the common quantizer across Battery Reserve SOC and every configured
  reserve-export target capability, including incompatible-capability failure.

### Solis adapter and controller

- Read/write only the Peak Shaving enable switch; never write the 0.1 kW limit.
- Add Peak Shaving to revision checks, the managed set and serialized writes.
- Each forced-slot exit creates stop debt immediately, attempts Peak Shaving on
  at most once, then stops even when that handover fails; minimum SOC bypasses
  the attempt.
- Forced-slot entry proves the slot armed before Peak Shaving is turned off.
- Peak Shaving-off failure leaves an eligible bounded slot armed and retries
  only the handover.
- Unknown Peak Shaving state blocks ordinary starts.
- Direct direction changes follow the four-phase order and never overlap slots.
- Restart at every handover phase reconstructs the next action from live state.
- Restart or input loss stops an enabled direction outside its authoritative
  half-open native schedule without relying on remembered ownership/expiry.
- CAS conflicts, write timeouts and provisional readback preserve single-writer
  behavior.
- Existing planner, overlap, retry, fail-safe, shutdown, bonus-lease and
  deployment suites remain green.

## Live acceptance after deployment approval

1. At the quantized reserve boundary, prove no discharge slot remains enabled,
   Peak Shaving is on, Feed-In Priority and Battery Reserve remain correct, and
   battery power follows house demand for at least five minutes with grid import
   no greater than the commissioned behavior.
2. Above the reserve boundary, prove the make-before-break export entry and
   physical full-rate discharge/export with no slot overlap.
3. On return to the boundary, prove Peak Shaving becomes effective before the
   slot stop, followed by uninterrupted load following.
4. During a trusted cheap interval, prove the charge slot is armed under Peak
   Shaving, Peak Shaving then turns off, and physical charging begins.
5. Restart once during an incomplete handover and prove stateless recovery from
   authoritative control state.

Control readback is provisional. Every physical acceptance uses fresh battery
power, whole-site import/export and, where practical, SOC movement.
