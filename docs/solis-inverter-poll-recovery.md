# Solis Inverter v4.0.1 poll-recovery overlay

Deployment applies the locally reviewed `solis-sensor` commit `9cd1281`
(`Bound Solis updates and always reschedule polling`) over the pinned upstream
v4.0.1 component.

The upstream poller registered its next one-shot timer only after a successful
update. A stalled or exceptional portal request could therefore prevent all
future polling until Home Assistant restarted. The overlay:

- bounds one complete portal update to 60 seconds;
- always schedules the next retry, including after timeout or an empty inverter
  list;
- resets the in-memory portal session after timeout;
- replaces rather than accumulates pending timers; and
- cancels the pending timer during integration unload.

Focused lifecycle tests in the source checkout used to produce the overlay
cover timeout recovery, timer replacement, missing inverter lists, and shutdown
during an update. Remove the overlay when an upstream release contains an
equivalent fix and update the pinned version in `deployment/config.yaml`.
