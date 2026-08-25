# T0044 — Usable reserve energy sensor

Status: Implemented locally — dashboard and deployment pending

Add a diagnostic `House Battery Reserve (Usable)` energy sensor that reports
the exact modeled reserve target above the configured battery safety floor.
The value is clamped to zero at or below that floor and is unavailable when
the controller snapshot or reserve target is unavailable. It deliberately
does not use the quantized control reserve.

Local implementation and focused validation are complete. No dashboard files
exist yet; the UI action and deployment remain pending.
