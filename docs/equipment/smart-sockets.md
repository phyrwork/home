# Smart socket troubleshooting

All three affected P110M plugs were ultimately migrated to Matter, with live
readings verified. The microwave automation IDs were retained, the vacuum uses
`vacuum_socket`, and the tower uses `tv_power_tower`. The incident and intermediate
findings below explain the migration and record the address-change occurrence.

## 28 October 2026: address change and TP-Link protocol failures

The microwave's TP-Link integration retained `192.168.1.194`, but that address
had a different MAC address in HA's neighbour table. Unicast discovery at
`192.168.1.190` confirmed a P110M(UK) with the microwave's registered MAC,
`30:68:93:9d:02:51`.

Treat this as an observed address change, not evidence of its exact cause or
frequency. Connor's decision is to correct the address when encountered and
record incidents here. Consider DHCP reservations if it becomes frequent; no
reservation or static address was configured during this investigation.

The initial address-correction attempt did not complete: HA 2026.9.3's reconfigure flow did not
persist the supplied new host, and discovery at the correct address exposed a
second problem: unsupported `TPAP` encryption. The failed flows were closed;
the saved integration still pointed to `.194` at that stage. Do not mistake an address
correction for a verified recovery of monitoring or control.

Direct, unauthenticated discovery using the same python-kasa version required
by HA (0.10.2) confirmed the following devices advertise `SMART.TAPOPLUG`,
`TPAP`, HTTP port 80, login version 2:

| Device | Address | MAC | Observed HA failure |
| --- | --- | --- | --- |
| Vacuum Cleaner Socket | `192.168.1.186` | `30:68:93:9c:fa:01` | HTTP 403 at `handshake1` |
| Microwave Socket | `192.168.1.190` (current), `.194` (saved) | `30:68:93:9d:02:51` | Saved host unreachable; discovery at current host rejects TPAP |
| TV Power Tower | `192.168.1.64` | `30:68:93:9d:04:ed` | HTTP 403 at `handshake1` |

The vacuum's integration title is historically `Kettle Socket`; its registered
device and switch identified it as the Vacuum Cleaner Socket. The time of any
firmware update preceding the failure was not established.

Connor subsequently found Third-Party Compatibility disabled and enabled it,
but reported that no plugs are registered in the Tapo app. Repeating discovery
and reloading the vacuum/tower entries still produced TPAP discovery responses
and HTTP 403 handshake failures. Enabling the account setting alone did not
restore these devices; whether it has reached these unregistered plugs remains
unverified. Discovery also reports an empty `owner` and `obd_src: matter` on all
three devices.

The proposed next step at that stage was to register one existing plug in the
Tapo app using local discovery, then repeat the compatibility/protocol check.
Connor subsequently updated firmware and chose Matter pairing instead, as
recorded below. TP-Link documents adding an
already-paired Matter device from the Tapo app's Discovered Devices list
[without an initial factory-reset step](https://www.omadanetworks.com/us/support/faq/3564/).
TPAP support is tracked
[upstream](https://github.com/python-kasa/python-kasa/issues/1590).
[HA's TP-Link documentation](https://www.home-assistant.io/integrations/tplink/)
describes the compatibility setting. Do not assume a general HA upgrade fixes
TPAP without checking the supported protocol and testing it.

## Device lifecycle changes on the same date

- Connor power-cycled the PC socket. It returned through Matter and reported
  numeric power and cumulative energy. Its observed IPv4 address was `.228`,
  compared with `.230` in cached Matter diagnostics before the power cycle;
  this additional observation does not establish the earlier outage's cause.
- Renamed Living Room Media Panel Socket to **TV Power Tower**, renamed its
  integration title, and changed all 21 registered entity IDs to the
  `tv_power_tower` prefix (main switch: `switch.tv_power_tower`). Updated its
  Power dashboard reference. The power cycle restored network reachability,
  but the TP-Link integration still fails as described above.
- Removed the unplugged Coffee Machine Socket from HA's Matter integration
  and removed its Energy/Power dashboard entries. No recorder history or
  statistics were explicitly purged. The physical plug is awaiting a new use.
- HA is downstream of TV Power Tower, per Connor. Cycling that supply can
  interrupt HA itself; no remote relay-toggle test was performed.

## TV Power Tower migration to Matter

Connor subsequently updated firmware on all three P110M plugs and paired
TV Power Tower with HA through Matter. Its serial `3068939D04ED` matches the
old TP-Link device's MAC. The new Matter device reported an on-state and numeric
power, voltage, current and cumulative energy before migration.

Removed only the superseded TV Power Tower TP-Link config entry, then reused
the existing IDs for the equivalent Matter entities:

| Existing ID retained | Matter measurement |
| --- | --- |
| `switch.tv_power_tower` | Outlet relay |
| `sensor.tv_power_tower_current_consumption` | Power, W |
| `sensor.tv_power_tower_voltage` | Effective voltage, V |
| `sensor.tv_power_tower_current` | Effective current, A |

The new cumulative energy sensor is `sensor.tv_power_tower_energy` (kWh).
Other Matter entities use the same `tv_power_tower` prefix. Matter does not
expose the old TP-Link daily/monthly energy counters, LED, auto-off, or
protection configuration entities; these were not mapped to unrelated sensors.
The existing Power dashboard references the retained power sensor ID.
No relay toggle or restart was needed for this migration. Vacuum Cleaner and
Microwave were migrated subsequently, as recorded below.

## Vacuum and microwave migration to Matter

Connor then paired both remaining P110M plugs with Matter. Their serials matched
the registered MACs above. After confirming numeric power, voltage, current and
cumulative energy, removed their two superseded TP-Link entries and renamed
the new Matter entities.

- Vacuum Cleaner Socket is now **Vacuum Socket**, using `vacuum_socket` for all
  entity IDs. Main switch: `switch.vacuum_socket`; power:
  `sensor.vacuum_socket_current_consumption`; cumulative energy:
  `sensor.vacuum_socket_energy`. Updated its Power dashboard card and shortened
  that card's title to Vacuum.
- Microwave retains `switch.microwave_socket` and
  `sensor.microwave_socket_current_consumption`, preserving the existing
  pantry-door/idle automation and Power dashboard references. Other Matter
  entities now use the `microwave_socket` prefix; cumulative energy is
  `sensor.microwave_socket_energy`.

The old microwave IP configuration was retired with the TP-Link entry, so no
manual storage edit or DHCP reservation was needed. Its historical address
change remains recorded above. No relay-toggle tests were performed, and no
recorder history/statistics were explicitly purged.
