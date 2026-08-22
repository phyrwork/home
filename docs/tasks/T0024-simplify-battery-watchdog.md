# T0024 — Simplify the independent battery watchdog

Status: Council approved

## Objective

Reduce the independent Home Assistant watchdog to the T0022 fail-safe contract:
assert the disable guard, disable every native charge/discharge slot, and select
Self-Use when the controller is unavailable or stale.

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

