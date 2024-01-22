# Home

My Home Assistant apps and deployment.

## Features

### Bindicator

Daily at 5pm, query upcoming refuse collections using local council service API and
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
           config_dir: /root/config
   ```

2. Deploy configuration.
   ```shell
   cd deployment
   ansible-playbook -i inventories/local.yaml playbooks/config.yaml
   ```
