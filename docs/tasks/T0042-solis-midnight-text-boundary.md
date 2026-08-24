# T0042 — Commission the Solis text-entity midnight boundary

Status: Implemented locally — `23:59` selected; deployment and physical acceptance pending

Depends on: T0026, T0029, T0034, T0038, T0041

## Fresh read-only evidence

At `2026-08-24 23:44–23:48` Europe/London, the controller was in
`CHEAP_CHARGE` and attempted charge-slot writes whose first split segment ended
as `23:xx-24:00`. The Solis text entity accepts hours `00–23`; the `24:00` end
was therefore rejected. Both charge slots remained off. This evidence is
control-plane failure evidence only and does not prove physical charging.

## Decision

Use `solis.midnight_end: "23:59"` in the deployed configuration. The resulting
`23:59–00:00` one-minute gap is explicitly accepted. The adapter continues to
model split logical intervals as two native segments and retains `24:00`
support for compatibility tests; schedule encoding, slot allocation and
reconciliation behavior are otherwise unchanged.

## Local acceptance

- The deployed YAML parses with `midnight_end == "23:59"`.
- Component tests continue to cover both `24:00` and `23:59` native boundaries.
- Component and deployment test suites pass.
- No Home Assistant, SSH, token, network or deployment access is used for this
  card.

## Remaining live acceptance

After deployment, prove the first `23:59` segment stops, retain the accepted
one-minute gap, and prove the next-day segment becomes effective without
charge/discharge overlap. The equivalent split-discharge exercise remains in
T0034.
