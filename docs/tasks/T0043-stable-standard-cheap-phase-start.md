# T0043 — Stabilize standard-cheap current-phase starts

Status: Implemented locally — deployment and live acceptance pending

Depends on: T0038, T0039, T0042

## Fresh passive evidence

The pre-T0043 midnight observation found the old `24:00` text-entity
representation rejected through midnight. At `00:00` the controller recovered
with charge slot 1 at `00:00-05:30`. By `00:04`, fresh telemetry showed SOC
`18% → 19%`, battery charge power `5.014 kW`, and whole-site demand of
approximately `5.87 kW`.

This proves that the post-midnight native charge schedule became effective and
that physical charging occurred. It does not prove that the pre-midnight split
was prearmed continuously, because the first segment had already failed at
the `24:00` boundary and the later slot was recovered after midnight.

## Minimal decision

For a `STANDARD_CHEAP` charge intent, derive the current contiguous
same-class phase start from the validated source intervals when that history
is available, and round it up to an exact minute. This stabilizes retries
within the available source day. Fused source history may omit the prior day
at rollover, so the active Solis adapter bridges that boundary by reusing a
unique already-enabled allocated cheap slot whose owner, direction, values,
native end, and active half-open schedule match exactly.

`BONUS_DISPATCH` remains now-based and retains its existing native component
lease clipping. Full-SOC cycling remains independently now-based. Adjacent
mixed phases use the current component start; a bonus component never lends its
start or lease to a neighboring standard-cheap phase.

At a source-day rollover, controller retry generations omit only the rolling
standard text start while retaining the physical entity, native end, expiry,
current/target values, tariff source tokens, reserve/cycle/lease authority,
and all other change identity. End/value/entity changes and a
standard-to-bonus transition still create a new generation.

The exact-start bonus case where a `23:59` first segment is empty is explicitly
deferred. It is not broadened by this task; the one-minute `23:59` boundary and
its known gap remain governed by T0042.

## Files

- `deployment/files/custom_components/house_battery_control/planner.py`
- `deployment/files/custom_components/house_battery_control/controller.py`
- `deployment/files/custom_components/house_battery_control/solis.py`
- focused planner, Solis adapter, and controller regression tests
- commissioning log

## Local acceptance

Tests cover:

- minute-ceiled standard-cheap current-phase starts;
- one unchanged standard phase retaining `23:30-05:30` when planned at both
  `23:45` and `00:01`;
- adjacent standard components and mixed bonus/standard boundaries;
- adapter active-slot continuity and native `23:59`/`24:00` end regressions
  from T0038 and T0042;
- a prearmed `23:59` split remaining matched at `00:01`, while a genuinely
  inactive prior slot becomes the only conflict and an active second slot is
  reused;
- controller bonus-authority isolation;
- unchanged standard-plan start retry generation remaining stable through
  suppression;
- exact native end/value/entity mismatches resetting rollover reuse, and
  BONUS remaining positional;
- T0039 lease clipping and non-extension;
- DST-fold UTC ordering; and
- the complete house-battery component suite.

No Home Assistant, SSH, token, network, deployment, or live mutation access
was used for this card.

## Live acceptance remaining

After deployment, prove the first `23:59` segment behavior, the explicit
one-minute gap, and effective physical charging after rollover. It is
acceptable to stop a past slot 1 while an active slot 2 continues; acceptance
is physical continuity and absence of overlap, not retaining both native
slots. Use controller readback and fresh battery/site telemetry to distinguish
prearmed continuity from post-midnight recovery.
