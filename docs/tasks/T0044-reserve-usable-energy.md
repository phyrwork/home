# T0044 — Usable reserve energy sensor

Status: Complete

Add a diagnostic `House Battery Reserve (Usable)` energy sensor that reports
the exact modeled reserve target above the configured battery safety floor.
The value is clamped to zero at or below that floor and is unavailable when
the controller snapshot or reserve target is unavailable. It deliberately
does not use the quantized control reserve.

Deployed on 2026-08-25. Home Assistant reported `2.0 kWh` for an exact
`5.21536 kWh` reserve target and configured `3.21536 kWh` safety floor. The
Power dashboard's House Battery section shows the existing `Reserve` card and
the new `Reserve (Usable)` card together; dashboard configuration remains
UI-managed rather than repository-managed.
