# T0041 — Quantized reserve following and slot handover

Status: Implemented locally — focused validation complete; live acceptance remains deployment-gated

Superseded in part by T0049: the dynamic quantized reserve remains the forced
discharge-slot target, but the global Battery Reserve SOC is the fixed 10%
safety floor so ordinary load following can consume the planned reserve.

Local evidence: planner, Solis adapter/controller, sensor, configuration, and
deployment mapping changes are implemented on
`codex/t0041-quantized-reserve-following`. The focused integration suite passes
(133 tests) and the deployment suite passes (53 tests) with frozen local
dependencies and compiled component sources. Tests remain local; no live Home
Assistant, SSH, token, or deployment access is used for this card.

Depends on: T0001, T0003, T0026, T0030, T0039, T0040

This card supersedes T0001/T0026/T0029/T0030 statements that Grid Peak Shaving
is commissioned off, unmanaged, or excluded from normal runtime entities. The
existing fail-safe, shutdown fallback, and crash sentinel still never write
Peak Shaving. The only newly owned normal-runtime control is
`switch.garage_inverter_control_grid_peak_shaving`; its power-limit setting
remains manually commissioned and unmanaged.

## Objective

Make reserve decisions in the same native SOC capability domain that Solis
accepts, and hand the battery directly between forced slots and normal
house-load following.

Normal load following is a real controller action, `RESERVE_FOLLOW`, not idle:

- Storage Mode remains Feed-In Priority;
- Battery Reserve remains enabled at the fixed safety-floor SOC;
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
but the inverter could act only on the upward-rounded native capability target.
The approximately 1.17 Wh exact difference was not a physically actionable
reserve surplus.

The preceding transition also proved that stopping the forced discharge slot
while Grid Peak Shaving was off left the battery idle rather than load-following.

## Canonical reserve control domain

Keep two deliberately distinct reserve values:

1. `exact_reserve_energy_kwh`: the reverse-planned model result, including the
   10% safety floor and configured reserve margin. It is diagnostic only.
2. `control_reserve_soc_percent` and `control_reserve_energy_kwh`: the
   upward-quantized native target and its energy equivalent. These drive all
   reserve-export and cycle-discharge slot decisions.

T0049 adds a third, independent native value: the global Battery Reserve SOC is
the fixed configured safety floor. It is not the dynamic model reserve and is
not part of the common discharge-slot target capability domain.

Derive the control target by converting exact reserve energy to SOC and choosing
the smallest supported percentage at or above it that is representable by
every configured reserve-export and full-SOC-cycle discharge slot target
capability. Clamp to the safety floor and
the common capability range;
report planning unavailable if no common value exists. Convert the resulting
SOC back to energy. This common quantizer is the sole reserve control domain;
the adapter must not independently round it to different values.

Native target writes, reserve-export eligibility and target-completion checks
must all use this quantized target. Preserve the existing reserve target and
balance sensor values as the exact model result requested for observability.
Expose the quantized control target and balance as attributes on those existing
sensors; do not add entities for T0041's quantized-reserve diagnostics. In the
model, retain `reserve_energy_kwh` and
`reserve_balance_kwh` as exact values and add explicitly named
`control_reserve_energy_kwh` and `control_reserve_balance_kwh` values.

Use a stateless boundary:

- Solis reports integer SOC, so define a one-percent uncertainty band;
- start or continue reserve export only when reported SOC is strictly above
  the quantized control target plus that one-percent band;
- stop it at or below that boundary; and
- keep the independent 10% safety stop stronger than either rule.

Do not add durable hysteresis, restart state, or any further uncertainty beyond
this one-percent reporting band. Live verification may justify a separate
follow-up change if integer SOC telemetry chatter is actually observed.

For the captured example, the control target is 17% / 5.466112 kWh. Reported
SOC 18% follows reserve and reported SOC 19% permits export. The exact 1.17 Wh
difference must not independently create a slot or any export action.

## Runtime states

The desired physical states are:

| Action | Peak Shaving | Native slot |
|---|---:|---|
| `IDLE` (cheap window with no charge/cycle action) | off | none |
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
5. only the absolute minimum-SOC stop bypasses this one attempt.

This preserves one-write-per-pass and normally avoids an import gap, while a
failed Peak Shaving write can delay an important stop by at most one bounded
service attempt. The existing `StopDebt` records one boolean for this ephemeral
attempt, at most once for the active debt during one controller lifetime. A
restart may repeat it; add no restart marker, separate handover object or
persistent state.

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
date-less recurring native local schedule is only a physical bound on the
programmed inverter direction. It cannot identify which occurrence was
pre-armed after a restart, so the controller must not infer an occurrence
expiry or create restart-only stop debt from the current minute. The inverter
clock remains part of the persistent state read; unknown inputs are reread and
never written speculatively;
already-known stop debt remains active while any of those inputs is unavailable.

An enabled slot still inside its native bound is preserved until valid current
inputs can prove it conflicting, target-complete, lease-invalid or otherwise
unwanted. Explicit UTC `_owned_expiry` and bonus-lease deadlines continue to
drive their own stop obligations, while target and minimum-SOC safety stops and
valid-plan conflict stops remain authoritative. Do not persist or resurrect the
previous desired intent, and do not add a restart-only force-stop. Restart
reconciliation may reconstruct an ephemeral lease only from a valid matching
plan and live slot state.

## Failure, fail-safe and shutdown

- A failed Peak Shaving handover is `DEGRADED`; it does not introduce a new
  fail-safe trigger.
- Existing bounded start retries, scoped T0040 escalation, and unbounded
  important-stop retries remain unchanged.
- Target, safety and lease stops are important even when Peak Shaving, tariff,
  forecast or telemetry inputs are unknown or unavailable. A recurring native
  schedule remains a physical inverter bound, not an inferred occurrence
  expiry.
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
- With target 17%, reported SOC 18% plans `RESERVE_FOLLOW` and no discharge
  slot; reported SOC 19% permits reserve export.
- Prove a raw difference smaller than one SOC step cannot create a phantom
  reserve action.
- Preserve the independent 10% safety floor.
- Distinguish exact and control targets/balances in diagnostics without adding
  helper entities.
- Prove the common quantizer across every configured reserve-export and
  full-SOC-cycle discharge target capability, including
  incompatible-capability failure and no adapter re-round mismatch.

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
- The pure full-sequence adapter test is restart-at-each-phase evidence: each
  next action is reconstructed from live state without transition memory.
- Restart and input loss do not infer occurrence expiry from a date-less
  recurring native schedule; explicit UTC ownership/lease, target, minimum-SOC
  and valid-plan conflict evidence remain authoritative.
- CAS conflicts, write timeouts and provisional readback preserve single-writer
  behavior.
- Existing planner, overlap, retry, fail-safe, shutdown, bonus-lease and
  deployment suites remain green.

## Live acceptance after deployment approval

1. At the quantized reserve boundary, prove no discharge slot remains enabled,
   Peak Shaving is on, Feed-In Priority remains correct, Battery Reserve is on
   at the fixed safety floor, and
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
