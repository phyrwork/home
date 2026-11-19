# Dashboard snapshots

Climate is a Home Assistant-managed (storage mode) dashboard at
`/dashboard-climate/overview`, shown in the sidebar. It uses compact built-in climate
and sensor tile cards with standard section headings and layout, without Markdown or
custom cards. `climate.yaml` is the versioned definition, not an Ansible-managed
YAML dashboard. To restore it, create an empty dashboard named Climate with URL
`dashboard-climate`, then paste `climate.yaml` into its Raw configuration editor.

The Home section contains tiles for `binary_sensor.house_heating_away` and
`switch.house_heating_zone_call_for_heat`. Household away On means everyone is
confirmed away. Room sections contain the thermostat and, where configured,
its latched Heating vacancy tile. Vacancy On means heating may pause; protection
and household controls are described in [heating commissioning](../heating-commissioning.md).
Thermostats remain interactive. Tapping any status tile or its icon opens the
standard entity details and history rather than toggling heating directly.

Vacancy tiles also show `Vacancy in N min` while an uninterrupted countdown is
running. The `vacancy_countdown` attribute uses the shared delay and the same
motion/protection timestamps as the vacancy state. It is empty during protection,
while motion is detected, when inputs are unavailable, after expiry, or if the
next protected period would start before vacancy. The countdown refreshes each
minute and is rounded up. It adds no other status labels or helper entities.

Climate tiles show heating activity and current temperature alongside inline
target-temperature buttons. Tapping the tile opens full thermostat details.
