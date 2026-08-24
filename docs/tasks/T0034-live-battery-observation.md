# T0034 — Live battery commissioning and 24-hour evidence capture

Status: Implemented locally — live execution pending

Depends on: T0026, T0031

## Objective

Create a minimal read-only Home Assistant WebSocket collector and an exact live
runbook for the remaining T0026 acceptance. First deploy and commission discrete
behaviors; only after those pass, run an uncontaminated 24-hour observation.

The collector observes evidence. It consumes only an operator-provided cached
token and never acquires or refreshes credentials, calls a Home Assistant
service, changes state, controls Solis, or decides that physical behavior is
proven from control-plane readback alone.

## Evidence artifacts

Use one new append-only JSONL file for each phase. Record alongside it:

- T0034/controller Git commit and deployed custom-component file hashes;
- HA Core/OS versions and UTC start/end times;
- collector command with token value omitted;
- SHA-256 and byte/record counts after capture;
- every observer disconnection/reconnection and any evidence gap; and
- an operator verdict table linking each requirement to exact JSONL timestamps
  plus SolisCloud/whole-site physical evidence where required.

Keep the predeploy, discrete commissioning and 24-hour soak files separate.
Never append a restarted experiment to an artifact already declared complete.

## Read-only collector

Add `deployment/tools/house_battery_evidence.py`. It must:

- accept an HA base URL, output path, positive duration, and either a cached
  token file or named environment variable;
- derive `/api/websocket`, authenticate without logging or persisting the token,
  and use only `subscribe_events` and `get_states` WebSocket commands;
- subscribe independently to `state_changed` and `call_service`;
- immediately capture, then repeat every 60 seconds, a filtered full-state
  snapshot;
- retain raw HA event/state dictionaries inside UTC-timestamped JSONL envelopes;
- include controller diagnostics, the cycle helper, configured Solis telemetry
  and controls, every charge/discharge slot field, Octopus fused/current-rate/
  demand/export/dispatch entities, and EV charger power;
- retain a service event only when its payload targets a relevant entity;
- recursively redact credential-like fields and the in-memory token;
- append with restrictive file permissions and never truncate existing data;
- record connection loss, reconnect with capped backoff until the monotonic
  duration expires, and take a prompt snapshot after a missed connection; and
- close the socket/file and write a terminal record on duration completion or
  cancellation.

No framework, database, analysis engine, state mutation, `op`, SSH, REST write,
browser control, notification, or token refresh belongs in the collector.

Example from the repository root, after a token has independently been cached:

```sh
deployment/.venv/bin/python deployment/tools/house_battery_evidence.py \
  --base-url https://home.newtonho.me \
  --output scratch/battery-evidence/soak.jsonl \
  --duration 24h \
  --token-env HA_API_TOKEN
```

Use `--token-file "$HA_API_TOKEN_FILE"` instead when the current shell follows
the repository's restrictive cached-token-file convention. Never put the token
value itself on the command line.

## Phase 0 — exact predeploy snapshot

Start the collector before any mutation and capture at least two minute
snapshots. Persist:

- repository/deployment commit and dirty status;
- HA Core/OS and installed custom-component versions/hashes;
- controller heartbeat, health, action, retry/error attributes and all reserve
  energy sensors;
- Storage Mode, Allow Grid Charging, Battery Reserve/SOC, inverter clock,
  capability currents and all 12 direction enables/times/currents/SOC targets;
- fresh inverter SOC, battery power, voltage and device timestamp;
- Octopus current demand, import/export rates, dispatch and EV charging state;
- sentinel enabled state plus the exact presence/state of the legacy guard and
  script that this deployment is expected to delete; and
- commissioned EMS disabled, Feed-In Priority baseline, Peak Shaving disabled,
  Feed-In Power Limit disabled, protective SOC settings and output power.

An unavailable or unknown slot enable makes the predeploy state unproven.

## Phase 1 — bounded transition and deployment

With explicit live authorization:

1. use and prove the currently deployed legacy control-disable helper to
   suppress its old writer; this is a one-time migration action, not a feature of
   the new runtime;
2. while suppressed, select and confirm Self-Use, read all configured enables,
   switch off only directions observed on, and reread until all 12 are
   authoritatively off; cross-check the complete state in SolisCloud;
3. retain the predeploy collector while running the normal Ansible playbook and
   record its result plus the files/hash about to become active;
4. acknowledge that the playbook currently has no remote `ha core check` between
   file copy and its restart handler; local validation is green, and post-restart
   `ha core check` is mandatory evidence for this deployment gap;
5. let the collector reconnect, run HA configuration/status checks, and record
   HA Core/OS plus the actually installed component hashes;
6. inspect only the remote house-battery component directory: because rsync
   excludes `__pycache__`, remove that exact stale bytecode directory if present
   and restart before accepting import/source-shape evidence;
7. verify pinned Solis Inverter `v4.0.1` and Solis Cloud Control `v2.21.0` load
   without setup/poll errors;
8. prove obsolete guard/script entities are absent and the cycle helper,
   diagnostics, entity map and sentinel remain present; and
9. after fresh telemetry/planning inputs, prove the new guard-free controller
   restores and confirms Feed-In Priority before its first start.

Fail the phase on an unaccounted enabled/unknown direction, configuration or
deployment failure, two simultaneous writers, unexpected managed entities, or
a start before Feed-In Priority is proven.

## Phase 2 — discrete commissioning tests

Use bounded native schedules and a fresh collector file. Execute one test at a
time; confirm all directions off between tests.

1. **Cheap charge:** prove Allow Grid Charging, correct local slot fields,
   positive battery power/whole-site import/SOC movement, and stop at cheap end
   or target SOC.
2. **Reserve discharge:** prove correct reserve target, negative battery power
   and whole-site export/load offset, then authoritative off at reserve SOC.
3. **Direction handover:** prove old direction off before the new enable and no
   charge/discharge overlap in snapshots or events.
4. **Full-SOC cycle:** at full SOC during a profitable cheap interval, prove
   discharge for the configured duty duration, stop at its bound, then charge
   back toward full without a pre-discharge phase.
5. **Local midnight charge:** determine whether `24:00` is accepted. Prove the
   first segment stops and the adjacent second segment becomes physically
   effective. If only `23:59` works, commission it and record the one-minute gap.
6. **Local midnight discharge:** prove the equivalent two-segment readback,
   physical flow and no overlap. Because normal reserve economics do not span
   the static cheap midnight, run this as an isolated controller-unloaded,
   sentinel-disabled native-control exercise and restore both afterward.
7. **Transient telemetry loss:** prove `DEGRADED`, no new start, passive recovery
   before 15 minutes, and no sentinel write while heartbeat remains fresh.
8. **Fail-safe/restart:** prove 15 minutes continuous degradation latches
   mode-only Self-Use while known stops continue; recovered inputs do not clear
   it; fresh integration setup/restart does.
9. **Crash sentinel:** with the controller actually dead, prove startup grace,
   stale-heartbeat/mode recheck and exactly the single Self-Use write.
10. **Orderly shutdown:** prove continuing shutdown heartbeats, Self-Use first,
    then only observed-on directions off; unknown directions receive no write.
    Retain SolisCloud readback because the HA WebSocket disappears during stop.

Control-plane service/readback evidence is necessary but not physical proof.
Tests 1–6 require fresh inverter power plus whole-site demand/export and, where
practical, SOC movement. Restore the commissioned baseline after every test.

## Phase 3 — uncontaminated 24-hour soak

Begin only when every discrete test is passed or explicitly scheduled for the
soak, there is no stop debt, Feed-In Priority is proven, all current controls
match intent, and telemetry is fresh.

For the next continuous 24 hours:

- make no manual Solis/HA control writes;
- do not deploy, reload, restart, edit helpers, or run another commissioning
  experiment;
- keep only the integration and stale-heartbeat sentinel as possible writers;
- run one collector artifact for the full interval; and
- record any observer gap. A gap over five minutes makes the soak inconclusive
  and requires a fresh 24-hour run.

## 24-hour pass/fail evidence

Pass requires all of the following in one uncontaminated artifact:

- at least 23h55m of minute snapshots (at least 1,436 distinct UTC minute
  buckets) and no unaccounted gap over five minutes;
- heartbeat normally no older than 90 seconds and never over 180 seconds (or 60
  seconds future) while expected to run, with no sentinel write under a fresh
  heartbeat;
- device telemetry remains under the 30-minute freshness budget; samples used
  as physical transition proof are at most five minutes old;
- telemetry loss reports recoverable `DEGRADED` and later recovers without a
  restart; generic prolonged degradation does not become `FAIL_SAFE`, while a
  hard invariant or important stop unconfirmed for
  `IMPORTANT_STOP_FAILSAFE_TIMEOUT` may latch it;
- every start follows a valid trusted window/reserve opportunity and every
  direction change proves the previous direction off first;
- no charge/discharge overlap, including the two local-midnight segments;
- cheap charging reaches at least +1 kW for two fresh samples within five minutes
  of the eligible start, with Octopus demand rising at least 0.75 kW absent a
  documented load/PV confounder, and subsequently falls below +0.5 kW for two
  samples no later than five minutes after the fused-rate cheap end or target
  SOC; the current static end is expected near 05:30 local but the fused rates
  are authoritative, and bonus dispatch is assessed if one occurs;
- reserve discharge reaches at most -1 kW for two fresh samples and subsequently
  reduces Octopus demand/increases export by at least 0.75 kW absent a documented
  confounder, then rises above -0.5 kW for two samples no later than five minutes
  after the dynamic target; SOC never crosses below `MINIMUM_SOC_PERCENT`;
- at least one full-SOC duty discharge reaches at most -1 kW and returns to at
  least +1 kW charge during cheap time, with duration agreeing with the helper
  within one fresh telemetry interval;
- actual energy is within 0.02 kWh of `32.1536 × SOC / 100`, reserve balance is
  within 0.02 kWh of actual minus target energy, and reserve SOC is at least 10%;
- stopped slots do not recur the following day; and
- every important stop retries until authoritative off, while suppressed start
  generations do not write indefinitely. If failures occur naturally, start
  attempts follow 0/15/60 seconds and stop retries approximately 5/10/20/40/60
  seconds capped; do not manufacture a cloud failure merely for evidence.

Commissioned-only Peak Shaving, feed-limit, output and protective settings must
remain unchanged throughout; any runtime mutation is a release failure.

Any violated invariant fails the soak. A required physical precondition that
never occurs (for example the battery never reaches full SOC) makes the run
inconclusive rather than silently passing; arrange and repeat a bounded test.

## Offline tests

Prove without auth or network:

- exact entity and relevant-service filtering, including every slot field;
- UTC JSON serialization and recursive credential/token redaction;
- positive finite duration, base URL, output and token-source validation; and
- reopening/reconnecting appends valid JSONL records without truncation.

## Completion

- Local collector tests, compile and diff checks pass and the implementation is
  committed without auth/network/live access.
- T0034 remains live-pending until all three phases have timestamped evidence.
- T0001/T0026 remain incomplete until the 24-hour verdict is recorded.

## Local implementation evidence

- `deployment/tools/house_battery_evidence.py` uses the deployment venv's
  existing `aiohttp` dependency and no added package or framework.
- The only outbound WebSocket message types are `auth`, `subscribe_events` and
  `get_states`. There is no HA service/REST mutation path or credential fetch.
- Exact entities plus narrow prefixes cover diagnostics, cycle duration, Solis
  telemetry, the complete Garage Inverter Control namespace, Octopus fused/
  current/demand/dispatch data, sentinel state, EV charger power and the exact
  legacy guard/fail-safe/watchdog entities whose removal must be observed.
- Records use one redacted UTC JSON line per append through a mode-0600,
  no-follow, append-only file descriptor. Reopening never truncates evidence.
- Network/protocol loss is itself recorded; reconnect uses capped backoff and a
  new connection requests an immediate snapshot. Each outstanding snapshot has
  a bounded response deadline, so a continuing event stream cannot hide a
  stalled `get_states`; expiry is recorded and reconnects through the same
  path. A monotonic deadline and stop event close the WebSocket/session and
  append the terminal reason.
- The 32 MiB WebSocket limit accommodates HA's unfiltered `get_states` response;
  filtering occurs before the relevant snapshot is persisted.

Offline gates:

- 15 focused collector tests passed;
- 17 collector-plus-sentinel tests passed;
- all 56 deployment tests passed;
- all 93 battery component tests passed; and
- CLI help, `compileall`, read-only protocol search and `git diff --check`
  passed.

No `op`, real credential read, network, SSH, browser, HA API, deployment or live
Solis access was used. Live commands and artifact hashes belong to the later
authorized execution and must be appended to this card/commissioning log then.

## Deployment cutover hardening

The authenticated pre-deployment inspection found legacy module bytecode under
`/config/custom_components/house_battery_control/__pycache__`. The component
rsync intentionally excludes generated bytecode, so deletion of source modules
alone would not remove it. Deployment therefore removes only that exact cache
directory before synchronizing the seven-module component. It also runs
`ha core check` after all files are staged and before notified reload/restart
handlers run. A failed configuration check must abort the deployment without
restarting Home Assistant.

## Review

Two independent reviewers approved the local tooling with no blockers. Their
final review explicitly covered the bounded snapshot-response deadline, exact
legacy-entity filtering, read-only WebSocket protocol, descriptor-level `0600`
permissions and the 17 collector-plus-sentinel tests. Local implementation is
council-approved; deployment, commissioning and the 24-hour capture remain
pending live execution.
