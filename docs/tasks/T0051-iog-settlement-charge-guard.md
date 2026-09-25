# T0051 — IOG settlement-period charge guard

Status: Implemented and deployed as 0.2.2 — 224 affected tests passed; natural commissioning remains partial

Preparatory regression coverage now exists in
`test_iog_idle_projection.py` and
`tests/scenarios/projections/iog_ev_finished_2026_09_22.yaml`: observed EV stop plus
explicit synthetic idle continuation through the four 21:30–23:30 periods.
All eight bonus ordinary/cycle cases now pass with the guard, as do both 23:30
static-off-peak cases. Sequential guard/planner/Solis-adapter replay also verifies
latch survival, expiry, native-slot cleanup and overnight resumption. None remain
marked expected-failure. Live results and unobserved scenarios are recorded in
the [commissioning log](../house-battery-commissioning-log.md).

Design and observed inputs:
[IOG battery charge guard](../intelligent-octopus-battery-charge-guard.md).

## Accepted behavior

Household charging permission is independent of scheduled price opportunities.
The whole interval requested by `charge_is_authorized(start, end)` must be
covered by the union of guaranteed 23:30–05:30 Europe/London windows and the
currently qualified bonus half-hour. Intervals are half-open; UTC identifies
settlement periods without DST ambiguity.

Outside overnight, qualify the current period only when current
`SMART_CONTROL_IN_PROGRESS` and EV power strictly above 50 W overlap, with
both observations evidenced within that period. Use the dispatches retrieval
sensor's timestamp value for smart-state source freshness and Shelly power's
HA report timestamp for power observation freshness, subject to the source
checks below. Neither timestamp may be in the future or borrowed from a previous
period. `last_changed` is not a freshness timestamp.

Latch qualification until the settlement boundary. Zero power, a subsequent
Boost or a later unknown state cannot erase a previously observed qualifying
charge. No new permission from missing/invalid data. Restart loses the latch;
restored sensor values alone must not establish new evidence. Only the current
bonus period is retained. Future overnight permission is known; future bonus
permission is not.

Apply this predicate to every charge/recharge segment, not discharge. Preserve
reserve/economic constraints and recharge-before-discharge enabling. This work
does not introduce speculative unpaired discharge or reconstruct the EV's
six-hour allowance. Charge Cap remains an explicit operating assumption.

## 1. Verify input freshness and capture the contract

- Extend `deployment/tools/house_battery_evidence.py`'s exact entity allowlist
  with the configured Intelligent State and dispatches retrieval timestamp.
  Capture relevant source values, timestamps, authorization diagnostics and
  physical charge slots, without unrelated entity dumps or credentials.
- Capture a natural charge across a settlement boundary. Verify that Shelly's
  `last_reported` advances on actual device reports rather than unrelated cached
  republications; the current power entity exposes no device measurement time.
- Verify Octopus's retrieval timestamp advances with a successful refresh of
  the same coordinator that supplies Intelligent State. Handle the order in
  which those two HA entities update so a new retrieval timestamp is not paired
  with an obsolete state during a coordinator update.
- Inspect restart/restoration and failed refresh behavior. Require a successful
  post-start retrieval and power report before establishing bonus permission.
- Set explicit maximum evidence ages from the captured cadence. In-slot
  membership remains mandatory even when the previous-period reading would be
  within the age limit. Document chosen limits in configuration/tests. Normal
  Octopus source polling is three minutes, so qualification need not be instant
  at a boundary. Do not invent second-level physical certainty from that feed.

## 2. Add an isolated authorization model

Add `charge_authorization.py` with small, independently testable value objects:

- `ChargeGuard`: retains input values/report times, startup time and the
  transient latch; rejects stale/restored evidence before minting permission.
- `QualifiedHalfHour`: UTC start/end, qualification time and the evidence times
  that established permission.
- `ChargeAuthorization`: a snapshot of current qualification and overnight
  policy, exposing interval coverage and the next charging cutoff.

Keep qualification state in the controller; keep interval calculations pure.
Use explicit Europe/London overnight policy and a validated entity configuration
for the three inputs in `config.py` and `house_battery_control.yaml`. Default
threshold is the agreed 50 W, compared strictly with `>`.

## 3. Wire controller observations and wakeups

- Watch changes to EV power, Intelligent State and the retrieval timestamp.
  Keep the existing minute backstop and explicitly schedule the next :00/:30,
  23:30 and 05:30 boundaries. No broad report-event subscription is needed.
- At each wake, read independent report/source timestamps even if values have
  not changed. Expire/update qualification before forecast or inverter I/O.
- Preserve evidence of a short valid overlap if a subsequent change arrives
  while the serialized writer is busy: use a small synchronous observation step
  in source callbacks to update the same guard state, then mark the worker dirty.
  Do not run the planner or writer in callbacks. This prevents coalescing two
  changes into only the later, nonqualifying snapshot. This is evidence capture,
  not an extra full replanning pass for every power fluctuation.
- Never refresh one input's timestamp because the other reports. Once qualified,
  do not churn the authorization identity on each new reading.
- Missing authorization is a normal blocked charging decision, not a generic
  planning error that can preserve an old charge schedule indefinitely.

## 4. Gate planning and all actuator paths

- Pass an authorization snapshot explicitly to `build_plan`; retain candidate
  rate windows for economic calculations and diagnostics.
- Bound ordinary charging by the authorized coverage end and existing shorter
  lease/component deadlines. Only complete, nonempty minute-encodable segments
  may be emitted; rounding must never extend beyond permission.
- In `_cycle_schedule`, `_cycle_can_start` and `_cycle_after_recharge_fits`,
  validate recharge coverage independently of discharge. Do not globally shrink
  `CheapWindow` to the qualified half-hour, which would also constrain discharge.
  Do not pre-arm another discharge whose promised recharge is unauthorized.
  Keep configured phase duration; skip a pair that cannot fit rather than
  silently shortening its recharge. Existing discharge profitability rules
  remain separately visible.
- Recheck retained recharge in `_retain_armed_schedule`, reconstructed leases,
  startup reconciliation and start retries. Retaining a confirmed pair during
  stale SOC must not bypass authorization.
- Before an enable, re-evaluate permission using the actual current time and
  the remaining interval. A charge interval begun in a past period need not
  retain that past period's latch; only its unelapsed portion is actionable.
  Prevent dependent discharge enabling if its armed recharge is no longer safe.
- Discover expired/unauthorized charge directions and enqueue stops before any
  early return for missing forecasts, stale telemetry or planning errors. Use
  existing stop priority, retry/backoff and authoritative off confirmation.
- Enforce the cutoff in native inverter schedules. Audit delayed service calls,
  timezone conversion and minute rounding. Record expiry before an ambiguous
  write, preserving recovery cleanup. Native repeating schedules retain their
  documented next-day outage limitation.

## 5. Expose diagnostics

Add attributes to existing controller diagnostics: current settlement start/end,
qualification time, EV power/report time, smart state/retrieval time,
authorization reason/cutoff and blocked reason. Distinguish a scheduled bonus
opportunity from a qualified billing period and from a physically confirmed
charge schedule. Avoid adding a separate user-facing control mode.

## 6. Automated verification

Add focused authorization tests and extend planner/controller/Solis integration
tests and behavioral replay. Update config fixtures and evidence-collector tests.
Run the full house-battery suite plus affected deployment tests using the
repository's deployment test environment.

Required cases:

| Area | Expected result |
| --- | --- |
| Overnight | Authorized without EV inputs; exact 23:30 inclusion / 05:30 exclusion |
| Threshold | 2–3 W, 50 W rejected; above 50 W plus smart state qualifies |
| Attribution | Boost-only, unknown state and smart-enabled setting alone rejected |
| Temporal overlap | Inputs observed separately without overlapping true conditions rejected |
| Freshness | Previous-slot, stale, future, restored and malformed timestamps rejected |
| Repeated values | Same numerical/state value with valid current-slot evidence can qualify |
| Source versus report | Cached smart-state HA report cannot substitute for source retrieval |
| Latch | Smart charge then zero power/Boost remains qualified through period end |
| Boundary | Latch expires; continuous charging needs new-period evidence |
| Busy worker | Brief qualifying overlap is not lost while a Solis write is awaited |
| Charge coverage | Entire future recharge covered; boundary equality accepted, overrun rejected |
| Cycling | Recharge-only authorization; no new discharge backed by unqualified recharge |
| Retention | Stale telemetry, forecast errors and confirmed-pair recovery cannot bypass guard |
| Writes | Delayed preparation, retries and ambiguous enables preserve cutoff/cleanup |
| Time | Midnight, both DST transitions, repeated local half-hours and native rounding |

Replay the supplied 18:00–18:12 charge with a schedule lasting to 19:00 and prove
no charge in 18:30–19:00 without new evidence. Add a continuous-charge boundary
case and an ordinary-charge versus full-SOC recharge case. Update replay's shared
invariants to distinguish economic constraints from charge authorization rather
than simply requiring every segment to lie in a qualified bonus period.

Acceptance invariant: every emitted or newly enabled charge segment's remaining
interval is authorized at decision time and its native end does not exceed the
authorized deadline. Discharge qualification is not required. Failure to stop
physically is reported, never represented as a confirmed success.

## 7. Deployment and live verification

1. Finish local tests and inspect the exact diff. Update runtime documentation
   to describe the implemented behavior and retain the historical investigation.
2. Install only changed files for iteration, check HA configuration and reload
   the controller through its existing bounded stop/start path. Reconcile any
   previously armed ordinary or cycle recharge before allowing new starts.
3. Because the plan adds a component file and configuration, run the full Ansible
   deployment after targeted iteration, as required by AGENTS.md. Verify the
   retrieval sensor remains enabled and available. Cache credentials once for
   the verification session, deleting the tempfile on completion.
4. Capture one real bonus qualification, EV stopping within that period,
   expiry without next-period evidence, and renewal during continuous smart
   charging. Check actual inverter charge power and native end time, not just
   desired action. Confirm an ordinary recharge and a paired-cycle recharge.
5. Confirm overnight behavior without EV charging. Exercise Boost and source
   outages in simulation first; do not deliberately incur a live Boost bill just
   to obtain coverage. Record any live case not yet observed as pending.
6. Check native inverter clock accuracy/end-time semantics and stop latency.
   Choose conservative earlier cutoffs if needed; do not claim zero boundary
   leakage without measurement. Compare captured qualified periods with Octopus
   settled household half-hours when available.
7. On a regression, stop/clean up affected charging and use an overnight-only
   fallback rather than reinstating schedule-only bonus authorization. Verify
   cleanup; do not silently restore unsafe bonus slots.

Completion requires passing automated tests and an explicit live commissioning
record separating verified behavior, pending natural scenarios and known
physical/outage limits. Do not mark unobserved cases as passed.
