# House-battery behavioural replay scenarios

Each YAML file is a reduced real incident and an explicit statement of the
behaviour that should have occurred. The test runner feeds its observations
through the production planner and Solis adapter; it is not a saved-output
golden test.

Use `deployment/tools/house_battery_evidence.py` when a fresh timestamped HA
event/state capture is useful. Keep that raw JSONL as commissioning evidence;
manually reduce only the observations that affected the decision into the YAML
scenario. The reduction step is intentional: it records the accepted behaviour
instead of blindly treating the incident's buggy output as correct.

To add an incident:

1. choose the shortest time range that reproduces the behaviour;
2. copy only relevant tariff boundaries, SOC/device timestamps and cycle
   settings into a new YAML file;
3. describe the intended action, cycle phase, deadline and exact half-open
   segments at every meaningful event boundary;
4. identify a direction that must be retained without writes when applicable;
5. record required enable/stop ordering only when it is part of the contract;
6. run `test_behavioral_replay.py` and the full house-battery suite; and
7. cite the original incident in the scenario and commissioning log.

Do not include credentials, entity-state dumps unrelated to the failure,
economic-performance expectations or implementation-internal state. Add a new
schema field only when a second accepted scenario actually needs it.

`projections/iog_ev_finished_2026_09_22.yaml` preserves the observed 21:11 EV stop
and a user-requested forward projection of idle power through 23:30. Future
samples, successful retrievals and the full-SOC variant are explicitly synthetic.
`test_iog_idle_projection.py` exercises each projected boundary in the production
planner and real authorization model; all bonus cases must reject charge and
overnight cases must allow it. Sequential tests also carry the latch and cycle
state through the stop, boundary and overnight transition, checking native slot
cleanup through the Solis adapter. Earlier source retrievals are explicitly
synthetic, since that sensor was enabled after the EV stopped. Keep this
projection schema separate from the existing rolling-cycle replay format.

`incidents/idle_surplus_2026_09_23.yaml` reduces the recorder diagnostic snapshot
captured during the idle-with-surplus investigation. `test_surplus_priority.py`
replays the observed charge-guard inputs, SOC, cheap-window end and reserve
through the production guard, planner and Solis adapter. Current prices, EV states and inverter values come from the adjacent recorder
JSON captured on 2026-09-24. Future tariff intervals/provenance and capability
metadata are representative fixtures because recorder omits those attributes.
The reserve forecast is injected from the
recorded result; this does not claim to replay raw forecast or tariff inputs.
Additional transition cases are synthetic. The expected behaviour is surplus
export despite the unqualified cheap period, with authorized charging taking
priority when available.

`test_reserve_stall_replay.py` uses `incidents/reserve_discharge_stalled_2026_09_25_states.json`
to replay the enabled-but-idle export incident. Recorded SOC, power and device
timestamps verify the 2% stopping boundary and refusal to rearm. Capability
metadata and tariff inputs use fixtures; the reserve forecast result is recorded.
This verifies controller decisions, not physical load following after disabling.
