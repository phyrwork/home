# T0024 — Simplify the independent battery watchdog

Status: Superseded by T0026/T0030

Historical implementation evidence below is retained. The broad watchdog,
guard and script were replaced by the mode-only stale-heartbeat sentinel.

## Objective

Reduce the independent Home Assistant watchdog to the T0022 fail-safe contract:
assert the disable guard, disable every native charge/discharge slot, and select
Self-Use when the controller is unavailable or stale.

Grid Peak Shaving is commissioned disabled directly in SolisCloud. It is not
part of the watchdog's runtime writes or safe-state proof. Charge slots require
it disabled, while battery discharge/load following does not require it.

## Scope

- Retain heartbeat/startup/shutdown triggering and bounded retries.
- Retain readback checks needed to establish Self-Use and all-slots-off.
- Remove commissioning authority, candidate evidence, legacy command, and
  uncommissioned diagnostics assumptions.
- Keep the watchdog independent from the custom integration runtime.
- Add or update focused YAML/rendering tests.

## Boundaries

- Own only watchdog automation/script files and their tests.
- Do not edit the Python coordinator, strategy, config, reader, or actuator.
- Do not enable dynamic control.

## Completion criteria

- The watchdog applies the same fail-safe state as T0022.
- The focused tests pass.
- The resulting YAML is materially smaller and contains no commissioning
  lifecycle machinery.
