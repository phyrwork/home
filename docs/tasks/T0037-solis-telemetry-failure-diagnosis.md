# T0037 — Diagnose Solis telemetry availability

Status: Investigation complete — further changes deferred

Depends on: T0036

## Observed failure

The Solis Inverter telemetry integration can remain loaded while all inverter
entities are restored and unavailable. Its log message is usually `No inverters
found` or `No valid inverters found`.

During the 2026-08-24 incident:

- both the local poll-recovery overlay and pristine upstream v4.0.1 failed in
  login/discovery;
- Solis Cloud Control independently received timeouts, device-offline errors
  and repeated HTTP 502 responses from SolisCloud;
- the controller remained alive with a fresh heartbeat, `DEGRADED` health and
  no writes while its required telemetry was unavailable.

## Diagnosis

The removed polling overlay was not causal. Its changes applied only to the
normal update loop after discovery; login, inverter-list discovery and response
parsing were unchanged from v4.0.1.

The strongest supported cause is intermittent SolisCloud API failure. The
telemetry client makes several sequential requests during discovery and has no
per-request retry. It also collapses network errors, non-200 responses, Solis
error responses, malformed data and a genuinely empty inverter list into the
same vague empty-list outcome. This makes a cloud failure look like missing
configuration.

The control integration is not an independent telemetry fallback: it uses the
same SolisCloud service and exhibited the same incident concurrently.

Pristine v4.0.1 also leaks failed-discovery retry callbacks across manual
integration reloads. `schedule_discovery()` discards the unsubscribe handle,
and shutdown neither cancels the callback nor prevents the old service from
rescheduling itself. Repeated reloads therefore create overlapping login loops;
the observed staggered failures only seconds apart were consistent with several
retained retry chains. The removed overlay managed the normal update timer but
did not fix the separate discovery-timer lifecycle.

## Decision

- Keep the telemetry integration pinned to pristine upstream v4.0.1 for the
  current commissioning baseline.
- Accept that successful samples may be more than ten minutes apart while the
  SolisCloud API is slow. Do not impose the removed 60-second whole-update
  timeout: live evidence showed a slow request eventually return valid data.
- Do not add another local reliability patch while SolisCloud itself is failing
  and before a distinct post-discovery polling failure is reproduced.
- Treat entity freshness and availability, not the integration's loaded state,
  as the telemetry readiness signal.
- Do not manually reload the telemetry integration while it is failing. A clean
  Home Assistant Core restart clears leaked in-memory discovery callbacks and
  leaves one retry chain.
- Allow the battery controller to remain `DEGRADED` without writes and recover
  passively when fresh telemetry returns.

## Deferred smallest useful upstream work

If reliability work resumes, first improve safe diagnostics: record endpoint,
elapsed time, HTTP status, Solis response code/message and failure category
without credentials. Only then consider bounded retry/backoff for transient
discovery requests and removal of duplicate discovery fetches. Do not replace
the telemetry source with Solis Cloud Control as a reliability fix. Any
lifecycle patch must retain and cancel both discovery and update callback
handles and prevent rescheduling after unload.
