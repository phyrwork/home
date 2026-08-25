# T0045 — Lean reserve forecast

Status: Deployed and live-verified

Supersedes the ordinary-grid contribution and stored external-PV-surplus
assumptions in T0001, T0013, T0026 and T0028.

## Objective

Correct the dynamic reserve so it protects forecast household demand that
cannot be served by concurrent PV before the next trusted cheap opportunity.
The site deliberately exports surplus PV; the reserve model must never assume
that surplus will be stored for later use.

## Model

For each non-cheap interval:

```text
concurrent_deficit_kwh = max(load_kwh - solar_kwh, 0)
stored_requirement_kwh = concurrent_deficit_kwh / discharge_efficiency
```

Reverse planning adds that stored requirement to later requirements. A solar
surplus leaves the later requirement unchanged. Do not carry surplus between
intervals and do not subtract an assumed ordinary-grid contribution.

Preserve the existing next-cheap horizon, trusted tariff and forecast
validation, cheap-interval refill, charge/discharge capability checks, battery
capacity failure, configured reserve margin and absolute safety floor.

The exact total remains:

```text
reserve_target_kwh = safety_floor_kwh
                   + reserve_margin_kwh
                   + forecast_requirement_kwh
```

## Observability

Preserve:

- `sensor.house_battery_reserve_target`: total exact reserve;
- `sensor.house_battery_reserve_usable`: total reserve above the safety floor.

Add:

- entity ID `sensor.house_battery_reserve_forecast`;
- unique ID `house_battery_control_reserve_forecast`;
- friendly name `House Battery Reserve (Forecast)`;
- value `max(reserve_target - safety_floor - reserve_margin, 0)`;
- energy device class, kWh, two-decimal display precision and no state class;
- unavailable state when the snapshot or reserve target is unavailable.

Add `Reserve (Forecast)` immediately after `Reserve (Usable)` in the Power
dashboard's House Battery section. Dashboard configuration remains UI-managed.

## Acceptance

- Concurrent PV offsets load only within the same interval.
- Earlier surplus cannot reduce a later deficit.
- A non-cheap deficit receives no assumed grid-energy contribution.
- Cheap refill and all existing physical/safety bounds remain unchanged.
- Focused tests cover the corrected recurrence and sensor zero/unavailable
  behavior.
- The complete component and deployment suites pass.
- Live readback proves all three reserve diagnostics are available and ordered
  together on the dashboard.

## Deployment evidence — 2026-08-25

- Full Ansible deployment completed with `ok=140`, `changed=4`, no failures,
  configuration validation, and a Home Assistant restart.
- Fresh controller output reported total reserve `5.28382598494008 kWh`, usable
  reserve `2.06846598494008 kWh`, and forecast reserve
  `0.0684659849400784 kWh`.
- The Power dashboard was saved and read back with `Reserve`,
  `Reserve (Usable)`, `Reserve (Forecast)`, then `Surplus` in that order.
