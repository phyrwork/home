# T0036 — Revert Solis poll-recovery overlay for A/B commissioning

Status: Implemented locally — deployment and live A/B verification pending

Depends on: T0035

## Decision

Return the Solis Inverter integration to the pristine pinned upstream release
`hultenvp/solis-sensor@v4.0.1`. Do not install the local poll-recovery overlay.

This is a controlled commissioning baseline, not a claim that the overlay is
the cause of the current telemetry failure. The current HA evidence shows
repeated Solis login/discovery failures (`No valid inverters found`, `No
inverters found`) before the overlay's normal polling path can run. The
overlay's scheduler does reschedule after those failures, so an A/B comparison
against pristine v4.0.1 is the shortest way to determine whether the upstream
change contributed to the observed behaviour.

## Change

- Remove `component_overlay_dir` from the Solis Inverter deployment entry.
- Remove the overlay implementation, its lifecycle smoke tests, and its
  standalone documentation.
- Keep the upstream version pinned at `v4.0.1`.
- Set the generic role's source-marker revision to `pristine-v4.0.1` for this
  component. This forces one clean reinstall when the old overlay installation
  still has the same upstream version marker; subsequent runs use the same
  marker and remain idempotent.
- Keep this card as the record of the rollback and its verification result.

No Home Assistant or Solis Cloud change is part of this task. Deployment and
live verification are separate, explicit steps.

## Acceptance

1. Focused deployment/config tests pass.
2. The rendered deployment installs Solis Inverter v4.0.1 without copying any
   overlay files.
3. After deployment and restart, compare telemetry availability, login/
   discovery errors, update freshness, and controller recovery with the
   overlay baseline.
4. Record the A/B result here before deciding whether to reintroduce or replace
   polling changes.

## Local verification

- `deployment/tests/test_solis_component_config.py` passes.
- No deployment or live Home Assistant mutation has been performed by this
  task.
