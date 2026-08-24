# T0039 — Bound Intelligent bonus charging with native leases

Status: Implemented — local acceptance complete; live acceptance pending

Depends on: T0012, T0028, T0035, T0038

## Objective and evidence

Keep the EV Intelligent charge target at `100%`, but bound the battery's
exposure when Home Assistant or the Solis control path cannot react to a bonus
dispatch change.

On 2026-08-24, an active bonus charge slot remained physically effective while
the planner could not revalidate its inputs. The slot was bounded only by the
distant dispatch end. Later evidence showed the EV at effectively zero power
while Intelligent Dispatch remained on and the whole-house tariff remained
adjusted to `6.9p/kWh`; EV power is therefore neither charge authority nor a
withdrawal signal.

## Decisions

Define one named `BONUS_CHARGE_LEASE_DURATION = 15 minutes`. A newly created
`BONUS_DISPATCH` charge segment has the native half-open deadline:

`min(native bonus-component end, lease start + BONUS_CHARGE_LEASE_DURATION)`

The lease applies only to bonus charging. `STANDARD_CHEAP` charging, reserve
discharge and full-SOC cycling retain their existing native bounds.

A valid bonus authority requires:

- complete fused-rate coverage at `now` with actionable `BONUS_DISPATCH`
  classification;
- exact configured dispatch-source identity and direct source state `on`;
- valid event minimum, price and provenance fields;
- no future source revision; and
- import and export source freshness within their existing 26-hour limits.

The dispatch source is change-driven, not a periodic heartbeat. Remove the
10-minute `last_reported` age as a hard gate rather than increasing it. Retain
identity, structure, future-skew and rate-source checks. An unchanged
authoritative `on` state may renew until an explicit interval/source change;
the lease bounds controller/API reaction failure, not an upstream withdrawal
that is never reported.

EV power of zero is not withdrawal. Authoritative dispatch `off`, or bonus
interval withdrawal, reclassification, shortening or repricing, requests an
immediate important stop. Unknown, unavailable, malformed or
provenance-mismatched authority is recoverable `DEGRADED`: it permits no new
start or renewal while the existing native lease remains bounded.

## Lease lifecycle

- Clip each new bonus lease to the explicit deadline above.
- Lease ownership is intentionally not persisted across controller restarts. A
  fresh setup may derive a new bounded lease from the current external fused
  authority and eligibility; no restart-only ownership registry is required.
- Once active, preserve its exact observed physical key, owner, direction,
  current, target SOC and native end. Heartbeats must not move or extend it.
- Schedule an aware-UTC wakeup at its half-open expiry. Expiry must defeat the
  T0038 continuity exception and create the existing important stop debt.
- Stop and wait for authoritative off proof. Only then rebuild the plan and
  create a fresh lease when bonus authority remains valid.
- Renewal uses the existing stop, changed-fields-only programming and start
  paths. Do not add in-place mutation, a lease registry or a second writer.
- Preserve individual component boundaries when contiguous standard and bonus
  components form one economic window. Do not let either phase inherit the
  other's bound.
- Preserve UTC-instant ordering, local offsets/folds, split-midnight behavior,
  and both native `24:00` and `23:59` representations.

The fused template already dereferences the dispatch entity and its
`last_reported` value. Retain that dependency and prove that a dispatch-source
change updates the fused entity and wakes reconciliation; add no duplicate
poller or heartbeat.

## Acceptance

Tests must prove:

- a new bonus lease ends at the earlier of its component end and 15 minutes;
- three or more minute heartbeats neither write nor extend an active lease;
- expiry creates stop debt, authoritative off proof precedes any renewal, and
  renewal starts only with still-valid bonus authority;
- explicit withdrawal stops immediately; EV power zero does not stop;
- dispatch `last_reported` age alone is not a failure, while off,
  unknown/unavailable, identity mismatch, malformed provenance, future
  revision and stale rate sources retain the specified behavior;
- standard/bonus adjacency, midnight, DST folds/gaps, `24:00`/`23:59`, target
  SOC and physical-slot boundaries remain correct;
- dispatch-source change wakes reconciliation through the fused entity; and
- existing planner, controller, Solis, T0035 and T0038 suites pass.

Live acceptance must prove one 15-minute bonus lease remains fixed, stops at
its native end, renews only after off proof while the tariff remains bonus, and
stops without renewal after authoritative withdrawal.

Update the T0012 freshness contract and tests superseded by this card.

## Rollout

T0039 must be deployed before T0040, or both must be deployed atomically.
Deploying T0040 alone would remove the existing blanket backstop while long
unleased bonus schedules remain possible.

## Implementation evidence

- Added `BONUS_CHARGE_LEASE_DURATION = timedelta(minutes=15)` and clipped each
  new actionable bonus component to the earlier component end or lease end.
- Preserved the lease deadline across planner heartbeats and native continuity;
  expiry is included in the controller's aware-UTC wakeup candidates and creates
  the existing important stop debt.
- Removed dispatch `last_reported` age as a hard gate while retaining direct
  source identity/state, future-skew, fused provenance, event structure, and
  26-hour import/export freshness checks. Semantic dispatch/rate changes stop
  the active bonus lease; EV power is not used as withdrawal evidence.
- Added focused bonus lease planner coverage and updated the superseded T0012
  freshness contract/test.
- Adjacent bonus components retain separate native boundaries; all lease and
  authority comparisons use UTC instants, including a DST-fold regression.
- A successful stop with an enabled readback retains immediate stop debt and
  retries on the existing bounded backoff until off proof. The bonus
  fingerprint includes the exact dispatch-source entity identity. Lease
  ownership is intentionally in-memory only; after restart, a fresh bounded
  lease may be issued from valid current external authority and eligibility.
  When a valid bonus plan already matches an enabled physical slot, the
  controller reconstructs that ephemeral ownership from the observed slot and
  plan so later expiry or withdrawal still creates stop debt; no persistence or
  restart-only force-stop path is added.
- `PYTHONPATH=deployment/files /Users/connor/src/home/deployment/.venv/bin/pytest
  -q deployment/files/custom_components/house_battery_control/tests` — 109
  passed.
- `PYTHONPATH=deployment/files:deployment /Users/connor/src/home/deployment/.venv/bin/pytest
  -q deployment/tests` — 53 passed.

Live acceptance was not run in this implementation worktree, per the task
scope; deployment and on-device proof remain required before closing rollout.
