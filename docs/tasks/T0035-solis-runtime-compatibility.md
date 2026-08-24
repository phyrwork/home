# T0035 — Solis runtime compatibility fixes

Status: Implemented locally — deployment and live verification pending

Depends on: T0026, T0029, T0030

## Problem and evidence

Live evidence showed the controller reporting `SERVICE_TIMEOUT` every ten
seconds while stopping a slot. Home Assistant optimistically changed the switch
to `off`, but the blocking Solis Cloud Control service remained in its internal
retry policy. Cancelling that service after ten seconds made the stop ambiguous,
so the controller correctly retained stop debt and repeatedly forced proof.

Read-only SSH inspection of the deployed source identified
`mkuthan/solis-cloud-control@v2.21.0`. Its control call has:

- a 30-second `max_retry_time`;
- a 30-second timeout for each HTTP request;
- a retry check before starting the next request, so a final request may extend
  total blocking duration toward 60 seconds;
- optimistic coordinator state before the API call and a deferred refresh after
  it; and
- no cancellation suppression: `CancelledError` propagates through its retry
  loop.

Independent live evidence also showed reserve export healthy at 08:08 UTC and
stopped at 08:09 UTC. The planner moved the reserve intent start from the
previous minute to `_minute_floor(now)` each pass. Strict native schedule
comparison therefore treated the otherwise unchanged active slot as a conflict.

## Minimal decisions

1. Use one 80-second controller write deadline. Remove the redundant adapter
   ten-second service timeout; the same deadline bounds lock, one blocking
   service call and readback. This covers the pinned integration's approximately
   60-second worst service path plus the 15-second readback allowance and stays
   below the three-minute stale-heartbeat crash sentinel.
2. Preserve the existing single adapter lock and in-flight service reference.
   Outer cancellation still cancels the service task; a cancellation-resistant
   task blocks a second writer. An actual bounded failure remains ambiguous and
   important-stop debt retries indefinitely with its existing capped backoff.
   Immediately before awaiting an important stop, publish a degraded heartbeat
   with the exact stop, attempt and retry context; an 80-second call must never
   leave the previous healthy snapshot visible.
3. A forced stop whose precondition is already `off` is `APPLIED` after the
   blocking `turn_off` completes and the live state is still exactly `off`, even
   if Home Assistant emits no new revision for the idempotent call. Do not apply
   this proof to starts, non-switch writes or a state other than exact `off`.
4. Treat a shifted start as continuous only for an enabled configured
   `RESERVE_EXPORT` discharge on the same physical slot when current, target and
   end still match and inverter-local now is inside both half-open schedules.
   End, target, current, owner, slot or direction changes remain conflicts and
   produce the normal stop obligation.

No new writer, retry layer, background task, state store or abstraction is
introduced.

## Files

- `deployment/files/custom_components/house_battery_control/controller.py`
- `deployment/files/custom_components/house_battery_control/solis.py`
- focused controller/Solis tests

## Local acceptance

Tests must prove:

- the controller bound covers 60 seconds of pinned service behavior plus 15
  seconds of readback while remaining below the sentinel threshold;
- a service completing beyond the old ten-second cutoff succeeds without real
  test sleeping and uses one HA service call;
- a degraded stop-in-progress heartbeat and exact operation context are
  published before the blocking service is awaited;
- a hung or cancellation-resistant service remains bounded and never overlaps
  a forced retry;
- ambiguous optimistic `off` followed by a completed idempotent forced `off`
  supplies proof and lets controller stop debt clear;
- reserve discharge remains matched across a one-minute start shift; and
- before the desired start, at/after the observed half-open end, or after a
  changed current, end or target, the active slot still projects as a conflict.

## Live acceptance

After deployment:

1. stop an observed-on slot during a naturally retrying Solis control call;
2. verify there is one controller write in flight, no ten-second
   cancel/retry loop, truthful degraded heartbeat during the bounded call, and
   stop debt clears after completed exact-off proof;
3. allow reserve discharge across at least two minute boundaries and verify the
   same slot remains enabled, controller health returns/stays healthy and
   physical export continues; and
4. verify reaching reserve or the half-open end still creates and completes the
   important stop.

## Local evidence

- 23 focused Solis adapter tests passed.
- All 97 house-battery component tests passed.
- No live state, deployment, Home Assistant service or Solis control was
  mutated; SSH use was limited to reading the deployed integration source.
