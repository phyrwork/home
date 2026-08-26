# T0048 — Static-only reserve refill authority

Status: Deployed and live-verified

Supersedes the reserve refill authority in T0045.

## Objective

Prevent a planned Intelligent Dispatch from lowering the household reserve.
Dispatch may end early when the EV stops charging, so its scheduled duration is
not a guaranteed battery-refill opportunity.

## Contract

```text
reserve_target = safety_floor
               + reserve_margin
               + forecast_requirement_until_next_static_cheap

reserve_balance = actual_battery_energy - reserve_target
```

- Only `STANDARD_CHEAP` ends the reserve horizon or reduces the reverse-planned
  forecast requirement.
- `BONUS_CHEAP` remains eligible for ordinary cheap charging and full-SOC
  cycling, but is treated as an ordinary interval by the reserve calculation.
- Energy physically stored during a dispatch affects the balance only through
  `actual_battery_energy`.
- Do not add planned dispatch credit, virtual energy, persistence or restart
  reconstruction.
- Preserve the existing safety floor, reserve margin, concurrent-PV treatment,
  power limits and tariff/forecast validation.

## Acceptance

- A future dispatch before the next static cheap period does not shorten the
  reserve horizon.
- A bonus interval cannot reset or reduce forecast reserve.
- An active static-cheap phase is skipped when finding the next static refill.
- Dispatch still selects cheap charging/cycling when otherwise eligible.
- Focused and complete house-battery tests pass.
- Live sensors show a reserve forecast based on the next static cheap period
  while planned dispatches remain present.

## Deployment evidence — 2026-08-26

- 145 house-battery tests and all 154 house-battery/fused-tariff tests passed.
- Home Assistant configuration validation passed before restart.
- The fused tariff producer and controller were deployed together with the
  clearer `BONUS_CHEAP` classification name; no compatibility alias remains.
- With an active `BONUS_CHEAP` window, the controller remained eligible for
  `CHEAP_CHARGE` but selected the next `STANDARD_CHEAP` window at 23:30 as the
  reserve refill authority.
- Live values were actual energy `7.073792 kWh`, forecast reserve
  `1.23083801169591 kWh`, total reserve `6.44619801169591 kWh`, and balance
  `0.627593988304094 kWh`.
- Solis telemetry recovered passively after restart and controller health
  returned to `HEALTHY` without intervention.
