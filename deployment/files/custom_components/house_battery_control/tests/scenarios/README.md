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
