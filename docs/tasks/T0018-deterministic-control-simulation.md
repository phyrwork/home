# T0018 — Migrate the deterministic battery-control simulation

Status: Approved

Approval: small-model design council, 2/2

Parent: T0001 — Solis battery arbitrage control

Depends on:

- T0016 — guarded dynamic-slot commissioning evidence;
- T0017 — pure strategy composer.

## Objective

Build a pure discrete-event safety and energy-accounting harness around the real
T0017 composer, then migrate the useful physical coverage from the legacy
command simulation before later runtime cutover.

The harness owns simulated environment and device physics only. It does not
duplicate strategy selection, read Home Assistant, write Solis controls,
authenticate or deploy.

## Simulator and unit-under-test boundary

At every controller-evaluation event, construct the current authoritative T0017
`CompositeSnapshot` and prior `ComposerState`, then call the real pure T0017
composer under test.

Composer output may schedule modeled requested mutations. It is never supplied
as scenario input and never becomes the expected oracle.

Scenario expectations, conservation equations, safety invariants and economic
lower bounds are independently declared or computed from physical inputs. A
test must not validate a T0017 value by copying that same value into the expected
result.

## Exact event engine

Use exact `Decimal` quantities and aware UTC instants. The deterministic queue is
ordered by:

```text
(instant, event_priority, stable_sequence)
```

Integrate physical energy over the half-open interval
`[previous_instant, event_instant)` using state established after the preceding
boundary. Then process every current-instant event in this order:

1. guard, watchdog and fail-safe assertions;
2. evidence, dispatch, source, window, slot and deadline expiry plus
   forecast/rate/reserve boundaries;
3. pre-scheduled physical stop/disable;
4. pre-scheduled physical policy or slot mutation;
5. advanced cloud/device proof or reconciliation observation;
6. controller evaluation; and
7. desired service request and Home Assistant readback.

Stable sequence breaks ties inside one priority. Invalidation always precedes a
coincident keep/apply/proof decision. A desired request cannot become physically
effective at the same instant unless the commissioned lower-bound latency
explicitly allows it.

Reject unbounded event counts and zero-time recurrence loops with typed
`UNPROVED` results.

## Complete event scheduling

Schedule controller evaluation for:

- every heartbeat;
- every T0017 `fresh_until`;
- intent end and expiry;
- transition, reconciliation, stop and cleanup deadlines;
- final-charge cutoff;
- T0015 cycle and policy-proof deadlines;
- source and dispatch expiry;
- watchdog deadline;
- forecast, rate and reserve boundaries;
- target, capacity, floor and reserve crossings; and
- every external state change.

Re-run the real composer after every physical mutation and every valid or invalid
proof observation.

## Physical model and ledger

Model:

- stored battery energy, capacity and protected/dynamic reserve floor;
- charge and discharge efficiency exactly once at their AC boundaries;
- household load;
- external PV/negative load;
- ordinary grid contribution exactly `MAXIMUM_GRID_IMPORT_POWER_KW`;
- forced-charge grid import;
- battery AC input and output;
- grid export and conversion losses;
- T0016 conservative direction power bounds and shared limits; and
- commissioned Feed-In Priority and Peak Shaving interaction evidence.

Never invent an uncommissioned inverter interaction. A scenario needing missing
candidate-behavior evidence is `UNAVAILABLE`.

Split integration at every physical, forecast, rate, reserve, target, source and
deadline boundary. Record an exact conservation ledger for load, PV, ordinary
grid, forced import, battery AC input/output, stored energy, export and losses.

At every boundary prove:

- stored energy is within capacity and dynamic reserve;
- no simultaneous battery charge/discharge;
- at most one slot direction is active;
- power is inside commissioned fixed-point and shared bounds;
- import/export are not double-counted;
- control has fresh exact authority; and
- cleanup completes before the applicable strict deadline.

Impossible physics returns a typed violation and is never clamped into success.

## Four mutation/proof layers

Represent separately:

1. desired service request;
2. Home Assistant readback;
3. physical inverter-effective mutation; and
4. later Solis Cloud/device observation.

HA readback changes no physical energy. Battery energy starts or stops changing
only at the physical device-effective event. Cloud proof observes later state and
never retroactively changes the energy ledger.

Simulated energy/SOC and HA state can never manufacture:

- T0017 `AppliedStateProof`;
- advanced device reconciliation;
- all-slots-off or persistent-policy proof; or
- a later authoritative `FULL_SOC` source revision.

Those arise only from explicit, causally valid scenario cloud/device observations
whose source, revision and timestamp advance after the relevant physical mutation
and whose exact state/fingerprints match. Without them, the real composer must
remain waiting or stop.

Runtime voltage and temperature observations are also explicit independent
scenario evidence. Never derive them from stored energy. Missing, stale or
out-of-envelope conditions invalidate the T0016 operating point.

## Realized and bounded device behavior

A scenario may supply an immutable `RealizedDeviceTransition` containing exact:

- requested, HA-readback, physical-effective and cloud-observed times;
- realized AC power and direction;
- operating conditions;
- source/revision; and
- authority and operating-point fingerprints.

Validate causality, current authority, operating envelope and commissioned
time/power bounds. Invalid exact realizations are scenario errors.

When realization is not exact, return a bounded result. Never choose a convenient
instant or power silently.

Mandatory adversarial endpoint runs include:

- earliest effective plus maximum discharge for reserve depletion;
- latest effective plus minimum discharge for headroom/delivery;
- earliest effective plus maximum charge for capacity upper bounds;
- latest effective plus minimum charge for full/refill feasibility;
- latest effective stop for continued charge/discharge risk; and
- minimum realized export with maximum required refill import, loss and wear for
  conservative economics.

A strategy or migration claim passes only when every applicable uncertainty path
passes.

## Uncertainty soundness

Endpoint-only analysis is valid only with a recorded monotonicity proof over the
complete closed-loop region being claimed.

Target stops, capacity/floor/reserve crossings, forecast/rate boundaries, source
expiry, policy changes and composer thresholds can introduce non-monotonic
breakpoints. Discover and split uncertainty cells at every such breakpoint, then
enumerate every subcell endpoint.

If monotonicity cannot be established or finite breakpoint coverage is
incomplete, report `UNPROVED` or `BOUNDED`. Passing four outer corners alone never
proves safety.

## Failure and lifecycle modeling

Support deterministic:

- service rejection and timeout;
- cancellation and repeated cancellation;
- delayed or missing HA readback;
- delayed, contradictory or missing physical mutation;
- stale or invalid cloud proof;
- telemetry/source staleness;
- dispatch withdrawal;
- guard assertion;
- watchdog action; and
- restart with lost composer/cycle state.

Device reconciliation revisions advance only through explicit valid device
events. Model T0017's sequential stop, fail-safe proof, candidate convergence and
strategy admission exactly.

## Required scenarios

Cover at least:

- healthy idle Feed-In behavior with external PV charging;
- standard-cheap charge to full;
- bonus dispatch and mid-window withdrawal;
- T0014 desired and partially reachable pre-discharge headroom;
- T0015 ten-minute maximum with activation loss, discharge-off proof, refill and
  later full revision;
- final-charge equality cutoff and infeasible best effort;
- variable load, PV and reserve trajectory;
- restart through fail-safe and candidate proof;
- guard, watchdog and stale evidence;
- failed/late write, readback, physical effect and device proof; and
- cross-midnight and DST horizons.

Validate the actual import/export/loss/wear ledger against every strategy's
economic diagnostics.

## Property and metamorphic coverage

Use recorded reproducible seeds across bounded input ranges. Verify at least:

- energy conservation;
- reserve/capacity/power monotonicity;
- efficiency loss;
- no slot overlap or toggling;
- semantic poll revisions preserve `KEEP_ACTIVE`;
- material plan/source changes require stop;
- greater device delay never improves feasibility;
- lower export or higher import never creates a profitable cycle; and
- missing proof never advances recovery or cycling gates.

Random generation belongs to tests only and every failure records its seed and
minimal scenario.

## Legacy migration gate

Retain the legacy deterministic harness until explicitly mapped overlapping
scenarios pass under both harnesses.

Compare physical energy, reserve and ledger quantities—not obsolete command names.
Intentionally new arbitrage, proof and recovery behavior need not match legacy
commands.

Record the mapping from each retained legacy scenario/invariant to its new
equivalent. A later cleanup task may delete command-specific cases only after
equivalent physical coverage exists.

## Compatibility boundaries

- Do not change runtime coordinator, controller, sensor or YAML.
- Do not write Home Assistant or Solis.
- Do not remove the legacy harness in this task.
- Do not authenticate, deploy or access live services.

## Tests

Test the event engine, ledger, uncertainty analysis and scenario fixtures for:

- exact same-instant priority and half-open integration;
- invalidation before coincident apply/keep/proof;
- event completeness and zero-loop bounds;
- invocation of the real T0017 composer;
- independent-oracle enforcement;
- all four mutation/proof layers;
- proof and later-full-revision non-synthesis;
- exact realization validation;
- every mandatory adversarial endpoint combination;
- monotonic and non-monotonic breakpoint handling;
- explicit voltage/temperature envelope validation;
- every required physical and lifecycle scenario;
- every conservation and safety invariant;
- seeded property/metamorphic behavior; and
- mapped legacy/new physical equivalence.

Run the complete house-battery and deployment suites.

## Acceptance criteria

- The real T0017 composer is the only strategy selector in the harness.
- Expected results are independent physical oracles.
- HA optimism and simulated SOC never create device proof.
- Energy begins at physical effect, not request/readback/proof.
- Safety/economic claims hold across every sound uncertainty path.
- Unknown non-monotonic regions fail closed as unproved.
- Physical conservation and reserve are exact at every event.
- Legacy coverage is removed only after mapped physical equivalence.
- Focused and full local tests pass.
- The implementation commit changes this card's status to `Implemented`.

## Implementation ownership

- Branch: `codex/T0018-deterministic-control-simulation`.
- Isolated worktree.
- Small-model implementation agent after T0017 is integrated.
- Prefer new simulation modules and fixtures; do not touch runtime hotspots.
