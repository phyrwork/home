# T0046 — Stabilize active bonus-dispatch authority

Status: Implemented locally — deployment and live verification pending

Depends on: T0039

## Defect

At the start of an Intelligent Dispatch on 2026-08-25, the controller selected
`CHEAP_CHARGE` and created the expected 15-minute slot, but health remained
`DEGRADED`. Each reconciliation reported an important stop for slot 1 charge,
then recreated the slot with a start and end one minute later. Peak Shaving
therefore remained enabled while the controller repeatedly attempted the
charge handover.

The dispatch authority fingerprint used the evaluated component interval.
Evaluation clips an active interval's start to `now`, so that fingerprint
changed on every heartbeat even though the underlying import rate, export
rate, dispatch source, event and price were unchanged. The controller treated
each clipped start as withdrawn authority and correctly stopped its old lease.

## Minimal correction

Fingerprint the stable source intersection instead:

```text
start = max(import_rate.start, export_rate.start)
end   = min(import_rate.end, export_rate.end)
```

Retain the existing import price/source/event, dispatch-source identity,
source revision, export price and source interval fields. Do not weaken
withdrawal, reclassification, repricing, provenance, expiry, stop-debt or
15-minute lease rules. Do not add persistence, timers or new state.

The active bonus slot remains now-based when first issued. Its existing native
end and lease remain stable on ordinary heartbeats; expiry still stops it and
may issue a fresh bounded lease only after authoritative off proof.

## Acceptance

- Moving only the evaluated/clipped interval start does not change the bonus
  fingerprint.
- A change to either source intersection boundary still changes it.
- Dispatch-source identity, source event/revision and prices remain binding.
- An unchanged live bonus dispatch does not manufacture stop debt or repeatedly
  rewrite the slot.
- Existing T0039 expiry, withdrawal, retry and renewal tests continue to pass.
- The complete component and deployment suites pass.
- Live readback shows a fixed charge slot, Peak Shaving disabled after the
  handover, controller `HEALTHY`, and no pending stop operation during the
  unchanged dispatch.

## Local evidence

- The authority fingerprint now uses the original import/export intersection;
  the clipped interval remains only the active-component selector.
- Regression coverage proves a moving clipped start retains one fingerprint,
  a changed source boundary does not, and an unchanged heartbeat creates no
  stop debt.
- House-battery component suite: `144 passed`.
- Deployment suite: `53 passed`.
