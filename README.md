# Home

My Home Assistant deployment.

## Features

### Bindicator

Daily at 4pm, query upcoming refuse collections using local council service API and
light up the living room lamp with the color of any bin(s) if they will be collected in
the next day.

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
   ansible-playbook -i inventory/local config.yaml
   ```
