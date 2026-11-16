# Home

My Home Assistant deployment.

Direct HTTPS, Namecheap dynamic DNS and certificate renewal:
[remote access](docs/remote-access.md).

## Features

House battery behavior and commissioning requirements: [native TOU control](docs/house-battery-control.md).

Room heating configuration and one-time setup: [VTherm commissioning](docs/heating-commissioning.md).

### Bindicator

Daily at 4pm, query upcoming refuse collections using local council service API and
light up the living room lamp with the color of any bin(s) if they will be collected in
the next day.

### Room Remotes

Room remotes are standardized via the `hue_remote_room_controls` blueprint with
label-based targeting. Lights are grouped by area + label intersection.

Label conventions:
- `primary`: main ceiling/primary lights
- `secondary`: lamps/secondary lights

How to label lights (Home Assistant UI):
1. Settings → Devices & Services → Entities.
2. Select the light entity.
3. Add the `primary` or `secondary` label.

Note: Labels live in Home Assistant's storage and are not currently configurable via
YAML. If we want a scripted approach, use the HA websocket API to set labels in the
entity registry.

### Thread and Matter

Thread and Matter are managed in Home Assistant OS through Supervisor apps and
integrations. The Ansible deployment covers the repository configuration.

- **Thread radio** — SONOFF Dongle Plus MG24 (`Dongle-PMG24`), running SONOFF
  OpenThread RCP `2.4.4.0`. Use its `/dev/serial/by-id/usb-SONOFF_SONOFF_Dongle_Plus_MG24_…`
  path in OpenThread Border Router, with `460800` baud, hardware flow control off,
  start on boot and watchdog enabled. The older ITead SONOFF dongle serves ZHA.
- **Thread network** — `ha-thread-bb2b`, channel 15, preferred in
  **Settings → Connectivity → Thread**. The Pi's Ethernet interface (`end0`)
  provides the LAN connection. On a rebuilt host, check `ha docker info`.
  If `enable_ipv6` is unset or false, run `ha docker options --enable-ipv6=true`
  and reboot the host to apply it.
- **Matter controller** — the official Matter Server app (`core_matter_server`)
  and the Matter integration. For a new Matter-over-Thread device, sync the
  preferred Thread network to the phone using the Home Assistant Companion app,
  then go to **Settings → Connectivity → Matter → Add device** and scan the
  Matter QR code. The IKEA BILRESA was successfully paired on 16 November 2026.

For recovery or a host move, restore a Home Assistant backup that includes Core,
OpenThread Border Router and Matter Server. Preserve the existing Thread dataset
and Matter fabric so paired devices can reconnect. Check the radio path and Docker
IPv6 on the restored host, then confirm the same network is preferred and test a
press on the BILRESA. Keep the Thread dataset, Matter pairing codes and backup
encryption key out of Git.

[SONOFF MG24 firmware](https://dongle.sonoff.tech/guide/dongle-pmg24/how_to_flash_openthread_firmware/)
· [Home Assistant Thread setup](https://www.home-assistant.io/integrations/thread/)
· [Matter commissioning](https://www.home-assistant.io/integrations/matter/#adding-a-matter-device-to-home-assistant)
· [Backup and restore](https://www.home-assistant.io/common-tasks/general/backups/#restoring-a-backup)

## Requirements

Install required packages

```shell
$ brew bundle
```

## Secrets

Deployment secrets are encrypted with `ansible-vault`.

The vault password is stored in 1password and accessed via the `op` CLI.

Ansible accesses the vault password via tha `op-client.py` adapter script.

To create a secret, run command of the form

```shell
$ cd deployment
$ ansible-vault encrypt_string --vault-id op-client.py "some-secret-string" --name "secret_name"
Encryption successful
secret_name: !vault |
          $ANSIBLE_VAULT;1.1;AES256
          64386635353466656666376263346333336461306639633236333033623862383139313030363233
          3861386365333864386538666339636630653765303764620a616135343034393439393933383035
          66343962613338383135393738636661303634306231396166616364313238343937313063323436
          3634653731663564660a323338386162666134343466316531373039323963303737356366646165
          3561
```

then add the variable to the appropriate file.

## Deployment

1. Create inventory describing home assistant server in the `homeassistant` group.
   ```yaml
   all:
   children:
     homeassistant:
       hosts:
         homeassistant.local:
           ha_root_dir: /root
   ```

2. Deploy configuration.
   ```shell
   cd deployment
   ansible-playbook --vault-id op-client.py -i inventory/local config.yaml
   ```

Notes:
- The deploy playbook uses the `op` CLI to read the Home Assistant API token, so ensure
  `op` is authenticated before running.
- Most config changes use targeted Home Assistant reload services; full restarts are
  reserved for custom components and non-reloadable config (e.g. HTTP/ZHA).
- The Home Assistant host uses Raspberry Pi SD-card storage, so recursive filesystem
  walks are relatively expensive. Git-backed custom components are installed directly
  from pinned GitHub archives instead of cloned and copied on every deploy.
