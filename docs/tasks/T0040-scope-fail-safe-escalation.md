# T0040 — Scope fail-safe escalation and heartbeat handling

Status: Approved — implementation pending

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
