# T0040 — Scope fail-safe escalation and heartbeat handling

Status: Deployed — baseline accepted; fault-path live acceptance pending

Depends on: T0026, T0030, T0039

## Objective and evidence

Make planning/input degradation passively recoverable while retaining Self-Use
fail-safe protection for genuine controller and stop-confirmation failures.

On 2026-08-24, a change-driven dispatch timestamp exceeded the old 10-minute
age rule while the dispatch and `6.9p/kWh` tariff remained valid. The generic
15-minute degraded timer latched `FAIL_SAFE`, selected Self-Use and later
prevented recovery after inputs refreshed. The controller remained alive and
all native stops completed; this was not a fail-safe condition.

## Required behavior

| Condition | Outcome |
|---|---|
| Dispatch, tariff, planning or telemetry unavailable/stale/invalid | Recoverable `DEGRADED`; suppress starts and bonus-lease renewals |
| Generic prolonged `DEGRADED` | Remain `DEGRADED`; recover automatically |
| Controller heartbeat | Retain the one-minute event/backstop publication |
| Heartbeat stale for three minutes | Existing independent sentinel selects Self-Use |
| Inverter telemetry older than 30 minutes | Recoverable `DEGRADED` |
| Import/export tariff source older than 26 hours | Recoverable `DEGRADED` |
| Hard controller invariant | Immediate latched `FAIL_SAFE` and Self-Use |
| Important stop unconfirmed for 15 minutes | Latched `FAIL_SAFE` and Self-Use while stop retries continue |
| Orderly shutdown | Self-Use, then disable observed-on directions with existing strict retry behavior |

Remove the blanket `DEGRADED_FAILSAFE_TIMEOUT`. Do not add a guard, helper,
watchdog, polling loop or second writer.

## Important-stop deadline

Define `IMPORTANT_STOP_FAILSAFE_TIMEOUT = 15 minutes`.

Each distinct stop debt carries one monotonic `first_seen` or equivalent fixed
deadline. Retries, heartbeats, source events, strategy changes and provisional
state revisions must not reset or extend it. Clear it only after authoritative
off proof. When it expires, latch `FAIL_SAFE`, select Self-Use and continue the
existing stop retries. The latch clears only on fresh integration setup/Core
restart, retaining current hard-failure semantics.

Input unavailability by itself never creates or resets stop debt. T0039 lease
expiry and other already-defined intent/SOC/conflict boundaries retain their
important-stop behavior.

## Retained liveness and shutdown behavior

- Keep the one-minute event-driven reconciliation backstop and heartbeat.
- Keep the independent three-minute stale-heartbeat sentinel. It performs only
  its existing mode-only Self-Use action and does not add controller state or
  race the live writer.
- Keep the 30-minute inverter measurement-age check and 26-hour rate-source
  checks; their failure is recoverable degradation.
- Preserve shutdown heartbeat refresh, Self-Use-first ordering, stopping only
  observed-on directions, rereading unknown directions without speculative
  writes, and strict retry until proof or HA termination.

## Acceptance

Tests must prove:

- planning or input degradation lasting beyond 15 minutes remains `DEGRADED`
  and recovers automatically without restart or Self-Use;
- stale telemetry and tariff sources suppress starts but do not latch;
- a stop proved before its deadline clears the debt and never latches;
- retries/events cannot renew a stop deadline;
- an unproved stop at 15 minutes latches `FAIL_SAFE`, selects Self-Use and keeps
  retrying;
- a later distinct debt receives a new deadline;
- hard-invariant, sentinel and shutdown behavior remains unchanged; and
- the complete controller/component suites pass.

Live acceptance must reproduce a prolonged recoverable planning outage without
Self-Use, prove passive recovery, and separately prove one bounded unresolved
important stop escalates as specified.

## Rollout

Do not deploy T0040 without T0039. The two may be locally implemented and
deployed atomically after both review councils approve.

## Implementation evidence

- Removed the blanket `DEGRADED_FAILSAFE_TIMEOUT` escalation and wakeup.
  Ordinary planning, tariff, telemetry and other input degradation remains
  recoverable; healthy input recovery clears the degraded diagnostic state
  without selecting Self-Use.
- Added `IMPORTANT_STOP_FAILSAFE_TIMEOUT = 15 minutes`. Each in-memory
  `StopDebt` captures immutable monotonic `first_seen` and `fail_safe_deadline`;
  retry attempts, events, heartbeats and provisional revisions preserve both.
  Debt is retired only after authoritative off proof. Expiry latches the
  existing fail-safe path while the existing unbounded stop retry continues.
- Preserved the one-minute reconciliation/heartbeat backstop, independent
  three-minute sentinel, 30-minute telemetry and 26-hour tariff checks, hard
  invariant latch, and strict shutdown ordering and retry behavior.
- Added focused controller timeline coverage for prolonged recoverable
  degradation, proof before deadline, non-renewing retries/events, deadline
  expiry and retry continuation, and distinct stop debt deadlines.
- Local gates: component tests and deployment tests pass. No 1Password, network,
  SSH, live Home Assistant or deployment access was used.

Live acceptance remains required before closing rollout: reproduce prolonged
recoverable planning degradation and passive recovery without Self-Use, then
separately prove one bounded unresolved important stop escalates as specified.

## Deployment evidence — 2026-08-24

- T0039 and T0040 were deployed atomically after all 113 component tests and 53
  deployment tests passed on integrated `main`.
- The full deployment passed Home Assistant configuration validation and
  restarted Core successfully. On fresh setup the prior fail-safe latch was
  cleared, telemetry recovered, temporary reconciliation degradation cleared,
  and the controller reached `HEALTHY` with no `fail_safe_since` value.
- The one-minute heartbeat advanced normally, Feed-In Priority was restored,
  all unrelated slot directions remained off, and the selected reserve action
  produced the expected approximately 4.918 kW physical discharge.
- No house-battery-controller error or traceback appeared in recent Core logs.

This proves deployment, fresh-setup recovery and ordinary reconciliation. It
does not deliberately manufacture a prolonged planning outage or failed Solis
stop; those fault-path live acceptances remain pending natural or separately
approved bounded evidence.
